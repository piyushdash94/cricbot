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


# ----------------------------------------------------------------- v2 fixes

def _index():
    return stats.PlayerIndex.build([
        stats.Player(1, "Virat Kohli", 300, 275, 26), stats.Player(2, "Jasprit Bumrah", 160, 40, 158),
        stats.Player(3, "Rashid Khan", 150, 72, 153), stats.Player(4, "AU Rashid", 3, 1, 3),
        stats.Player(5, "Hardik Pandya", 200, 150, 116), stats.Player(6, "Krunal Pandya", 150, 110, 120),
        stats.Player(7, "Suryakumar Yadav", 165, 164, 1), stats.Player(8, "MS Dhoni", 250, 241, 0),
        stats.Player(9, "Arun Karthik", 8, 8, 0)])


def test_first_names_and_nicknames_resolve_but_shared_surnames_still_ask():
    index = _index()
    assert index.lookup("Rashid")[0].name == "Rashid Khan"            # AU Rashid has 3 innings
    assert index.lookup("hardik")[0].name == "Hardik Pandya"
    assert index.lookup("SKY")[0].name == "Suryakumar Yadav"
    assert index.lookup("Pandya")[0] is None
    found, ambiguous = index.scan("Suryakumar's runs in 2023 and Pandya's")
    assert [p.name for p in found] == ["Suryakumar Yadav"] and ambiguous == ["pandya"]
    assert index.scan("average first innings at the Arun Jaitley Stadium")[0] == []   # a ground, not a player


@pytest.mark.parametrize("question, batter", [
    ("Bumrah vs Kohli", "Virat Kohli"), ("Kohli against Bumrah", "Virat Kohli"),
    ("Rashid Khan bowling to Dhoni", "MS Dhoni"), ("Dhoni facing Rashid Khan", "MS Dhoni"),
])
def test_matchups_put_the_batter_first_whatever_the_word_order(question, batter):
    index = _index()
    calls = stats.rule_plan(question, resolve_entities(question), index)
    calls = stats.orient_matchups(calls, question, index)
    assert calls[0]["name"] == "batter_vs_bowler" and calls[0]["args"]["batter"] == batter


@pytest.mark.parametrize("question, topic", [
    ("Who scored the most runs in WPL 2024?", "women"), ("What is Virat Kohli's Test batting average?", "international"),
    ("PSL 2025 winner", "leagues"), ("How much did Rishabh Pant sell for in the IPL auction?", "auction"),
    ("Who will win IPL 2027?", "predictions"), ("Which club has won the most ISL titles?", "sports"),
])
def test_out_of_scope_questions_are_named(question, topic):
    assert topic in stats.out_of_scope(question)


@pytest.mark.parametrize("question", [
    "Who won IPL 2016?", "most sixes in IPL 2024", "Kohli against Bumrah", "average first innings at Wankhede",
    "most runs by an overseas player", "What's the average score at Chepauk?", "Indian Premier League records",
])
def test_ipl_questions_are_in_scope(question):
    assert stats.out_of_scope(question) is None


def test_graph_refuses_out_of_scope_without_running_queries(db):
    from backend.agent.service import CricbotAgent

    result = CricbotAgent(llm=FakeRouter(fail_plan=True)).answer("Most wickets in the Big Bash League 2024")
    assert "only have IPL" in result["reply"] and result["tool_calls"] == []
    assert result["cards"][0]["type"] == "scope" and result["cards"][0]["suggestions"]


def test_ambiguous_names_come_back_as_choices():
    results = [{"name": "player_profile", "args": {"name": "Pandya"}, "error": "x", "ambiguous": "Pandya",
                "choices": ["Hardik Pandya", "Krunal Pandya"], "blocks": []}]
    card = stats.clarify_card(results, "Pandya's batting record in the IPL")
    assert card["type"] == "clarify"
    assert card["options"][0] == {"label": "Hardik Pandya", "question": "Hardik Pandya's batting record in the IPL"}


