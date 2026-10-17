"""Stats questions for Pandit: choose archive queries, run them, and turn the tables into facts.

A question about one match ("who won the 2016 final?") stays on the match path. A question
across matches ("most sixes in IPL 2024", "Kohli against Bumrah", "how many titles has CSK
won?") is answered from the DuckDB archive with the same verified commands as
`python -m backend.db.queries` and the Stats view:

    plan  ->  [{"name": "batting_leaders", "args": {"year": "2024", "sort": "sixes"}}]
    run   ->  query blocks (tables) for each call
    facts ->  the tables as text, which the LLM may only summarise

Planning uses the LLM's tool calling when a provider is configured, and `rule_plan`
(keywords plus resolved entities) when it is not or when the LLM does not choose a tool.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from backend.db import queries, store

from .entities import TEAM_ALIASES, _team_entities

MATCH_ROUTE = "route_to_match"
YEAR_PATTERN = re.compile(r"^\d{4}(-\d{4})?$")
TEAM_CODES = set(TEAM_ALIASES)
MAX_CALLS = 4
TIME_WORDS = re.compile(r"\b(19|20)\d\d\b|\b(season|seasons|year|years|edition|this|last|current|previous|recent|ago)\b")
ON_SCREEN = re.compile(r"\b(this|that|the current) (match|game|innings|chase)\b|\bin this one\b")

# Short schemas: every planning call sends them, and Groq's free tier counts tokens per minute and per day.
YEAR_PARAM = {"type": "string", "description": "2016, or 2016-2018; omit for all time"}
TEAM_PARAM = {"type": "string", "description": "team code"}
PLAYER_PARAM = {"type": "string", "description": "full name"}


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


TOOLS: list[dict[str, Any]] = [
    _fn(MATCH_ROUTE, "Question is about ONE match (a final, a named game, its scorecard, momentum, commentary) "
        "or is not an IPL stats question.", {}),
    _fn("batting_leaders", "Batting leaderboard: most runs (Orange Cap), sixes, fours, fifties, hundreds, highest "
        "score, strike rate or average; optionally one team.", {
            "year": YEAR_PARAM, "team": TEAM_PARAM, "sort": {"type": "string", "enum": list(queries.BAT_SORTS)},
            "min_balls": {"type": "integer"}}),
    _fn("bowling_leaders", "Bowling leaderboard: most wickets (Purple Cap), economy, average, strike rate, "
        "four-wicket hauls; optionally one team.", {
            "year": YEAR_PARAM, "team": TEAM_PARAM, "sort": {"type": "string", "enum": list(queries.BOWL_SORTS)},
            "min_balls": {"type": "integer"}}),
    _fn("player_profile", "One player's batting and bowling by year (with totals) and by phase.",
        {"name": PLAYER_PARAM, "year": YEAR_PARAM}, ["name"]),
    _fn("batter_vs_bowler", "Every ball a batter faced from a bowler.",
        {"batter": PLAYER_PARAM, "bowler": PLAYER_PARAM, "year": YEAR_PARAM}, ["batter", "bowler"]),
    _fn("phase_leaders", "Best batters (strike rate) or bowlers (economy) in powerplay (1-6), middle (7-15) or "
        "death (16-20) overs.", {
            "phase": {"type": "string", "enum": ["powerplay", "middle", "death"]},
            "role": {"type": "string", "enum": ["bat", "bowl"]}, "year": YEAR_PARAM}, ["phase", "role"]),
    _fn("records", "Highest team totals, highest individual scores, best figures, fastest fifties, most sixes "
        "in an innings.", {"year": YEAR_PARAM}),
    _fn("head_to_head", "Results between two teams by season.",
        {"team1": TEAM_PARAM, "team2": TEAM_PARAM, "year": YEAR_PARAM}, ["team1", "team2"]),
    _fn("team_seasons", "One team by season: played, won, lost, how far they got; titles.",
        {"team": TEAM_PARAM, "year": YEAR_PARAM}, ["team"]),
    _fn("champions", "Each final's champion, runner-up and result; titles by team.", {"year": YEAR_PARAM}),
    _fn("venue_stats", "Grounds: average first innings, bat-first vs chasing wins, highest total.",
        {"name": {"type": "string", "description": "ground or nickname, e.g. Wankhede, Chepauk"}, "year": YEAR_PARAM}),
    _fn("season_summary", "Per season: matches, run rate, average first innings, highest total, total sixes.",
        {"year": YEAR_PARAM}),
]
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}

# The column a one-line answer leads with, per tool (and sort).
BAT_KEY = {"runs": "runs", "sr": "sr", "avg": "avg", "sixes": "6s", "fours": "4s",
           "fifties": "50s", "hundreds": "100s", "hs": "hs"}
BOWL_KEY = {"wkts": "wkts", "econ": "econ", "avg": "avg", "sr": "sr", "4w": "4w+"}
GROUND_ALIASES = {"chepauk": "Chidambaram", "kotla": "Arun Jaitley", "motera": "Narendra Modi",
                  "uppal": "Rajiv Gandhi", "mohali": "Bindra", "dharamsala": "Himachal", "ekana": "Ekana",
                  "chinnaswamy": "Chinnaswamy", "wankhede": "Wankhede", "eden": "Eden Gardens",
                  "jaipur": "Sawai Mansingh", "sawai": "Sawai Mansingh", "dubai": "Dubai", "sharjah": "Sharjah",
                  "abu dhabi": "Zayed", "pune": "Maharashtra", "lucknow": "Ekana", "guwahati": "Barsapara",
                  "hyderabad": "Rajiv Gandhi", "kolkata": "Eden Gardens", "mumbai": "Wankhede",
                  "bengaluru": "Chinnaswamy", "bangalore": "Chinnaswamy", "chennai": "Chidambaram",
                  "ahmedabad": "Narendra Modi", "delhi": "Arun Jaitley"}


def db_path() -> str:
    return os.environ.get("CRICBOT_DB") or str(store.DEFAULT_DB)


def available() -> bool:
    return Path(db_path()).exists()


# ----------------------------------------------------------------- players

@dataclass
class Player:
    player_id: int
    name: str
    innings: int


@dataclass
class PlayerIndex:
    """Every player in the archive, findable by full name or by surname when one player dominates it."""

    players: list[Player] = field(default_factory=list)
    keys: dict[str, list[Player]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "PlayerIndex":
        con = store.connect(path or db_path(), read_only=True)
        try:
            tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            con.execute(queries.FRANCHISE_MACRO + queries.source_views(tables) + queries.VIEWS)
            rows = con.execute("""
                SELECT player_id, any_value(player), count(*) FROM (
                    SELECT player_id, player FROM bat_inn UNION ALL SELECT player_id, player FROM bowl_inn)
                WHERE player_id IS NOT NULL GROUP BY player_id""").fetchall()
        finally:
            con.close()
        index = cls([Player(int(pid), name, int(n)) for pid, name, n in rows if name])
        for player in index.players:
            for key in _name_keys(player.name):
                index.keys.setdefault(key, []).append(player)
        return index

    def lookup(self, name: str) -> tuple[Player | None, list[Player]]:
        """(player, []) when the name is clear, (None, candidates) when it is ambiguous or unknown."""
        text = _norm(name)
        if not text:
            return None, []
        exact = [p for p in self.players if _norm(p.name) == text]
        if len(exact) == 1:
            return exact[0], []
        tokens = text.split()
        # "Virat Kohli" against Cricsheet's "V Kohli": same surname, same first initial.
        if len(tokens) >= 2:
            surname = " ".join(tokens[1:])
            initial = [p for p in self.keys.get(surname, []) if _norm(p.name)[:1] == tokens[0][:1]]
            if len(initial) == 1:
                return initial[0], []
            if initial:
                return _dominant(initial)
            if self.keys.get(surname):                # wrong first name, clear surname: "Chris Pollard"
                return _dominant(self.keys[surname])
        candidates = self.keys.get(text, [])
        if not candidates:
            candidates = [p for p in self.players if text in _norm(p.name)]
        return _dominant(candidates) if candidates else (None, [])

    def scan(self, text: str) -> tuple[list[Player], list[str]]:
        """Players named in free text in order of appearance, and surnames that fit several players."""
        lowered = f" {_norm(text)} "
        found: list[tuple[int, Player]] = []
        ambiguous: list[str] = []
        taken: list[tuple[int, int]] = []
        for key in sorted(self.keys, key=len, reverse=True):
            if len(key) < 4 or key in COMMON_WORDS:
                continue
            for match in re.finditer(rf"(?<=[ ]){re.escape(key)}(?=[ ])", lowered):
                span = (match.start(), match.end())
                if any(a < span[1] and span[0] < b for a, b in taken):
                    continue
                player, _ = _dominant(self.keys[key])
                taken.append(span)
                if player is None:
                    ambiguous.append(key)
                elif all(player.player_id != p.player_id for _, p in found):
                    found.append((span[0], player))
        return [player for _, player in sorted(found, key=lambda item: item[0])], ambiguous

    def mentioned(self, text: str) -> list[Player]:
        return self.scan(text)[0]


# Surnames that are everyday words ("head to head" is not Travis Head).
COMMON_WORDS = {"head", "short", "white", "green", "hope", "wood", "young", "power", "little", "best", "king",
                "bell", "cook", "hill", "stone", "price", "rose", "will", "most", "more", "over", "overs", "great",
                "fine", "long", "lewis", "hunt", "jordan", "dube", "salt"}


def _norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (value or "").casefold()).split())


def _name_keys(name: str) -> set[str]:
    full = _norm(name)
    tokens = full.split()
    keys = {full}
    if len(tokens) >= 2:
        keys.add(" ".join(tokens[1:]))            # surname, or "de villiers", "du plessis"
        keys.add(tokens[-1])
    return keys


def _dominant(candidates: list[Player]) -> tuple[Player | None, list[Player]]:
    ranked = sorted(candidates, key=lambda p: -p.innings)
    if len(ranked) == 1 or ranked[0].innings >= 3 * ranked[1].innings:
        return ranked[0], []
    return None, ranked[:5]


_INDEX: dict[str, PlayerIndex] = {}


def player_index() -> PlayerIndex:
    path = db_path()
    key = f"{path}:{os.path.getmtime(path) if os.path.exists(path) else 0}"
    if key not in _INDEX:
        _INDEX.clear()
        _INDEX[key] = PlayerIndex.load(path)
    return _INDEX[key]


# ----------------------------------------------------------------- calls -> queries

class PlanError(ValueError):
    pass


def _year(args: dict[str, Any]) -> list[str]:
    value = str(args.get("year") or "").strip()
    if value.casefold() in ("", "none", "null", "all", "all time", "all-time"):
        return []
    if not YEAR_PATTERN.match(value):
        raise PlanError(f"year must look like 2016 or 2016-2018, not {value!r}")
    return ["--year", value]


def _team(value: Any) -> str:
    code = queries.Q.team(str(value or ""))
    if code not in TEAM_CODES:
        resolved, _ = _team_entities(str(value or "").casefold(), fuzzy=False)
        code = resolved[0] if resolved else code
    if code not in TEAM_CODES:
        raise PlanError(f"unknown team {value!r}")
    return code


def _int(args: dict[str, Any], key: str, low: int, high: int) -> int | None:
    if args.get(key) in (None, ""):
        return None
    try:
        return max(low, min(high, int(args[key])))
    except (TypeError, ValueError):
        raise PlanError(f"{key} must be a number") from None


def _player(name: Any, index: PlayerIndex) -> tuple[str, str]:
    player, candidates = index.lookup(str(name or ""))
    if player:
        return str(player.player_id), player.name
    if candidates:
        names = ", ".join(p.name for p in candidates)
        raise PlanError(f"'{name}' could be {names}; which one?")
    raise PlanError(f"no IPL player matching '{name}'")


def to_argv(call: dict[str, Any], index: PlayerIndex | None = None) -> list[str]:
    """Validated CLI arguments for one planned call (raises PlanError)."""
    name, args = call["name"], call.get("args") or {}
    if name == "batting_leaders" or name == "bowling_leaders":
        sorts = queries.BAT_SORTS if name == "batting_leaders" else queries.BOWL_SORTS
        sort = str(args.get("sort") or next(iter(sorts)))
        if sort not in sorts:
            raise PlanError(f"sort must be one of {', '.join(sorts)}")
        argv = ["top-batters" if name == "batting_leaders" else "top-bowlers", "--sort", sort, *_year(args),
                "--limit", str(max(10, _int(args, "limit", 1, 25) or 10))]
        if args.get("team"):
            argv += ["--team", _team(args["team"])]
        if (balls := _int(args, "min_balls", 0, 5000)) is not None:
            argv += ["--min-balls", str(balls)]
        return argv
    if name == "player_profile":
        return ["player", _player(args.get("name"), index or player_index())[0], *_year(args)]
    if name == "batter_vs_bowler":
        idx = index or player_index()
        return ["matchup", _player(args.get("batter"), idx)[0], _player(args.get("bowler"), idx)[0], *_year(args)]
    if name == "phase_leaders":
        phase, role = args.get("phase"), args.get("role") or "bat"
        if phase not in ("powerplay", "middle", "death") or role not in ("bat", "bowl"):
            raise PlanError("phase must be powerplay, middle or death; role bat or bowl")
        balls = _int(args, "min_balls", 0, 5000)
        balls = balls if balls and balls >= 30 else None          # a tiny sample is not a ranking
        return ["phase", phase, "--role", role, *_year(args), "--limit", "10",
                *(["--min-balls", str(balls)] if balls is not None else [])]
    if name == "records":
        return ["records", *_year(args), "--limit", "5"]
    if name == "head_to_head":
        return ["h2h", _team(args.get("team1")), _team(args.get("team2")), *_year(args)]
    if name == "team_seasons":
        return ["team", _team(args.get("team")), *_year(args)]
    if name == "champions":
        return ["champions", *_year(args)]
    if name == "venue_stats":
        ground = str(args.get("name") or "").strip()
        ground = GROUND_ALIASES.get(ground.casefold(), ground)
        if len(ground) > 60:
            raise PlanError("ground name too long")
        return ["venues", *([ground] if ground else []), *_year(args), "--limit", "10"]
    if name == "season_summary":
        return ["seasons", *_year(args)]
    raise PlanError(f"unknown tool {name}")


def run_calls(calls: list[dict[str, Any]], db: str | None = None, index: PlayerIndex | None = None) -> list[dict[str, Any]]:
    """Run each call; a bad call becomes a text block instead of an exception."""
    results = []
    for call in calls[:MAX_CALLS]:
        try:
            argv = to_argv(call, index)
            blocks = queries.run(argv, db=db or db_path())
            results.append({**call, "argv": argv, "blocks": blocks, "error": None})
        except PlanError as exc:
            results.append({**call, "argv": [], "blocks": [{"type": "text", "text": str(exc)}], "error": str(exc)})
    return results


# ----------------------------------------------------------------- facts and summaries

LEADING = {"player", "team", "ground"}


def facts_text(results: list[dict[str, Any]], max_rows: int = 10) -> str:
    """Tables as compact text. Leaderboards get ranks so 'first' and 'second' are grounded."""
    lines: list[str] = []
    for result in results:
        for block in result["blocks"]:
            if block["type"] == "text":
                lines.append(f"NOTE: {block['text']}")
                continue
            lines.append(f"TABLE: {block['title']}")
            rows = block["rows"]
            if not rows:
                lines.append("(no rows)")
                continue
            columns = block["columns"]
            ranked = bool(columns) and columns[0] in LEADING
            lines.append(("rank | " if ranked else "") + " | ".join(columns))
            limit = max_rows if ranked else 30
            for i, row in enumerate(rows[:limit], 1):
                cells = ["-" if v is None else str(v) for v in row]
                lines.append((f"{i} | " if ranked else "") + " | ".join(cells))
            if len(rows) > limit:
                lines.append(f"... {len(rows) - limit} more rows")
        lines.append("")
    return "\n".join(lines).strip()


def _key_column(result: dict[str, Any]) -> str | None:
    args = result.get("args") or {}
    if result["name"] == "batting_leaders":
        return BAT_KEY.get(args.get("sort") or "runs")
    if result["name"] == "bowling_leaders":
        return BOWL_KEY.get(args.get("sort") or "wkts")
    if result["name"] == "phase_leaders":
        return "econ" if args.get("role") == "bowl" else "sr"
    if result["name"] == "champions":
        return "champion"
    return None


LABELS = {"6s": "sixes", "4s": "fours", "sr": "strike rate", "econ": "economy", "avg": "average", "wkts": "wickets",
          "inns": "innings", "hs": "highest score", "4w+": "four-wicket hauls", "50s": "fifties", "100s": "hundreds",
          "avg_1st_inns": "average first innings", "bat_first_won": "won batting first", "chasing_won": "won chasing",
          "highest_total": "highest total", "run_rate": "run rate", "balls_to_50": "balls to fifty",
          "with_balls": "with ball data", "no_result": "no result", "win_pct": "win %", "dot_pct": "dot-ball %"}
# Question words -> the column they ask about.
ASKS = [("average first", "avg_1st_inns"), ("first innings", "avg_1st_inns"), ("first-innings", "avg_1st_inns"),
        ("chasing", "chasing_won"), ("chase", "chasing_won"), ("batting first", "bat_first_won"),
        ("bat first", "bat_first_won"), ("highest total", "highest_total"), ("highest ipl total", "highest_total"),
        ("run rate", "run_rate"), ("sixes", "sixes"), ("six", "sixes"), ("strike rate", "sr"), ("economy", "econ"),
        ("economical", "econ"), ("wickets", "wkts"), ("runs", "runs"), ("fours", "4s")]


def _label(column: str) -> str:
    return LABELS.get(column, column.replace("_", " "))


def _pick_block(blocks: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    tables = [b for b in blocks if b["type"] == "table"]
    if len(tables) <= 1:
        return tables[0] if tables else None
    words = set(re.findall(r"[a-z]+", question.casefold())) - {"the", "in", "of", "a", "ipl", "most", "what", "who", "is"}
    synonyms = {"figures": {"bowling", "figures"}, "fifty": {"fifties", "fastest"}, "individual": {"individual", "score"},
                "innings": {"innings"}, "total": {"totals", "team"}}
    def score(block: dict[str, Any]) -> int:
        title = set(re.findall(r"[a-z]+", block["title"].casefold()))
        extra = sum(len(syn & title) for key, syn in synonyms.items() if key in words)
        return len(words & title) + extra
    return max(tables, key=score)


def _describe(cols: list[str], row: list[Any], skip: int = 0) -> str:
    return ", ".join(f"{_label(c)} {v}" for c, v in list(zip(cols, row))[skip:8] if v is not None and c != "match_id")


def summary(results: list[dict[str, Any]], question: str = "") -> str:
    """A grounded answer straight from the tables (used when no LLM answers or its numbers do not check out)."""
    text = question.casefold()
    asked = next((col for phrase, col in ASKS if phrase in text), None)
    years = re.findall(r"\b(20\d\d)\b", text)
    parts = []
    for result in results[:3]:
        notes = [b["text"] for b in result["blocks"] if b["type"] == "text"]
        block = _pick_block(result["blocks"], question)
        if block is None:
            parts.extend(notes[:1])
            continue
        rows, cols = block["rows"], block["columns"]
        if not rows:
            parts.append(f"{block['title']}: no rows.")
            continue
        if result["name"] == "champions":
            finals = next(b for b in result["blocks"] if b["type"] == "table" and b["title"].startswith("IPL finals"))
            rows, cols = finals["rows"], finals["columns"]
            if not rows:
                parts.append(f"{finals['title']}: no final in the archive.")
            elif len(rows) == 1:
                row = dict(zip(cols, rows[0]))
                parts.append(f"{row['champion']} won IPL {row['year']}, beating {row['runner_up']} "
                             f"(runners-up) in the final: {row['result']}.")
            else:
                titles = next((b for b in result["blocks"] if b["type"] == "table" and b["title"].startswith("Titles")), None)
                top = ", ".join(f"{r[0]} {r[1]}" for r in (titles or {}).get("rows", [])[:5])
                parts.append(f"IPL titles by team: {top}.")
            continue
        if cols[0] == "year" and str(rows[-1][0]) == "Total" and not years:
            parts.append(f"{block['title']} — in all: {_describe(cols, rows[-1], 1)}.")
            continue
        if cols[0] == "year":
            chosen = next((r for r in rows if str(r[0]) in years), None)
            column = asked if asked in cols else (asked == "sixes" and "6s" in cols and "6s") or None
            if chosen is None and column:
                numeric = [r for r in rows if isinstance(r[cols.index(column)], (int, float))]
                lowest = column == "econ" or "lowest" in text
                chosen = (min if lowest else max)(numeric, key=lambda r: r[cols.index(column)]) if numeric else None
            chosen = chosen or rows[-1]
            parts.append(f"{block['title']}: {chosen[0]} — {_describe(cols, chosen, 1)}.")
            continue
        if len(rows) == 1 or cols[0] == "ground":
            parts.append(f"{block['title']}: {rows[0][0]} — {_describe(cols, rows[0], 1)}.")
            continue
        key = _key_column(result)
        asked_col = "6s" if asked == "sixes" and "6s" in cols else (asked if asked in cols else None)
        record_col = next((c for c in ("score", "figures", "balls_to_50", "6s") if c in cols), None)
        column = key if key in cols else (asked_col or record_col or cols[1])
        top = [f"{r[0]} ({_label(column)} {r[cols.index(column)]})" for r in rows[:3]]
        parts.append(f"{block['title']}: " + ", then ".join(top) + ".")
    return " ".join(parts) or "The archive has no rows for that question."


def card(results: list[dict[str, Any]], question: str) -> dict[str, Any]:
    """Chat card: every table from every call, rendered by the UI like the Stats view."""
    blocks = [block for result in results for block in result["blocks"]]
    return {
        "type": "stats",
        "title": next((b["title"] for b in blocks if b["type"] == "table"), "IPL stats"),
        "question": question,
        "calls": [{"name": r["name"], "args": r.get("args") or {}, "command": " ".join(r["argv"]), "error": r["error"]}
                  for r in results],
        "blocks": blocks,
    }


# ----------------------------------------------------------------- planning

def planner_prompt(today: date | None = None) -> str:
    today = today or date.today()
    return f"""Route IPL questions to tools; call tools only, never answer in text. Today: {today.isoformat()}.
