"""Offline tests for Pandit's stats path: new archive queries, the player index, planning
(LLM tool calls and keyword rules), the graph branch, and the QnA evaluation set.

The archive is the recorded SRH v RR match (IPL 2025, 1473439) loaded from Cricsheet; LLM
providers are fakes, so nothing touches the network.
"""

import gzip
import json
from pathlib import Path

import httpx
import pytest

from backend.agent import stats
from backend.agent.entities import resolve_entities
from backend.agent.llm import ChatProvider, LLMRouter, ProviderConfig
from backend.db import cricsheet_load, queries

FIXTURES = Path(__file__).parent / "fixtures"
EVAL = Path(__file__).resolve().parents[1] / "eval"
MATCH_ID = 1473439


@pytest.fixture()
def db(tmp_path, monkeypatch):
    with gzip.open(FIXTURES / "cricsheet_ipl2025_1473439.json.gz", "rt") as fh:
        match = json.load(fh)
    folder = tmp_path / "cricsheet"
    folder.mkdir()
    (folder / f"{MATCH_ID}.json").write_text(json.dumps(match), encoding="utf-8")
    monkeypatch.setattr(cricsheet_load, "REGISTER_FILE", FIXTURES / "cricsheet_people_1473439.csv")
    path = tmp_path / "stats.duckdb"
    cricsheet_load.load(path, directory=folder, download_register=False)
    monkeypatch.setenv("CRICBOT_DB", str(path))
    stats._INDEX.clear()
    return str(path)


def table(argv, db):
    block = next(b for b in queries.run(argv, db=db) if b["type"] == "table")
    return [dict(zip(block["columns"], row)) for row in block["rows"]], block["title"]


# ----------------------------------------------------------------- archive queries

def test_leaderboards_sort_and_filter_by_team(db):
    rows, title = table(["top-batters", "--sort", "sixes", "--team", "SRH"], db)
    assert title.startswith("Most sixes") and "for SRH" in title
    names = " ".join(r["player"] for r in rows)
    assert "Kishan" in names and not any(rr in names for rr in ("Samson", "Jurel", "Jaiswal", "Hetmyer"))
    assert rows[0]["6s"] == max(r["6s"] for r in rows)
    bowlers, _ = table(["top-bowlers", "--team", "RR"], db)          # RR bowled at SRH
    assert bowlers and all(r["runs"] is not None for r in bowlers)
    assert table(["top-bowlers", "--team", "SRH", "--sort", "econ"], db)[1].endswith("(min 600 balls)")


def test_matchup_venue_champions_and_team(db):
    rows, title = table(["matchup", "Ishan Kishan", "Jofra Archer"], db)
    assert "Ishan Kishan v JC Archer" in title and rows[-1]["year"] == "Total" and rows[-1]["balls"] > 0   # Cricsheet spelling
    assert table(["venues", "eden"], db)[0] == []
    ground, _ = table(["venues", "uppal"], db)                       # the full venue text counts ("..., Uppal, Hyderabad")
    assert ground[0]["matches"] == 1 and ground[0]["bat_first_won"] == 1 and ground[0]["highest_total"] == 286
    finals, _ = table(["champions"], db)
    assert finals == []                                             # a league match only
    season, _ = table(["team", "SRH"], db)
    assert season[0]["won"] == 1 and season[-1]["year"] == "Total"


# ----------------------------------------------------------------- player index

def test_player_index_resolves_full_names_initials_and_ambiguity():
    index = stats.PlayerIndex([stats.Player(1, "Virat Kohli", 300), stats.Player(2, "Rohit Sharma", 300),
                               stats.Player(3, "Ishant Sharma", 150), stats.Player(4, "V Iyer", 20),
                               stats.Player(5, "Travis Head", 50)])
    for p in index.players:
        for key in stats._name_keys(p.name):
            index.keys.setdefault(key, []).append(p)
    assert index.lookup("virat kohli")[0].player_id == 1
    assert index.lookup("Kohli")[0].player_id == 1
    player, options = index.lookup("Sharma")
    assert player is None and [p.name for p in options] == ["Rohit Sharma", "Ishant Sharma"]
    found, ambiguous = index.scan("How did Sharma do against Kohli in the head to head?")
    assert [p.name for p in found] == ["Virat Kohli"] and ambiguous == ["sharma"]   # "head" is not Travis Head


# ----------------------------------------------------------------- planning