def test_llm_plans_keep_the_question_year_and_do_not_guess_first_names():
    index = _index()
    q = "Pandya's batting record in the IPL"
    kept, _ = stats.ground_calls([{"name": "player_profile", "args": {"name": "Hardik Pandya"}}], q,
                                 resolve_entities(q), index=index)
    assert kept[0]["args"]["name"] == "pandya"                        # the answer asks which Pandya
    q = "Rohit's runs"                                                # first name only: still grounded
    index = stats.PlayerIndex.build([stats.Player(1, "Rohit Sharma", 300, 276, 32)])
    kept, dropped = stats.ground_calls([{"name": "player_profile", "args": {"name": "Rohit Sharma"}}], q,
                                       resolve_entities(q), index=index)
    assert kept and not dropped
    q = "Best death-overs bowler in 2024"
    kept, _ = stats.ground_calls([{"name": "phase_leaders", "args": {"phase": "death", "role": "bowl"}}], q,
                                 resolve_entities(q))
    assert kept[0]["args"]["year"] == "2024"


@pytest.mark.parametrize("question, tool, args", [
    ("who hit the most maximums in 2024", "batting_leaders", {"sort": "sixes", "year": "2024"}),
    ("most economical bowler in the first six overs, 2023", "phase_leaders", {"phase": "powerplay", "role": "bowl"}),
    ("top bowler in overs 16 to 20, 2024 season", "phase_leaders", {"phase": "death", "role": "bowl"}),
    ("leading batter for Knight Riders, 2024", "batting_leaders", {"sort": "runs", "team": "KKR"}),
    ("number of IPL championships for Mumbai", "team_seasons", {"team": "MI"}),
    ("Chennai against Mumbai: the record", "head_to_head", {"team1": "CSK", "team2": "MI"}),
    ("record individual innings in the IPL", "records", {}),
    ("chasing at Chennai: average score", "venue_stats", {"name": "Chidambaram"}),
])
def test_rule_planner_reads_paraphrases(question, tool, args):
    calls = stats.rule_plan(question, resolve_entities(question), None)
    assert calls and calls[0]["name"] == tool
    assert args.items() <= calls[0]["args"].items()


def test_summary_answers_the_column_and_role_asked():
    batting = {"type": "table", "title": "Andre Russell: batting by year", "columns": ["year", "runs", "sr"],
               "rows": [[2024, 222, 185.0], ["Total", 2651, 174.18]]}
    bowling = {"type": "table", "title": "Andre Russell: bowling by year", "columns": ["year", "wkts", "sr"],
               "rows": [[2024, 19, 13.0], ["Total", 123, 14.7]]}
    results = [{"name": "player_profile", "args": {}, "blocks": [bowling, batting]}]
    assert "174.18" in stats.summary(results, "Andre Russell's IPL strike rate")
    assert "123" in stats.summary(results, "Russell's wickets")
    team = {"type": "table", "title": "MI season by season", "columns": ["year", "won", "finish"],
            "rows": [[2013, 13, "Champions"], [2014, 7, "Playoffs"], [2015, 10, "Champions"], ["Total", 30, None]]}
    reply = stats.summary([{"name": "team_seasons", "args": {"team": "MI"}, "blocks": [team]}], "MI trophy count")
    assert reply.startswith("MI won 2 IPL titles: 2013, 2015")


def test_hybrid_keeps_the_matchup_and_drops_invalid_llm_calls():
    index = _index()
    llm = [{"name": "player_profile", "args": {"name": "Jasprit Bumrah"}}]
    rules = [{"name": "batter_vs_bowler", "args": {"batter": "Virat Kohli", "bowler": "Jasprit Bumrah"}}]
    assert stats.prefer_rules(llm, rules)
    kept, invalid = stats._valid_calls([{"name": "team_seasons", "args": {"team": "Narine"}},
                                        {"name": "player_profile", "args": {"name": "Pandya"}}], index)
    assert [c["name"] for c in kept] == ["player_profile"] and "unknown team" in invalid[0]