Archive: every IPL match 2008 onwards, ball by ball.
- ONE specific match (a final, a named game, its scorecard/momentum/commentary) -> {MATCH_ROUTE}.
- Otherwise 1-{MAX_CALLS} stats calls. Compare two players: one call each.
- year: 2016, 2016-2018, or omit for all time. This season = {today.year}, last season = {today.year - 1}.
- Teams: CSK MI RCB KKR SRH RR PBKS(Kings XI Punjab) DC(Delhi Daredevils) GT LSG DCH(Deccan) GL RPS PWI KTK.
- Use full player names; a bare surname that fits several players ("Sharma", "Patel") -> pass it as written.
Examples:
best death bowlers 2024 -> phase_leaders(phase=death, role=bowl, year=2024)
compare Dhoni and Pant -> player_profile(MS Dhoni), player_profile(Rishabh Pant)
Kohli against Bumrah -> batter_vs_bowler(Virat Kohli, Jasprit Bumrah)
who won IPL 2019 -> champions(year=2019) | how many titles has CSK won -> team_seasons(team=CSK)
Bumrah's wickets -> player_profile(Jasprit Bumrah) | sixes hit in IPL 2024 -> season_summary(year=2024)
highest total at the Kotla -> venue_stats(name=Kotla) | most runs 2008 to 2025 -> batting_leaders(year=2008-2025)
Orange Cap = batting_leaders sort runs; Purple Cap = bowling_leaders sort wkts."""


def llm_plan(router: Any, question: str, history: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], str, str]:
    """(calls, provider, model). An empty list means the match path. Raises RuntimeError if no LLM answers."""
    context = [
        {"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("content", ""))[:300]}
        for item in (history or [])[-4:]
    ]
    messages = [{"role": "system", "content": planner_prompt()}, *context, {"role": "user", "content": question}]
    calls, provider, model = router.plan(messages, TOOLS)
    calls = [c for c in calls if c["name"] in TOOL_NAMES]
    if not calls or any(c["name"] == MATCH_ROUTE for c in calls):
        return [], provider, model
    return calls[:MAX_CALLS], provider, model


def plan(question: str, entities: dict[str, Any], router: Any = None, history: list[dict[str, str]] | None = None,
         index: PlayerIndex | None = None, mode: str = "hybrid") -> tuple[list[dict[str, Any]], str, str]:
    """(calls, planner, note). mode: hybrid (LLM, keyword rules as fallback), llm, or rules.
    An empty call list means the match path."""
    if ON_SCREEN.search(question.casefold()):
        return [], "rules", "The question is about the match on screen."
    calls, planner, note = [], "rules", ""
    if mode in ("hybrid", "llm") and router is not None:
        try:
            calls, provider, model = llm_plan(router, question, history)
            planner = f"{provider} ({model})"
            calls, dropped = ground_calls(calls, question, entities, history)
            if dropped:
                note = f"Dropped LLM calls not grounded in the question: {'; '.join(dropped)}."
        except RuntimeError as exc:
            note = f"LLM planner unavailable ({str(exc)[:100]})."
    if mode in ("hybrid", "rules"):
        rule_calls = rule_plan(question, entities, index)
        if rule_calls and prefer_rules(calls, rule_calls):
            planner = "rules" if not calls else f"rules (over {planner})"
            calls = rule_calls
    return calls, planner, note


def ground_calls(calls: list[dict[str, Any]], question: str, entities: dict[str, Any],
                 history: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only what the question supports: a player the planner names must be named in the question
    (or the recent conversation), and a year must be one the question states. The same idea as the
    numeric grounding check, applied to the plan."""
    context = _norm(" ".join([question, *(str(h.get("content", "")) for h in (history or [])[-4:]
                                          if h.get("role") != "assistant")]))
    years = sorted(entities.get("years") or [])
    kept, dropped = [], []
    for call in calls:
        args = dict(call.get("args") or {})
        names = [str(args[k]) for k in ("name", "batter", "bowler") if args.get(k)]
        missing = [n for n in names if _norm(n).split() and _norm(n).split()[-1] not in context.split()]
        if missing:
            dropped.append(f"{call['name']}({', '.join(missing)})")
            continue
        year = str(args.get("year") or "").strip()
        if year and YEAR_PATTERN.match(year):
            low, high = (int(p) for p in (year.split("-") + [year])[:2])
            if years and (low < years[0] or high > years[-1]):
                args["year"] = _year_arg(years)["year"]   # the question's own year wins
            elif not years and not TIME_WORDS.search(question.casefold()):
                args.pop("year")                      # the question never mentions a time
        kept.append({**call, "args": args})
    return kept, dropped