def test_calls_are_validated_before_any_query():
    with pytest.raises(stats.PlanError):
        stats.to_argv({"name": "batting_leaders", "args": {"year": "2016; DROP TABLE"}})
    with pytest.raises(stats.PlanError):
        stats.to_argv({"name": "head_to_head", "args": {"team1": "MI", "team2": "XYZ"}})
    with pytest.raises(stats.PlanError):
        stats.to_argv({"name": "drop_everything", "args": {}})
    argv = stats.to_argv({"name": "batting_leaders", "args": {"year": "null", "sort": "sixes", "team": "Mumbai Indians",
                                                              "limit": 1}})
    assert argv == ["top-batters", "--sort", "sixes", "--limit", "10", "--team", "MI"]


@pytest.mark.parametrize("question, tool, args", [
    ("Who scored the most runs in IPL 2016?", "batting_leaders", {"sort": "runs", "year": "2016"}),
    ("Most sixes for MI in 2020", "batting_leaders", {"sort": "sixes", "team": "MI", "year": "2020"}),
    ("Who won the Purple Cap in 2013?", "bowling_leaders", {"sort": "wkts", "year": "2013"}),
    ("Best death bowlers in 2024", "phase_leaders", {"phase": "death", "role": "bowl", "year": "2024"}),
    ("Who won IPL 2019?", "champions", {"year": "2019"}),
    ("MI vs CSK head to head", "head_to_head", {}),
    ("How many matches did RCB win in 2025?", "team_seasons", {"team": "RCB", "year": "2025"}),
    ("Highest team total ever", "records", {}),
    ("Average first innings score at Chepauk", "venue_stats", {"name": "Chidambaram"}),
])
def test_rule_planner(question, tool, args):
    calls = stats.rule_plan(question, resolve_entities(question, current_year=2026), None)
    assert calls and calls[0]["name"] == tool
    assert all(calls[0]["args"].get(k) == v for k, v in args.items())


@pytest.mark.parametrize("question", ["Who won the 2016 final?", "Show me the momentum in the 2025 final",
                                      "Ball by ball of the 2019 final"])
def test_single_match_questions_stay_on_the_match_path(question):
    assert stats.rule_plan(question, resolve_entities(question), None) == []


class FakeRouter:
    """Stands in for LLMRouter: fixed tool calls for planning, fixed prose for answers."""

    def __init__(self, calls=None, reply="", fail_plan=False):
        self.calls, self.reply, self.fail_plan = calls or [], reply, fail_plan
        self.providers = []

    def plan(self, messages, tools):
        if self.fail_plan:
            raise RuntimeError("groq: HTTP 429 rate limit")
        assert any(t["function"]["name"] == "route_to_match" for t in tools)
        return self.calls, "groq", "openai/gpt-oss-20b"

    def chat(self, messages, **kwargs):
        if not self.reply:
            raise RuntimeError("no provider")
        return self.reply, "groq", "openai/gpt-oss-120b"

    def status(self):
        return []


def test_llm_plan_wins_and_rules_cover_what_it_misses():
    q = "Compare Dhoni and Pant as batters"
    index = stats.PlayerIndex([stats.Player(1, "MS Dhoni", 250), stats.Player(2, "Rishabh Pant", 120)])
    for p in index.players:
        for key in stats._name_keys(p.name):
            index.keys.setdefault(key, []).append(p)
    one = FakeRouter([{"name": "player_profile", "args": {"name": "MS Dhoni"}}])
    calls, planner, _ = stats.plan(q, resolve_entities(q), one, index=index)
    assert len(calls) == 2 and planner.startswith("rules (over groq")                 # the LLM planned one player of two
    sixes = FakeRouter([{"name": "batting_leaders", "args": {"sort": "sixes", "year": "2024"}}])
    calls, planner, _ = stats.plan("who smashed the most maximums in 2024", {}, sixes, index=index)
    assert calls[0]["args"]["sort"] == "sixes" and planner.startswith("groq")
    calls, planner, note = stats.plan("Most sixes in 2024", resolve_entities("Most sixes in 2024"),
                                      FakeRouter(fail_plan=True), index=index)
    assert calls and planner == "rules" and "unavailable" in note
    calls, _, _ = stats.plan("Who won the 2016 final?", {}, FakeRouter([{"name": "route_to_match", "args": {}}]),
                             index=index)
    assert calls == []