def test_ground_report_compares_with_the_league(db):
    blocks = queries.run(["ground", "uppal"], db=db)          # the fixture: one match at Hyderabad
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) == 3 and tables[0]["rows"][-1][0] == "Total"
    assert {"avg_1st_inns", "league_1st", "vs_league", "chase_win_pct"} <= set(tables[0]["columns"])
    assert [r[0] for r in tables[1]["rows"]] == ["powerplay", "middle", "death"]
    assert "not a pitch reading" in blocks[-1]["text"]
    assert queries.run(["ground", "nowhere"], db=db)[0]["text"].startswith("No IPL ground")
    calls = stats.rule_plan("what is the pitch like at Chepauk?", {}, None)
    assert calls == [{"name": "ground_report", "args": {"name": "Chidambaram"}}]


def test_graph_answers_an_ambiguous_name_with_choices(db, monkeypatch):
    from backend.agent.service import CricbotAgent

    index = stats.PlayerIndex.build([stats.Player(1, "Hardik Pandya", 200, 150, 116),
                                     stats.Player(2, "Krunal Pandya", 150, 110, 120)])
    monkeypatch.setattr(stats, "player_index", lambda: index)
    result = CricbotAgent(llm=FakeRouter(fail_plan=True)).answer("Pandya's batting record in the IPL")
    assert [c["type"] for c in result["cards"]] == ["clarify"]
    assert [o["label"] for o in result["cards"][0]["options"]] == ["Hardik Pandya", "Krunal Pandya"]
    assert "which one" in result["reply"]


# ----------------------------------------------------------------- follow-ups

@pytest.mark.parametrize("history, question, standalone", [
    (["How many runs did Virat Kohli score in IPL 2016?"], "And in 2017?",
     "How many runs did Virat Kohli score in IPL 2017?"),
    (["Who took the most wickets in IPL 2018?"], "What about 2019?", "Who took the most wickets in IPL 2019?"),
    (["Tell me about MS Dhoni's IPL batting"], "What's his strike rate in the death overs?",
     "What's MS Dhoni's strike rate in the death overs?"),
    (["How many runs did Virat Kohli score in IPL 2016?"], "What about Bumrah?",
     "How many runs did Jasprit Bumrah score in IPL 2016?"),
    (["Most sixes in IPL"], "and in 2024?", "Most sixes in IPL in 2024?"),
    # Questions that stand alone are left alone.
    (["How many runs did Virat Kohli score in IPL 2016?"], "Who won IPL 2019?", "Who won IPL 2019?"),
    (["Tell me about MS Dhoni"], "Jasprit Bumrah's economy in 2024", "Jasprit Bumrah's economy in 2024"),
    ([], "And in 2017?", "And in 2017?"),
])
def test_follow_ups_become_standalone_questions(history, question, standalone):
    turns = [{"role": "user", "content": h} for h in history]
    assert stats.contextualize(question, turns, _index()) == standalone


def test_graph_answers_a_follow_up_from_history(db):
    from backend.agent.service import CricbotAgent

    agent = CricbotAgent(llm=FakeRouter(fail_plan=True))
    result = agent.answer("And in 2025?", history=[{"role": "user", "content": "Who hit the most sixes in IPL 2024?"},
                                                   {"role": "assistant", "content": "Abhishek Sharma, 42."}])
    first = next(e for e in result["trace"] if e["node"] == "normalize_request")
    assert "rewritten" in first["summary"] and "2025" in first["summary"]
    assert result["cards"][0]["type"] == "stats" and "sixes" in result["reply"]


# ----------------------------------------------------------------- gaps and traps (answer safety)

def test_new_queries_run_on_the_fixture(db):
    blocks = queries.run(["overs", "--from", "16", "--to", "20", "--innings", "2", "--min-balls", "1"], db=db)
    assert blocks[0]["title"].startswith("Batters by strike rate in overs 16-20, second innings")
    assert queries.run(["overs", "--from", "1", "--role", "bowl", "--sort", "wkts", "--min-balls", "1"], db=db)[0]["rows"]
    assert queries.run(["results", "close_runs", "--value", "50"], db=db)[-1]["type"] == "text"
    assert queries.run(["results", "defended_under", "--value", "300"], db=db)[0]["columns"][0] == "team"
    assert "Fewest innings" in queries.run(["milestone", "runs", "10"], db=db)[0]["title"]
    played = queries.run(["player-match", "Ishan Kishan", "1"], db=db)
    assert played[0]["title"].endswith("IPL match number 1") and "playing XIs" in played[-1]["text"]