def prefer_rules(llm_calls: list[dict[str, Any]], rule_calls: list[dict[str, Any]]) -> bool:
    """Use the keyword plan when the LLM chose nothing, or chose the same tool for fewer subjects
    (e.g. one player profile when the question compares two)."""
    if not llm_calls:
        return True
    player_tools = ("player_profile", "batter_vs_bowler")    # venue_stats also has a "name": a ground, not a player
    named = {str(v).casefold() for c in rule_calls if c["name"] in player_tools
             for k, v in (c.get("args") or {}).items() if k in ("name", "batter", "bowler")}
    looked_at = {str(v).casefold() for c in llm_calls if c["name"] in player_tools
                 for k, v in (c.get("args") or {}).items() if k in ("name", "batter", "bowler")}
    if named and not looked_at:
        return True                                   # the question names a player the LLM plan never looks at
    same_tool = {c["name"] for c in llm_calls} == {c["name"] for c in rule_calls}
    return same_tool and len(rule_calls) > len(llm_calls)


BAT_WORDS = {"sixes": "sixes", "six": "sixes", "fours": "fours", "boundaries": "fours", "fifties": "fifties",
             "half centuries": "fifties", "half-centuries": "fifties", "hundreds": "hundreds", "centuries": "hundreds",
             "tons": "hundreds", "strike rate": "sr", "average": "avg", "highest score": "hs", "runs": "runs",
             "orange cap": "runs", "run scorer": "runs", "run-scorer": "runs", "top scorer": "runs"}