def test_provider_parses_tool_calls_and_prose_refusals():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-20b" and body["reasoning_effort"] == "low" and body["tools"]
        if "final" in body["messages"][-1]["content"]:
            return httpx.Response(400, json={"error": {"code": "tool_use_failed"}})
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {
            "name": "batting_leaders", "arguments": json.dumps({"year": "2016"})}}]}}]})

    config = ProviderConfig("groq", "https://api.groq.test", "openai/gpt-oss-120b", "TEST_KEY", {}, "openai/gpt-oss-20b")
    provider = ChatProvider(config, client=httpx.Client(base_url="https://api.groq.test", transport=httpx.MockTransport(handler)))
    provider.api_key = "test"
    router = LLMRouter([provider])
    calls, name, model = router.plan([{"role": "user", "content": "most runs 2016"}], stats.TOOLS)
    assert calls == [{"name": "batting_leaders", "args": {"year": "2016"}}] and model == "openai/gpt-oss-20b"
    assert router.plan([{"role": "user", "content": "the 2016 final"}], stats.TOOLS)[0] == []


# ----------------------------------------------------------------- the graph branch

def test_graph_answers_stats_with_tables_and_rejects_invented_numbers(db, monkeypatch):
    from backend.agent.service import CricbotAgent

    plan = [{"name": "batting_leaders", "args": {"year": "2025"}}]
    good = CricbotAgent(llm=FakeRouter(plan, "Ishan Kishan led with 106 runs."))
    result = good.answer("Who scored the most runs in IPL 2025?")
    assert result["validation"] == "grounded" and result["reply"] == "Ishan Kishan led with 106 runs."
    card = result["cards"][0]
    assert card["type"] == "stats" and card["blocks"][0]["rows"][0][0] == "Ishan Kishan"
    assert result["tool_calls"][0]["command"].startswith("top-batters --sort runs --year 2025")
    nodes = [event["node"] for event in result["trace"]]
    assert "run_stats" in nodes and "load_match" not in nodes

    invented = CricbotAgent(llm=FakeRouter(plan, "Ishan Kishan made 999 runs."))
    result = invented.answer("Who scored the most runs in IPL 2025?")
    assert result["validation"] == "fallback-after-validation"
    assert "Ishan Kishan" in result["reply"] and "106" in result["reply"] and "999" not in result["reply"]


def test_graph_without_llm_uses_rules_and_a_summary(db):
    from backend.agent.service import CricbotAgent

    agent = CricbotAgent(llm=FakeRouter(fail_plan=True))
    result = agent.answer("Who hit the most sixes in IPL 2025?")
    assert result["cards"][0]["type"] == "stats" and "sixes" in result["reply"]
    plan = next(e for e in result["trace"] if e["node"] == "plan_question")
    assert plan["patch"]["planner"] == "rules" and plan["patch"]["route"] == "stats"


def test_unknown_player_becomes_a_question_not_an_error(db):
    from backend.agent.service import CricbotAgent

    agent = CricbotAgent(llm=FakeRouter([{"name": "player_profile", "args": {"name": "Don Bradman"}}]))
    result = agent.answer("Don Bradman's IPL record?")
    assert "no IPL player matching" in result["reply"]
    assert result["tool_calls"][0]["error"]


# ----------------------------------------------------------------- evaluation set