@pytest.mark.parametrize("question, tool, args", [
    ("who has the highest strike rate in overs 12-20 in second innings?", "over_range_leaders",
     {"from_over": 12, "to_over": 20, "innings": 2, "role": "bat"}),
    ("who has taken the most wickets in the 1st over of the innings?", "over_range_leaders",
     {"from_over": 1, "to_over": 1, "role": "bowl", "sort": "wkts"}),
    ("in how many matches was the win margin less than 5 runs ?", "match_results", {"kind": "close_runs", "value": 5}),
    ("which team has defended targets under 130 the most in IPL?", "match_results", {"kind": "defended_under", "value": 130}),
    ("has any team chased more than 100 runs required in the last 4 overs of chase?", "match_results",
     {"kind": "needed_at_over", "over": 17}),
    ("who has bowled the least balls to take 100 ipl wickets?", "milestones", {"stat": "wickets", "value": 100, "by": "balls"}),
    ("how many matches have ended in a tie?", "match_results", {"kind": "tied"}),
])
def test_rule_planner_reaches_the_new_queries(question, tool, args):
    calls = stats.rule_plan(question, resolve_entities(question), None)
    assert calls and calls[0]["name"] == tool and args.items() <= calls[0]["args"].items()


def test_milestones_take_every_value_and_years_ignore_run_counts():
    q = "who was the fastest to 2000, 3000 runs in all time history?"
    assert [c["args"]["value"] for c in stats.rule_plan(q, resolve_entities(q), None)] == [2000, 3000]
    assert resolve_entities(q)["years"] == []                       # 2000 runs is not the 2000 season
    assert resolve_entities("most runs in 2016")["years"] == [2016]


def test_gaps_are_named_instead_of_answering_a_nearby_question():
    assert stats.capability_gaps("who has taken the most hattricks for MI?", []) == ["hat-tricks"]
    assert stats.capability_gaps("highest strike rate in overs 12-20", [{"name": "phase_leaders", "args": {}}]) \
        == ["these specific overs"]
    assert stats.capability_gaps("highest strike rate in overs 12-20", [{"name": "over_range_leaders", "args": {}}]) == []
    assert stats.capability_gaps("best death bowlers in 2024", [{"name": "phase_leaders", "args": {}}]) == []
    assert stats.is_aggregate("in how many matches was the margin under 5 runs") and not stats.is_aggregate("where did this match turn")


def test_on_screen_teams_stay_with_questions_about_that_match():
    ui = {"teams": ["KKR", "PBKS"]}
    assert resolve_entities("Where did it turn?", ui_context=ui)["teams"] == ["KKR", "PBKS"]
    assert resolve_entities("who scored the most runs in this match?", ui_context=ui)["teams"] == ["KKR", "PBKS"]
    assert resolve_entities("in how many matches was the win margin less than 5 runs?", ui_context=ui)["teams"] == []


def test_graph_says_so_when_no_query_covers_a_whole_ipl_question(db):
    from backend.agent.service import CricbotAgent

    result = CricbotAgent(llm=FakeRouter(fail_plan=True)).answer("who has taken the most hat-tricks in the IPL?")
    assert result["reply"].startswith("I can't answer that yet") and "hat-tricks" in result["reply"]
    assert result["tool_calls"] == [] and result["cards"][0]["type"] == "scope"