BOWL_WORDS = {"economy": "econ", "economical": "econ", "purple cap": "wkts", "wickets": "wkts",
              "wicket taker": "wkts", "wicket-taker": "wkts", "four-wicket": "4w", "4 wicket": "4w"}
PHASES = {"powerplay": "powerplay", "power play": "powerplay", "middle overs": "middle", "death": "death",
          "death overs": "death", "slog": "death"}
LEADER_WORDS = ("most", "top", "highest", "best", "leading", "lowest", "who has", "who hit", "who scored",
                "who took", "orange cap", "purple cap", "leaderboard", "fewest")
MATCH_WORDS = ("final", "qualifier", "eliminator", "match on", "scorecard", "ball by ball", "ball-by-ball",
               "commentary", "momentum", "this match", "that match", "the match")


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def _year_arg(years: list[int]) -> dict[str, str]:
    if not years:
        return {}
    return {"year": str(years[0]) if len(years) == 1 else f"{min(years)}-{max(years)}"}


def rule_plan(question: str, entities: dict[str, Any], index: PlayerIndex | None) -> list[dict[str, Any]]:
    """Keyword planner for when no LLM is available. Returns [] for the match path."""
    text = question.casefold().replace("-", " ")
    teams, _ = _team_entities(text, fuzzy=False)       # only teams named in this message
    year = _year_arg(entities.get("years") or [])
    players, ambiguous = index.scan(question) if index else ([], [])
    leader = _has(text, *LEADER_WORDS)

    if _has(text, "who won the ipl", "won the ipl", "ipl champion", "champions", "titles", "title", "trophy", "trophies",
            "runner up", "runners up") or re.search(r"who won (the )?ipl", text):
        if teams and _has(text, "how many", "titles", "trophies"):
            return [{"name": "team_seasons", "args": {"team": teams[0], **year}}]
        return [{"name": "champions", "args": year}]
    # One match: a final, a scorecard, a match on screen.
    if _has(text, *MATCH_WORDS) and not leader:
        return []
    if len(players) >= 2 and _has(text, "against", "vs", "v", "versus", "facing", "faced"):
        return [{"name": "batter_vs_bowler", "args": {"batter": players[0].name, "bowler": players[1].name, **year}}]
    if len(players) >= 2 and _has(text, "compare", "better", "comparison"):
        return [{"name": "player_profile", "args": {"name": p.name, **year}} for p in players[:2]]
    phase = next((PHASES[w] for w in sorted(PHASES, key=len, reverse=True) if _has(text, w)), None)
    if phase and (leader or _has(text, "bowler", "bowlers", "batter", "batters", "batsmen", "economy", "strike rate")):
        role = "bowl" if _has(text, "bowler", "bowlers", "bowling", "economy", "economical", "wickets") else "bat"
        return [{"name": "phase_leaders", "args": {"phase": phase, "role": role, **year}}]
    if leader:
        bowl = next((BOWL_WORDS[w] for w in sorted(BOWL_WORDS, key=len, reverse=True) if _has(text, w)), None)
        bat = next((BAT_WORDS[w] for w in sorted(BAT_WORDS, key=len, reverse=True) if _has(text, w)), None)
        if _has(text, "highest total", "highest team total", "biggest total", "fastest fifty", "fastest 50",
                "best bowling figures", "best figures", "highest individual", "records", "in an innings",
                "single innings", "in a single", "in one innings"):
            return [{"name": "records", "args": year}]
        team = {"team": teams[0]} if len(teams) == 1 else {}
        if bowl and not (bat and bat != "runs" and not _has(text, "bowler", "bowlers")):
            return [{"name": "bowling_leaders", "args": {"sort": bowl, **team, **year}}]
        if bat:
            return [{"name": "batting_leaders", "args": {"sort": bat, **team, **year}}]
    if len(teams) == 2 and _has(text, "head to head", "head-to-head", "h2h", "record", "how many times",
                                "rivalry", "won more", "against"):
        return [{"name": "head_to_head", "args": {"team1": teams[0], "team2": teams[1], **year}}]
    if _has(text, "stadium", "ground", "venue", "pitch at", "chasing at", "batting first at", *GROUND_ALIASES) \
            and _has(text, "average", "score", "chasing", "chase", "bat first", "batting first", "total", "stats", "record"):
        ground = next((g for g in sorted(GROUND_ALIASES, key=len, reverse=True) if _has(text, g)), "")
        return [{"name": "venue_stats", "args": {"name": GROUND_ALIASES.get(ground, ground), **year}}]
    player_words = ("stats", "record", "career", "how many", "runs", "wickets", "perform", "performed", "season",
                    "seasons", "strike rate", "economy", "average", "profile", "bowling", "batting", "numbers",
                    "how did", "do in", "fared", "form")
    if len(players) == 1 and _has(text, *player_words):
        return [{"name": "player_profile", "args": {"name": players[0].name, **year}}]
    if not players and ambiguous and not teams and _has(text, *player_words):
        return [{"name": "player_profile", "args": {"name": ambiguous[0], **year}}]   # the answer asks which one
    if len(teams) == 1 and not players and _has(text, "season", "seasons", "record", "how did", "how many wins",
                                                 "win percentage", "playoffs", "finish", "finished", "win", "won",
                                                 "wins", "win loss", "matches"):
        return [{"name": "team_seasons", "args": {"team": teams[0], **year}}]
    if not teams and not players and (
            _has(text, "run rate", "scoring trend", "average first innings", "per season", "each season",
                 "which season", "which ipl season") or re.search(r"how many (sixes|fours|runs) were", text)):
        return [{"name": "season_summary", "args": year}]
    return []