def test_question_sets_are_well_formed():
    from backend.eval.run_qna import answer_ok, contains

    for name, minimum in (("qna_ipl.jsonl", 100), ("qna_ipl_heldout.jsonl", 40)):
        items = [json.loads(line) for line in (EVAL / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(items) >= minimum and len({i["id"] for i in items}) == len(items)
        for item in items:
            assert item["expected_route"] in ("stats", "match") and item["verified"] in ("published", "archive", "n/a")
            assert item["expected_route"] == "match" or item["checks"], item["id"]
            assert item["expected_tool"] is None or item["expected_tool"] in stats.TOOL_NAMES
    assert contains("Kohli made 973 runs", "973") and not contains("Kohli made 9736 runs", "973")
    assert contains("economy of 6.48", "6.48") and not contains("6.489", "6.48")
    assert answer_ok("CSK won", [["CSK", "Chennai"]]) and not answer_ok("MI won", [["CSK", "Chennai"]])


def test_plans_are_grounded_in_the_question():
    """LLM plans may only use players and years the question gives (seen on the held-out set)."""
    q = "chahal wickets total"
    assert stats.ground_calls([{"name": "player_profile", "args": {"name": "Jasprit Bumrah"}}], q, resolve_entities(q))[0] == []
    q = "orange cap holder 2019"
    kept, _ = stats.ground_calls([{"name": "batting_leaders", "args": {"year": "2018"}}], q, resolve_entities(q))
    assert kept[0]["args"]["year"] == "2019"
    q = "most centuries in ipl"
    kept, _ = stats.ground_calls([{"name": "batting_leaders", "args": {"sort": "hundreds", "year": "2024"}}], q,
                                 resolve_entities(q))
    assert "year" not in kept[0]["args"]
    q = "most runs this season"
    kept, _ = stats.ground_calls([{"name": "batting_leaders", "args": {"year": "2026"}}], q, resolve_entities(q))
    assert kept[0]["args"]["year"] == "2026"
    index = stats.PlayerIndex([stats.Player(1, "Kieron Pollard", 200)])
    index.keys = {k: index.players for k in stats._name_keys("Kieron Pollard")}
    assert index.lookup("Chris Pollard")[0].name == "Kieron Pollard"


def test_rate_limited_model_falls_through_to_the_next():
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "openai/gpt-oss-20b":
            return httpx.Response(429, json={"error": {"message": "tokens per day (TPD)"}})
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {
            "name": "champions", "arguments": "{}"}}]}}]})

    config = ProviderConfig("groq", "https://api.groq.test", "openai/gpt-oss-120b", "TEST_KEY", {},
                            "openai/gpt-oss-20b,qwen/qwen3.8-27b")
    provider = ChatProvider(config, client=httpx.Client(base_url="https://api.groq.test", transport=httpx.MockTransport(handler)))
    provider.api_key = "test"
    calls, name, model = LLMRouter([provider]).plan([{"role": "user", "content": "who won IPL 2019"}], stats.TOOLS)
    assert seen == ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"] and model == "qwen/qwen3.8-27b" and calls[0]["name"] == "champions"


def test_v2_set_and_robustness_scoring():
    from backend.eval.run_qna import asked, entity_check, signature

    items = [json.loads(line) for line in (EVAL / "qna_ipl_v2.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(items) >= 70 and len({i["id"] for i in items}) == len(items)
    groups = {}
    for item in items:
        assert item["expect"] in ("answer", "clarify", "refuse") and item["checks"]
        if item["group"]:
            groups.setdefault(item["group"], []).append(item)
    assert len(groups) >= 15 and all(len(g) == 3 for g in groups.values())

    assert asked("Which Sharma do you mean: Rohit Sharma or Mohit Sharma?")
    assert not asked("Rohit Sharma scored 417 runs in 2024.")
    index = stats.PlayerIndex([stats.Player(1, "Virat Kohli", 300), stats.Player(2, "Jasprit Bumrah", 200)])
    for p in index.players:
        for key in stats._name_keys(p.name):
            index.keys.setdefault(key, []).append(p)
    item = {"entities": {"players": ["Virat Kohli", "Jasprit Bumrah"]}}
    ok, _ = entity_check(item, [{"name": "batter_vs_bowler", "args": {"batter": "Kohli", "bowler": "Bumrah"}}], index)
    assert ok
    ok, note = entity_check({"entities": {"players": ["Yuzvendra Chahal"]}},
                            [{"name": "player_profile", "args": {"name": "Jasprit Bumrah"}}], index)
    assert ok is False and "bumrah" in note
    ok, note = entity_check({"entities": {"years": [2016]}}, [{"name": "batting_leaders", "args": {"year": "2015"}}], index)
    assert ok is False and "2015" in note
    a = signature([{"name": "champions", "args": {"year": "2016"}}])
    assert a == signature([{"name": "champions", "args": {"year": "2016", "x": None}}])


def test_a_venue_name_is_not_a_player_name():
    """Rules planning venue_stats(name=...) must not override an LLM plan that names no player."""
    llm = [{"name": "head_to_head", "args": {"team1": "CSK", "team2": "MI"}}]
    rules = [{"name": "venue_stats", "args": {"name": "Chidambaram"}}]
    assert not stats.prefer_rules(llm, rules)
    assert stats.prefer_rules([{"name": "batting_leaders", "args": {}}], [{"name": "player_profile", "args": {"name": "MS Dhoni"}}])