def test_checks_reject_invented_names_wrong_winners_and_one_name_for_a_tie(monkeypatch):
    from backend.agent.service import CricbotAgent

    index = stats.PlayerIndex.build([stats.Player(1, "Trent Boult", 100, 10, 100), stats.Player(2, "Ajit Chandila", 12, 2, 12),
                                     stats.Player(3, "Bhuvneshwar Kumar", 150, 40, 150)])
    monkeypatch.setattr(stats, "player_index", lambda: index)
    monkeypatch.setattr(stats, "available", lambda: True)
    facts = "Bowlers by wickets in over 1: Trent Boult 32, Bhuvneshwar Kumar 31"
    assert CricbotAgent._names_are_grounded("Trent Boult has 32.", facts, "most wickets in over 1")
    assert not CricbotAgent._names_are_grounded("Ajit Chandila has 10.", facts, "most wickets in over 1")
    card = {"type": "match", "match": {"status_text": "KKR won by 3 wickets", "teams": [{"abbr": "KKR"}, {"abbr": "PBKS"}]}}
    assert not CricbotAgent._result_is_consistent("The collapse handed the win to Punjab.", [card])
    assert CricbotAgent._result_is_consistent("Punjab's late burst could not stop KKR, who won by 3 wickets.", [card])
    block = {"type": "table", "title": "Most hundreds", "columns": ["player", "100s"],
             "rows": [["David Warner", 2], ["Abhishek Sharma", 2], ["Jonny Bairstow", 1]]}
    results = [{"name": "batting_leaders", "args": {"sort": "hundreds"}, "blocks": [block]}]
    assert stats.top_tie(results) == ["David Warner", "Abhishek Sharma"]
    assert not stats.mentions_tie("David Warner has the most hundreds, 2.", results)
    assert stats.mentions_tie("Warner and Abhishek Sharma are tied on 2 hundreds.", results)
    assert "are tied on hundreds 2" in stats.summary(results, "most centuries")


def test_number_check_reads_formatted_numbers_and_dates():
    from backend.agent.service import CricbotAgent

    facts = "Fewest innings to 1000 IPL runs: Shaun Marsh 21, reached_on 2011-05-02"
    assert CricbotAgent._numbers_are_grounded("Shaun Marsh reached 1,000 runs in 21 innings on 2 May 2011.", facts, "")
    assert not CricbotAgent._numbers_are_grounded("Shaun Marsh reached 1,000 runs in 19 innings.", facts, "")


def test_llm_innings_filter_needs_the_question_to_ask_for_it():
    q = "who has taken the most wickets in the 1st over of the innings?"
    kept, _ = stats.ground_calls([{"name": "over_range_leaders", "args": {"from_over": 1, "innings": 1, "role": "bowl"}}],
                                 q, resolve_entities(q))
    assert "innings" not in kept[0]["args"]
    q = "best strike rate in overs 12-20 while chasing"
    kept, _ = stats.ground_calls([{"name": "over_range_leaders", "args": {"from_over": 12, "innings": 2, "role": "bat"}}],
                                 q, resolve_entities(q))
    assert kept[0]["args"]["innings"] == 2


def test_precise_rules_win_and_unanswerable_gaps_skip_the_queries(db):
    from backend.agent.service import CricbotAgent

    rules = [{"name": "match_results", "args": {"kind": "needed_at_over", "over": 17}}]
    assert stats.prefer_rules([{"name": "match_results", "args": {"kind": "chased_over", "value": 100}}], rules)
    assert not stats.prefer_rules(rules, rules)
    result = CricbotAgent(llm=FakeRouter([{"name": "records", "args": {}}])).answer("who has taken the most hat-tricks?")
    assert result["reply"].startswith("I can't answer that yet") and result["tool_calls"] == []


def test_a_reply_without_numbers_is_not_an_answer_when_tables_have_rows():
    from backend.agent.service import CricbotAgent

    state = {"route": "stats", "gaps": [], "stats_results": [{"blocks": [{"type": "table", "rows": [["Dwayne Bravo", 39]]}]}]}
    assert CricbotAgent._is_non_answer("The tables do not contain data on the last over.", state)
    assert not CricbotAgent._is_non_answer("Dwayne Bravo has 39 wickets in the last over.", state)
    assert not CricbotAgent._is_non_answer("I can't break this down by these specific overs.", {**state, "gaps": ["x"]})
    note = {"route": "stats", "gaps": [], "stats_results": [{"blocks": [{"type": "text", "text": "5 matches were won by fewer than 2 wickets."}]}]}
    assert CricbotAgent._is_non_answer("The tables do not provide that number.", note)


def test_last_n_overs_comes_from_the_question():
    q = "has any team chased more than 100 runs required in the last 4 overs of chase?"
    kept, _ = stats.ground_calls([{"name": "match_results", "args": {"kind": "needed_at_over", "over": 16}}], q, resolve_entities(q))
    assert kept[0]["args"]["over"] == 17
