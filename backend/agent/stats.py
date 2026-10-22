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
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from backend.db import queries, store

from .entities import TEAM_ALIASES, _team_entities
from . import slices

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
    _fn("ground_report", "One ground's character against the league: par score by season, phase scoring, how "
        "wickets fall, chasing (pitch / conditions questions).",
        {"name": {"type": "string", "description": "ground or nickname"}, "year": YEAR_PARAM}, ["name"]),
    _fn("over_range_leaders", "Leaders for ANY over range and innings (over 1; overs 12-20; chasing = innings 2, "
        "batting first = innings 1).", {
            "from_over": {"type": "integer"}, "to_over": {"type": "integer"},
            "innings": {"type": "integer", "enum": [1, 2]}, "role": {"type": "string", "enum": ["bat", "bowl"]},
            "sort": {"type": "string", "enum": ["sr", "runs", "econ", "wkts"]}, "year": YEAR_PARAM}, ["from_over", "role"]),
    _fn("match_results", "Facts across all matches: close wins (close_runs, close_wickets), targets defended "
        "(defended_under: totals below value), chased (chased_over), runs still needed at an over in won chases "
        "(needed_at_over), ties, and innings totals of value+ by team (scored_over / conceded_over; innings 1 = "
        "batting or bowling first). stage limits to finals or playoffs.", {
            "kind": {"type": "string", "enum": ["close_runs", "close_wickets", "defended_under", "chased_over", "needed_at_over", "tied", "scored_over", "conceded_over"]},
            "value": {"type": "integer"}, "over": {"type": "integer"}, "innings": {"type": "integer", "enum": [1, 2]},
            "stage": {"type": "string", "enum": ["final", "playoff"]}, "year": YEAR_PARAM}, ["kind"]),
    _fn("milestones", "Fewest innings (or balls) to reach N runs or wickets.", {
            "stat": {"type": "string", "enum": ["runs", "wickets", "fifties", "hundreds", "sixes", "fours", "ducks"]},
            "value": {"type": "integer"},
            "by": {"type": "string", "enum": ["innings", "balls"]}}, ["stat", "value"]),
    _fn("player_match", "A player's Nth IPL match and what they did in it.",
        {"name": PLAYER_PARAM, "nth": {"type": "integer"}}, ["name", "nth"]),
    _fn("season_summary", "Per season: matches, run rate, average first innings, highest total, total sixes.",
        {"year": YEAR_PARAM}),
]
TOOLS += slices.SLICE_TOOLS
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
    bat: int = 0                                      # batting innings
    bowl: int = 0                                     # bowling innings

    @property
    def bat_share(self) -> float:
        """How much of a batter this player is: 1.0 never bowls, 0.0 never bats."""
        return self.bat / (self.bat + self.bowl) if self.bat + self.bowl else 0.5


@dataclass
class PlayerIndex:
    """Every player in the archive, findable by full name or by surname when one player dominates it."""

    players: list[Player] = field(default_factory=list)
    keys: dict[str, list[Player]] = field(default_factory=dict)
    first: dict[str, list[Player]] = field(default_factory=dict)   # "rashid" -> Rashid Khan, "rohit" -> ...

    @classmethod
    def load(cls, path: str | None = None) -> "PlayerIndex":
        con = store.connect(path or db_path(), read_only=True)
        try:
            tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            con.execute(queries.FRANCHISE_MACRO + queries.source_views(tables) + queries.VIEWS)
            rows = con.execute("""
                SELECT player_id, any_value(player), count(*), count(*) FILTER (WHERE r = 'bat'),
                       count(*) FILTER (WHERE r = 'bowl') FROM (
                    SELECT player_id, player, 'bat' AS r FROM bat_inn
                    UNION ALL SELECT player_id, player, 'bowl' AS r FROM bowl_inn)
                WHERE player_id IS NOT NULL GROUP BY player_id""").fetchall()
        finally:
            con.close()
        return cls.build([Player(int(pid), name, int(n), int(bat), int(bowl)) for pid, name, n, bat, bowl in rows if name])

    @classmethod
    def build(cls, players: list[Player]) -> "PlayerIndex":
        index = cls(players)
        for player in index.players:
            for key in _name_keys(player.name):
                index.keys.setdefault(key, []).append(player)
            tokens = _norm(player.name).split()
            # Full first names only ("Rashid Khan" -> rashid), never initials ("MS Dhoni" -> ms).
            if len(tokens) >= 2 and len(tokens[0]) >= 4 and tokens[0] not in FIRST_NAME_STOP:
                index.first.setdefault(tokens[0], []).append(player)
        return index

    def _candidates(self, key: str) -> list[Player]:
        """Surname matches plus first-name matches: "rashid" is AU Rashid (3 innings) or Rashid Khan."""
        seen = {p.player_id: p for p in [*self.keys.get(key, []), *self.first.get(key, [])]}
        return list(seen.values())

    def _tables(self) -> dict[str, Any]:
        """Lookup tables built once per index (and rebuilt if keys change, as tests do by hand).

        scan() used to try every name key (~2,500) as its own regex on every call, which overflowed
        Python's regex cache and recompiled them all: about 450 ms per question. Looking up the
        question's own 1-4 word runs in a dict gives the same matches in well under a millisecond."""
        stamp = (len(self.players), len(self.keys), len(self.first))
        cached = getattr(self, "_cache", None)
        if cached and cached["stamp"] == stamp:
            return cached
        norms = {p.player_id: _norm(p.name) for p in self.players}
        by_norm: dict[str, list[Player]] = {}
        for p in self.players:
            by_norm.setdefault(norms[p.player_id], []).append(p)
        nicknames = {key: by_norm.get(_norm(full), []) for key, full in NICKNAMES.items()}
        keys = {**{k: v for k, v in self.first.items() if k not in self.keys}, **self.keys,
                **{k: v for k, v in nicknames.items() if v}}
        # Same order and filters the regex version used: longest key first, short and everyday words skipped.
        order = sorted(keys, key=len, reverse=True)
        scan_keys = {}
        for rank, key in enumerate(order):
            if (len(key) < 4 and key not in NICKNAMES) or key in COMMON_WORDS:
                continue
            player, _ = _dominant(keys[key] if key in nicknames else self._candidates(key))
            scan_keys[key] = (rank, player, key in self.keys)
        self._cache = {"stamp": stamp, "norms": norms, "by_norm": by_norm, "scan": scan_keys,
                       "max_words": max((k.count(" ") + 1 for k in scan_keys), default=1), "memo": {}}
        return self._cache

    def lookup(self, name: str) -> tuple[Player | None, list[Player]]:
        """(player, []) when the name is clear, (None, candidates) when it is ambiguous or unknown."""
        text = _norm(name)
        if not text:
            return None, []
        tables = self._tables()
        norms = tables["norms"]
        text = _norm(NICKNAMES.get(text, text))
        exact = tables["by_norm"].get(text, [])
        if len(exact) == 1:
            return exact[0], []
        tokens = text.split()
        # "Virat Kohli" against Cricsheet's "V Kohli": same surname, same first initial.
        if len(tokens) >= 2:
            surname = " ".join(tokens[1:])
            initial = [p for p in self.keys.get(surname, []) if norms[p.player_id][:1] == tokens[0][:1]]
            if len(initial) == 1:
                return initial[0], []
            if initial:
                return _dominant(initial)
            if self.keys.get(surname):                # wrong first name, clear surname: "Chris Pollard"
                return _dominant(self.keys[surname])
        candidates = self._candidates(text)
        if not candidates:
            candidates = [p for p in self.players if text in norms[p.player_id]]
        return _dominant(candidates) if candidates else (None, [])

    def scan(self, text: str) -> tuple[list[Player], list[str]]:
        """Players named in free text in order of appearance, and surnames that fit several players."""
        tables = self._tables()
        memo = tables["memo"]
        if text in memo:                              # planner, grounding and matchup checks scan the same text
            found_players, ambiguous_keys = memo[text]
            return list(found_players), list(ambiguous_keys)
        tokens = _norm(text).split()
        starts, pos = [], 1                           # character offsets in " " + normalized + " "
        for token in tokens:
            starts.append(pos)
            pos += len(token) + 1
        scan_keys = tables["scan"]
        hits = []
        for i in range(len(tokens)):
            for n in range(1, min(tables["max_words"], len(tokens) - i) + 1):
                key = " ".join(tokens[i:i + n])
                if key in scan_keys:
                    rank, player, shared_surname = scan_keys[key]
                    hits.append((rank, starts[i], starts[i] + len(key), key, player, shared_surname))
        hits.sort(key=lambda h: (h[0], h[1]))         # longest key first, then left to right
        found: list[tuple[int, Player]] = []
        ambiguous: list[str] = []
        taken: list[tuple[int, int]] = []
        for _, a, b, key, player, shared_surname in hits:
            if any(x < b and a < y for x, y in taken):
                continue
            taken.append((a, b))
            if player is None:
                if shared_surname:                    # a shared surname asks; a shared first name ("Chris") is skipped
                    ambiguous.append(key)
            elif all(player.player_id != p.player_id for _, p in found):
                found.append((a, player))
        result = ([player for _, player in sorted(found, key=lambda item: item[0])], ambiguous)
        if len(memo) > 512:
            memo.clear()
        memo[text] = result
        return list(result[0]), list(result[1])

    def mentioned(self, text: str) -> list[Player]:
        return self.scan(text)[0]


# Surnames that are everyday words ("head to head" is not Travis Head).
COMMON_WORDS = {"head", "short", "white", "green", "hope", "wood", "young", "power", "little", "best", "king",
                "bell", "cook", "hill", "stone", "price", "rose", "will", "most", "more", "over", "overs", "great",
                "fine", "long", "lewis", "hunt", "jordan", "dube", "salt"}


# First names that are also place or team words ("Arun Jaitley Stadium", "Royal Challengers").
FIRST_NAME_STOP = {"arun", "rajiv", "narendra", "sawai", "feroz", "sardar", "subrata", "royal", "super", "knight",
                   "kings", "test", "world", "mark", "will", "jack", "most", "best", "rich", "long", "young", "glenn"}
# Names fans use. Each maps to the archive's full name.
NICKNAMES = {"sky": "Suryakumar Yadav", "bhuvi": "Bhuvneshwar Kumar", "abd": "AB de Villiers", "mahi": "MS Dhoni",
             "thala": "MS Dhoni", "jaddu": "Ravindra Jadeja", "hitman": "Rohit Sharma", "gabbar": "Shikhar Dhawan",
             "universe boss": "Chris Gayle", "dre russ": "Andre Russell", "king kohli": "Virat Kohli",
             "boom boom": "Jasprit Bumrah", "chahal": "Yuzvendra Chahal"}


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


class UnknownPlayer(PlanError):
    pass


class AmbiguousName(PlanError):
    def __init__(self, name: str, candidates: list[Player]):
        self.name, self.candidates = name, candidates
        super().__init__(f"'{name}' could be {', '.join(p.name for p in candidates)}; which one?")


def _year(args: dict[str, Any]) -> list[str]:
    value = str(args.get("year") or "").strip()
    if value.casefold() in ("", "none", "null", "all", "all time", "all-time"):
        return []
    if not YEAR_PATTERN.match(value):
        raise PlanError(f"year must look like 2016 or 2016-2018, not {value!r}")
    return ["--year", value]


def _team(value: Any) -> str:
    code = queries.Q.team(str(value or ""))
    code = TEAM_NAMES.get(str(value or "").casefold().strip(), code)
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
        raise AmbiguousName(str(name), candidates)
    raise UnknownPlayer(f"no IPL player matching '{name}'")


def to_argv(call: dict[str, Any], index: PlayerIndex | None = None) -> list[str]:
    """Validated CLI arguments for one planned call (raises PlanError)."""
    name, args = call["name"], call.get("args") or {}
    if name in slices.SLICE_TOOL_NAMES:
        return slices.to_argv(name, args, index)
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
    if name == "over_range_leaders":
        lo = _int(args, "from_over", 1, 20) or 1
        hi = _int(args, "to_over", lo, 20) or lo
        role = args.get("role") if args.get("role") in ("bat", "bowl") else "bat"
        sort = args.get("sort") if args.get("sort") in (("sr", "runs") if role == "bat" else ("econ", "wkts")) else None
        innings = _int(args, "innings", 1, 2)
        balls = _int(args, "min_balls", 0, 5000)
        if balls is None:                             # a fair sample for the width of the range
            span = hi - lo + 1
            balls = 60 if span <= 2 else 150 if span <= 6 else 300
        return ["overs", "--from", str(lo), "--to", str(hi), "--role", role, *(["--sort", sort] if sort else []),
                *(["--innings", str(innings)] if innings else []), *_year(args), "--min-balls", str(balls), "--limit", "10"]
    if name == "match_results":
        kind = args.get("kind")
        defaults = {"close_runs": 5, "close_wickets": 2, "defended_under": 130, "chased_over": 200,
                    "needed_at_over": 0, "tied": 0, "scored_over": 200, "conceded_over": 200}
        if kind not in defaults:
            raise PlanError("kind must be one of " + ", ".join(defaults))
        value = _int(args, "value", 0, 400) or defaults[kind]
        over = _int(args, "over", 2, 20)
        innings = args.get("innings") if args.get("innings") in (1, 2) and kind in ("scored_over", "conceded_over") else None
        stage = args.get("stage") if args.get("stage") in ("final", "playoff") else None
        return ["results", kind, "--value", str(value), *(["--over", str(over)] if over else []),
                *(["--innings", str(innings)] if innings else []), *(["--stage", stage] if stage else []),
                *_year(args), "--limit", "10"]
    if name == "milestones":
        stat = args.get("stat") if args.get("stat") in ("runs", "wickets", "fifties", "hundreds", "sixes", "fours", "ducks") else "runs"
        value = _int(args, "value", 1, 20000)
        if not value:
            raise PlanError("milestones needs a value, e.g. 2000 runs")
        by = "balls" if args.get("by") == "balls" else "innings"
        return ["milestone", stat, str(value), "--by", by, "--limit", "10"]
    if name == "player_match":
        nth = _int(args, "nth", 1, 500)
        if not nth:
            raise PlanError("player_match needs nth, e.g. 100")
        return ["player-match", _player(args.get("name"), index or player_index())[0], str(nth)]
    if name == "ground_report":
        ground = str(args.get("name") or "").strip()
        ground = GROUND_ALIASES.get(ground.casefold(), ground)
        if not ground or len(ground) > 60:
            raise PlanError("ground_report needs a ground name")
        return ["ground", ground, *_year(args)]
    raise PlanError(f"unknown tool {name}")


def run_calls(calls: list[dict[str, Any]], db: str | None = None, index: PlayerIndex | None = None) -> list[dict[str, Any]]:
    """Run each call; a bad call becomes a text block instead of an exception."""
    results = []
    for call in calls[:MAX_CALLS]:
        try:
            argv = to_argv(call, index)
            blocks = queries.run(argv, db=db or db_path())
            if call.get("definition"):                 # an evaluative word: say what it was taken to mean
                blocks = [{"type": "text", "text": f"Definition: {call['definition']}"}, *blocks]
            results.append({**call, "argv": argv, "blocks": blocks, "error": None})
        except PlanError as exc:
            choices = [p.name for p in exc.candidates] if isinstance(exc, AmbiguousName) else []
            results.append({**call, "argv": [], "blocks": [{"type": "text", "text": str(exc)}], "error": str(exc),
                            **({"ambiguous": exc.name, "choices": choices} if choices else {})})
    return results


def clarify_card(results: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    """Choices for an ambiguous name, each with the question re-asked about that player."""
    result = next((r for r in results if r.get("choices")), None)
    if result is None:
        return None
    name = result["ambiguous"]
    pattern = re.compile(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", re.IGNORECASE)
    options = [{"label": full, "question": pattern.sub(full, question, count=1) if pattern.search(question)
                else f"{question} ({full})"} for full in result["choices"]]
    return {"type": "clarify", "title": f"Which {name.strip().title()}?", "name": name, "options": options}


# ----------------------------------------------------------------- follow-ups

FOLLOW_UP_START = re.compile(r"^\s*(and|what about|how about|same for|also|then|ok,? and)\b", re.IGNORECASE)
PRONOUN = re.compile(r"\b(he|him|his|she|her|they|them|their)\b", re.IGNORECASE)


def contextualize(question: str, history: list[dict[str, str]] | None, index: PlayerIndex | None = None) -> str:
    """Rewrite a follow-up as a standalone question, using the conversation so far.

        "How many runs did Virat Kohli score in IPL 2016?" + "And in 2017?"
            -> "How many runs did Virat Kohli score in IPL 2017?"
        "Tell me about MS Dhoni's IPL batting" + "What's his strike rate in the death overs?"
            -> "What's MS Dhoni's strike rate in the death overs?"

    The same job LangChain's history-aware retriever gives an LLM, done with rules so it also works
    without one and can be tested. A question that already stands alone is returned unchanged."""
    users = [str(h.get("content", "")) for h in (history or []) if h.get("role") != "assistant"]
    if not users or not question.strip():
        return question
    index = index or player_index()
    text = question.strip()
    players = index.scan(text)[0]
    teams = _teams(text.casefold())
    years = re.findall(r"\b(?:19|20)\d\d\b", text)

    # 1. A pronoun with no player named: "his", "him", "he" -> the last player the conversation named.
    if PRONOUN.search(text) and not players:
        named = next((found for turn in reversed([*users, *(str(h.get("content", "")) for h in history or []
                                                            if h.get("role") == "assistant")])
                      if (found := index.scan(turn)[0])), [])
        if named:
            name = named[0].name
            return PRONOUN.sub(lambda m: f"{name}'s" if m.group(1).lower() in ("his", "her", "their") else name,
                               text, count=1)
        return question

    # 2. An elliptical follow-up ("And in 2017?", "What about Bumrah?"): the previous question with the new
    #    year, player or team swapped in. Long questions that name their own subject stand alone.
    rest = re.sub(r"\b(?:19|20)\d\d\b", " ", _norm(text))
    for player in players:
        for key in _name_keys(player.name):
            rest = re.sub(rf"\b{re.escape(key)}\b", " ", rest)
    rest = {w for w in rest.split() if w not in ("in", "for", "the", "ipl", "season", "s", *TEAM_NAMES, *map(str.lower, teams))}
    elliptical = FOLLOW_UP_START.search(text) or not rest   # "2017?", "Bumrah?": only a new subject or year
    if not elliptical or not (years or players or teams):
        return question
    base = users[-1]
    out = base
    if years:
        old = re.findall(r"\b(?:19|20)\d\d\b", base)
        if old:
            out = re.sub(r"\b(?:19|20)\d\d\b(?:\s*(?:-|to)\s*(?:19|20)\d\d\b)?", "-".join(years[:2]), out, count=1)
        else:
            out = f"{out.rstrip(' ?.')} in {'-'.join(years[:2])}?"
    if players:
        prev = index.scan(base)[0]
        if prev:
            for key in sorted(_name_keys(prev[0].name), key=len, reverse=True):
                pattern = re.compile(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", re.IGNORECASE)
                if pattern.search(out):
                    out = pattern.sub(players[0].name, out, count=1)
                    break
    if teams:
        prev_teams = _teams(base.casefold())
        if prev_teams:
            out = re.sub(rf"\b{re.escape(prev_teams[0])}\b", teams[0], out, count=1, flags=re.IGNORECASE)
    return out if out != base else question


# ----------------------------------------------------------------- gaps

# Questions the archive cannot answer yet. Each says what is missing, so the reply can say so instead of
# answering a nearby question ("overs 12-20" is not "the death overs").
UNSUPPORTED = [
    (r"(man|player) of the (series|tournament)|most valuable player|\bmvp\b", "player of the series (only player of the match is recorded)"),
    (r"\bimpact (points|score|index|rating)s?\b|\bsmart (stats|runs|wickets)\b|\bpressure index\b|\bmvp points\b",
     "impact points (ESPN's Smart Stats are not in the archive; a win-probability impact measure is planned)"),
    (r"\bcatch(es)?\b|\bfielders?\b|\bfielding\b|\bstumpings?\b|\brun[- ]outs? by\b",
     "fielding (catches, stumpings and run outs are not loaded yet)"),
    (r"\bwinning (side|team)\b|\bpart of (a |the )?winning\b", "team results in the matches a player played"),
    (r"\bspeed\b|\bfastest (ball|delivery)\b|\bkph\b|km/h|\bmph\b", "ball speed"),
]
OVER_RANGE = re.compile(r"\bovers?\s*(\d{1,2})\s*(?:-|to|and|–)\s*(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+over\b"
                        r"|\b(first|last|final)\s+over\b")
INNINGS = re.compile(r"\b(second|2nd|first|1st)\s+innings\b|\bchas(e|es|ing)\b|\bbatting (first|second)\b|\bdefend")
AGGREGATE = re.compile(r"\bhow many (matches|times|games|teams|players|seasons|finals|playoffs|knockouts|ties)\b"
                       r"|\b(fastest|quickest|slowest|first|youngest|oldest) to\b|\bin a season\b"
                       r"|\bwhich (team|teams|player|players|bowler|batter|side)\b"
                       r"|\b(most|least|fewest|highest|lowest|record|ever|all[- ]time)\b|\bipl history\b|\bin (the )?ipl\b")


UNANSWERABLE = {label for _, label in UNSUPPORTED}      # no query can stand in for these


def capability_gaps(question: str, calls: list[dict[str, Any]]) -> list[str]:
    """What the question asks for that the planned queries cannot provide."""
    text = question.casefold()
    gaps = [label for pattern, label in UNSUPPORTED if re.search(pattern, text)]
    names = {c["name"] for c in calls}
    gaps += [g for g in slices.coverage_gaps(question, calls) if g not in gaps]
    if OVER_RANGE.search(text) and not names & {"over_range_leaders", "match_results", "slice_stats"} \
            and not _phase_in(text.replace("-", " ")):
        gaps += [] if "these specific overs" in gaps else ["these specific overs"]
    if INNINGS.search(text) and not names & {"over_range_leaders", "match_results", "slice_stats", "partnerships"} \
            and names - {"player_profile", "batter_vs_bowler", "head_to_head", "team_seasons", "champions", "ground_report", "venue_stats"}:
        gaps += [] if "one innings (batting first or chasing)" in gaps else ["one innings (batting first or chasing)"]
    return gaps


def top_tie(results: list[dict[str, Any]]) -> list[str]:
    """Names sharing the top value of the first leaderboard, or [] when there is a single leader."""
    for result in results:
        key = _key_column(result)
        block = next((b for b in result.get("blocks", []) if b["type"] == "table" and b["rows"]), None)
        if not key or not block or key not in block["columns"] or block["columns"][0] not in ("player", "team", "batter", "bowler", "pair"):
            continue
        i = block["columns"].index(key)
        tied = [r[0] for r in block["rows"] if r[i] == block["rows"][0][i]]
        return tied if len(tied) > 1 else []
    return []


CROWN = re.compile(r"\b(top|tops|leads?|leading|the most|the best|the highest|the lowest|the fewest|number one|no\. ?1)\b", re.I)


def crowns_top(reply: str, results: list[dict[str, Any]]) -> bool:
    """False when the reply's opening sentence crowns a name that is not first (or tied first) in the
    leaderboard it answers from: every name and number can be in the table while the ranking is wrong."""
    for result in results:
        if (result.get("args") or {}).get("split"):
            return True                                # a comparison, not a ranking
        block = next((b for b in result.get("blocks", []) if b["type"] == "table" and b["rows"]), None)
        if not block or block["columns"][0] not in ("player", "team", "batter", "bowler", "pair"):
            continue
        first = re.split(r"(?<=[.!?])\s", reply.strip())[0]
        if not CROWN.search(first):
            return True
        names = [str(r[0]) for r in block["rows"]]
        allowed = set(top_tie([result])) or {names[0]}
        where = {}
        for name in names:
            for key in {name, name.split()[-1]}:
                m = re.search(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", first)
                if m:
                    where[name] = min(where.get(name, m.start()), m.start())
        if not where:
            return True
        return min(where, key=where.get) in allowed
    return True


# A claim word -> the columns it can refer to, and whether a lower value is the better one for "best".
CLAIM_COLUMNS = {
    "sixes": (("6s", "sixes"), False), "fours": (("4s", "fours"), False), "runs": (("runs", "R"), False),
    "wickets": (("wkts", "wickets", "W"), False), "strike rate": (("sr", "strike rate"), False),
    "economy": (("econ", "economy"), True), "average": (("avg", "average"), False),
    "dot balls": (("dot balls", "dots"), False), "dot-ball": (("dot_pct", "dot-ball %", "Dot %"), False),
    "boundaries": (("boundaries", "4s+6s"), False),
}
CLAIM = re.compile(r"\b(most|highest|lowest|fewest|least|best)\b(?:\s+\w+){0,2}?\s+(sixes|fours|runs|wickets|strike rate|economy"
                   r"|average|dot balls|dot[- ]ball|boundaries)\b", re.I)


def claims_hold(reply: str, results: list[dict[str, Any]]) -> bool:
    """Every "most / highest / lowest X" claim about a named player must be true for column X of the table,
    not just for the ranking column: "Stoinis hit the most sixes" came from a strike-rate table where Dhoni had 72."""
    tables = [(r, b) for r in results if not (r.get("args") or {}).get("split")
              for b in r.get("blocks", []) if b["type"] == "table" and b["rows"]
              and b["columns"][0] in ("player", "batter", "bowler", "team", "pair")]
    for sentence in re.split(r"(?<=[.!?])\s+", reply):
        for m in CLAIM.finditer(sentence):
            word, what = m.group(1).lower(), m.group(2).lower().replace("dot ball", "dot-ball")
            columns, lower_better = CLAIM_COLUMNS.get(what, ((), False))
            for _, block in tables:
                col = next((block["columns"].index(c) for c in columns if c in block["columns"]), None)
                if col is None:
                    continue
                values = [(str(r[0]), r[col]) for r in block["rows"] if isinstance(r[col], (int, float))]
                if not values:
                    continue
                where = {}
                for name, _ in values:
                    for key in {name, name.split()[-1]}:
                        hit = re.search(rf"(?<![A-Za-z]){re.escape(key)}(?![A-Za-z])", sentence)
                        if hit:
                            where[name] = min(where.get(name, hit.start()), hit.start())
                if not where:
                    break
                claimed = min(where, key=where.get)
                lowest = word in ("lowest", "fewest", "least") or (word == "best" and lower_better)
                target = (min if lowest else max)(v for _, v in values)
                if dict(values).get(claimed) != target:
                    return False
                break
    return True


def states_definition(reply: str, definition: str) -> bool:
    """A definition counts as stated when the numbers in its first sentence appear in the reply."""
    head = definition.split(". ")[0]
    numbers = re.findall(r"\d+", head)
    return all(re.search(rf"(?<!\d){n}(?!\d)", reply) for n in numbers) if numbers else head.casefold() in reply.casefold()


def mentions_tie(reply: str, results: list[dict[str, Any]]) -> bool:
    """False when the table has a tie at the top and the reply crowns one name."""
    return not top_tie(results) or bool(re.search(r"\b(tie|tied|share|shared|joint|level|equal)\b", reply, re.I))


def _innings_of(text: str) -> dict[str, int]:
    """'bowling first' / 'batting first' -> innings 1; 'bowling second' / 'chasing' -> innings 2."""
    if re.search(r"\b(batting|bowling|fielding) first\b|\bfirst innings\b|\b1st innings\b", text):
        return {"innings": 1}
    if re.search(r"\b(batting|bowling|fielding) second\b|\bsecond innings\b|\b2nd innings\b|\bchasing\b", text):
        return {"innings": 2}
    return {}


STAGE_FINAL = re.compile(r"(?<!semi-)(?<!semi )(?<!qualifying )(?<!elimination )\b(?:ipl )?finals?\b(?!\s+(?:\w+\s+)?overs?\b)")
STAGE_PLAYOFF = re.compile(r"\b(play-?offs?|knock-?outs?|qualifiers?|eliminators?|semi-?finals?)\b")


def _stage_of(text: str) -> dict[str, str]:
    """'in finals' -> stage final; 'playoffs', 'knockouts', 'qualifiers' -> stage playoff."""
    return {"stage": "final"} if STAGE_FINAL.search(text) else {"stage": "playoff"} if STAGE_PLAYOFF.search(text) else {}


def is_aggregate(question: str) -> bool:
    """A question about many matches, which a list of 10 matches cannot answer."""
    text = question.casefold()
    return bool(AGGREGATE.search(text)) and not ON_SCREEN.search(text)


def gap_reply(gaps: list[str], has_tables: bool) -> str:
    what = " and ".join(gaps)
    if has_tables:
        return f"I can't break this down by {what} yet, so here is the closest the archive has. "
    if what == "AGGREGATE":
        return ("I can't answer that one yet: none of my archive queries covers it, and I won't guess from a sample of "
                "matches. I can answer stats across every IPL match (leaders by over range or innings, milestones, "
                "close finishes, players, grounds), or open any match.")
    return (f"I can't answer that yet: the archive doesn't cover {what}. I can answer stats across every IPL match "
            f"(leaders by over range or innings, milestones, close finishes, players, grounds), or open any match.")


# ----------------------------------------------------------------- scope

SCOPE_RULES: list[tuple[str, str]] = [
    (r"\bwpl\b|\bwomen'?s\b|\bwomen\b", "the Women's Premier League or women's cricket"),
    (r"\btests?\b|\bodis?\b|one[- ]day|world cup|champions trophy|\bt20is?\b|t20 internationals?"
     r"|\bfor (india|australia|england|pakistan|south africa|new zealand|sri lanka|west indies|bangladesh|afghanistan)\b",
     "international cricket"),
    (r"champions league|\bclt ?20\b|big bash|\bbbl\b|\bpsl\b|pakistan super league|\bcpl\b|caribbean premier|\bsa ?20\b|\bilt ?20\b|the hundred"
     r"|\bmlc\b|major league cricket|\bcounty\b|ranji|vijay hazare|syed mushtaq|lanka premier|\blpl\b|\bbpl\b|t20 blast",
     "other leagues"),
    (r"auction|sold for|\bsalary\b|\bsalaries\b|\bcrores?\b|retained for|\bcontract\b|net worth|\bpaid\b|\bearns?\b",
     "auctions or salaries"),
    (r"who will win|\bwill (win|score|take)\b|\bpredict|\bfavou?rites?\b|\bodds\b|going to win", "predictions"),
    (r"how old|\bage\b|\bborn\b|birthday|\bheight\b|\bwife\b|married", "personal details"),
    (r"\blive (score|match|game|update)s?\b|right now|today'?s (ipl )?(match|game|score)", "live scores"),
    (r"football|soccer|\bisl\b|\bnba\b|\bnfl\b|tennis|hockey|kabaddi|formula (1|one)|\bf1\b|(?<!indian )premier league",
     "other sports"),
]


def out_of_scope(question: str, today: date | None = None) -> str | None:
    """What the question asks about that the archive cannot answer, or None when it is an IPL question."""
    text = question.casefold()
    for pattern, topic in SCOPE_RULES:
        if re.search(pattern, text):
            return topic
    year = (today or date.today()).year
    if any(int(y) > year for y in re.findall(r"\b(20\d\d)\b", text)):
        return "predictions"
    return None


def scope_reply(topic: str) -> str:
    return (f"That's outside what I cover: I only have IPL matches (2008 onwards, ball by ball), so I can't answer "
            f"questions about {topic}. Try an IPL question instead, such as \"most runs in IPL 2024\".")


SCOPE_SUGGESTIONS = ["Most runs in IPL 2024", "Kohli chasing vs batting first", "Who are the best finishers in the IPL?",
                     "Highest partnership in IPL history"]


# ----------------------------------------------------------------- facts and summaries

LEADING = {"player", "team", "ground", "batter", "bowler", "pair"}


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
            split = (result.get("args") or {}).get("split")
            ranked = bool(columns) and columns[0] in LEADING and not (split and split in columns)   # a comparison is not a ranking
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
    if result["name"] == "over_range_leaders":
        return args.get("sort") or ("econ" if args.get("role") == "bowl" else "sr")
    if result["name"] == "champions":
        return "champion"
    return slices.key_column(result)


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
        ("economical", "econ"), ("wickets", "wkts"), ("runs", "runs"), ("fours", "4s"), ("maximums", "sixes"),
        ("aggregate", "runs"), ("hundreds", "100s"), ("centuries", "100s"), ("fifties", "50s"), ("average", "avg")]


def _label(column: str) -> str:
    return LABELS.get(column, column.replace("_", " "))


def _pick_block(blocks: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    tables = [b for b in blocks if b["type"] == "table"]
    if len(tables) <= 1:
        return tables[0] if tables else None
    words = set(re.findall(r"[a-z]+", question.casefold())) - {"the", "in", "of", "a", "ipl", "most", "what", "who", "is"}
    synonyms = {"figures": {"bowling", "figures"}, "fifty": {"fifties", "fastest"}, "individual": {"individual", "score"},
                "innings": {"innings"}, "total": {"totals", "team"}, "knock": {"individual", "scores"}}
    # A player's batting or bowling table, by what the question asks for ("Russell's strike rate" is batting).
    bowl_cue = bool(words & {"wickets", "wicket", "economy", "bowling", "bowled", "scalps", "dismissed", "overs",
                             "maidens", "bowler"})
    bat_cue = bool(words & {"runs", "strike", "sr", "sixes", "fours", "batting", "score", "scored", "fifties",
                            "hundreds", "fast", "batter", "knock"})
    asked_phase = _phase_in(question.casefold().replace("-", " "))
    def score(block: dict[str, Any]) -> int:
        title = set(re.findall(r"[a-z]+", block["title"].casefold()))
        extra = sum(len(syn & title) for key, syn in synonyms.items() if key in words)
        if asked_phase and "phase" in title:
            extra += 5                                # "at the death": the by-phase table, not the career total
        role = 3 if ("batting" in title and bat_cue and not bowl_cue) or ("bowling" in title and bowl_cue) else \
            -2 if ("bowling" in title and bat_cue and not bowl_cue) else 0
        return len(words & title) + extra + role
    return max(tables, key=score)


def _describe(cols: list[str], row: list[Any], skip: int = 0, lead: str | None = None) -> str:
    """The row as "label value" pairs; the column the question asks about comes first, so it is never cut."""
    pairs = [(c, v) for c, v in list(zip(cols, row))[skip:] if v is not None and c != "match_id"]
    if lead:
        pairs = [p for p in pairs if p[0] == lead] + [p for p in pairs if p[0] != lead]
    return ", ".join(f"{_label(c)} {v}" for c, v in pairs[:8 - skip])


def summary(results: list[dict[str, Any]], question: str = "") -> str:
    """A grounded answer straight from the tables (used when no LLM answers or its numbers do not check out)."""
    text = question.casefold()
    asked = next((col for phrase, col in ASKS if phrase in text), None)
    years = re.findall(r"\b(20\d\d)\b", text)
    def lead(cols: list[str]) -> str | None:
        column = {"sixes": "6s", "fours": "4s"}.get(asked or "", asked) if asked and asked not in cols else asked
        return column if column in cols else None
    parts = []
    defined = next((r["definition"] for r in results if r.get("definition")), None)
    if defined:
        parts.append(defined)                          # an evaluative word: say what it was taken to mean
    for result in results[:3]:
        notes = [b["text"] for b in result["blocks"] if b["type"] == "text" and not b["text"].startswith("Definition: ")]
        block = _pick_block(result["blocks"], question)
        if block is None:
            parts.extend(notes[:1])
            continue
        rows, cols = block["rows"], block["columns"]
        if not rows:
            parts.append(notes[-1] if result["name"] == "hattricks" and notes else f"{block['title']}: no rows.")
            continue
        if result["name"] == "player_match" and block and block["rows"]:
            row = dict(zip(block["columns"], block["rows"][0]))
            mine = next((b for b in result["blocks"] if b["type"] == "table" and b is not block), None)
            did = "; ".join(f"{r[0]} {r[1]} ({r[2]} balls)" for r in (mine or {}).get("rows", []))
            parts.append(f"{block['title']}: {row.get('date')}, {row.get('team1')} v {row.get('team2')} "
                         f"({row.get('result')})" + (f" — {did}." if did else "."))
            continue
        if result["name"] == "match_results" and (result.get("args") or {}).get("kind") in ("close_runs", "close_wickets", "tied") and notes:
            parts.append(notes[0])
            continue
        if result["name"] == "player_profile" and _has(text, "teams", "franchise", "franchises", "played for") \
                and any("played for" in n for n in notes):
            parts.append(next(n for n in notes if "played for" in n))
            continue
        if result["name"] == "hattricks" and notes:
            parts.append(notes[-1])
            continue
        if result["name"] == "slice_stats" and len(cols) > 1 and cols[1] == (result.get("args") or {}).get("split"):
            key = _key_column(result)
            column = key if key in cols else cols[2]
            pick = rows if len(rows) <= 6 else [rows[0], *sorted(rows, key=lambda r: r[cols.index(column)] or 0)[:: len(rows) - 1], rows[-1]]
            seen, shown = set(), []
            for r in pick:                                   # a long trend: first, lowest, highest, last
                if id(r) not in seen:
                    seen.add(id(r))
                    shown.append(r)
            parts.append(f"{block['title']}: " + "; ".join(
                f"{r[0]} {r[1]} — {_label(column)} {r[cols.index(column)]}" for r in shown) + ".")
            continue
        if result["name"] == "ground_report" and notes:
            parts.append(notes[-1])
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
        if result["name"] == "team_seasons" and "finish" in cols and _has(text, *TITLE_WORDS):
            won = [str(r[0]) for r in rows if r[cols.index("finish")] == "Champions"]
            team = (result.get("args") or {}).get("team", "")
            parts.append(f"{team} won {len(won)} IPL title{'s' if len(won) != 1 else ''}"
                         + (f": {', '.join(won)}." if won else "."))
            continue
        if cols[0] == "year" and str(rows[-1][0]) == "Total" and not years:
            parts.append(f"{block['title']} — in all: {_describe(cols, rows[-1], 1, lead(cols))}.")
            continue
        if cols[0] == "year":
            chosen = next((r for r in rows if str(r[0]) in years), None)
            column = asked if asked in cols else (asked == "sixes" and "6s" in cols and "6s") or None
            if chosen is None and column:
                numeric = [r for r in rows if isinstance(r[cols.index(column)], (int, float))]
                lowest = column == "econ" or "lowest" in text
                chosen = (min if lowest else max)(numeric, key=lambda r: r[cols.index(column)]) if numeric else None
            chosen = chosen or rows[-1]
            parts.append(f"{block['title']}: {chosen[0]} — {_describe(cols, chosen, 1, lead(cols))}.")
            continue
        if cols[0] == "phase" and (ph := _phase_in(text.replace("-", " "))):
            row = next((r for r in rows if r[0] == ph), rows[0])
            parts.append(f"{block['title']}: {row[0]} — {_describe(cols, row, 1, lead(cols))}.")
            continue
        if len(rows) == 1 or cols[0] == "ground":
            parts.append(f"{block['title']}: {rows[0][0]} — {_describe(cols, rows[0], 1)}.")
            continue
        key = _key_column(result)
        asked_col = "6s" if asked == "sixes" and "6s" in cols else (asked if asked in cols else None)
        record_col = next((c for c in ("score", "figures", "balls_to_50", "6s") if c in cols), None)
        column = key if key in cols else (asked_col or record_col or cols[1])
        top = [f"{r[0]} ({_label(column)} {r[cols.index(column)]})" for r in rows[:3]]
        best = rows[0][cols.index(column)]
        tied = [r[0] for r in rows if r[cols.index(column)] == best]
        if len(tied) > 1:                              # "Warner has the most" is wrong when three share the top
            parts.append(f"{block['title']}: {', '.join(tied[:-1])} and {tied[-1]} are tied on {_label(column)} {best}.")
            continue
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

def planner_prompt(today: date | None = None, tools: list[dict[str, Any]] | None = None) -> str:
    today = today or date.today()
    names = {t["function"]["name"] for t in (tools or TOOLS)}
    hints = "\n".join(line for line in slices.PLANNER_HINTS.splitlines()
                      if not any(f"-> {tool}" in line and tool not in names for tool in slices.SLICE_TOOL_NAMES - {"slice_stats"})
                      or "slice_stats" in line)
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
Kohli against Bumrah, or Bumrah vs Kohli -> batter_vs_bowler(batter=Virat Kohli, bowler=Jasprit Bumrah)
who won IPL 2019 -> champions(year=2019) | how many titles has CSK won -> team_seasons(team=CSK)
Bumrah's wickets -> player_profile(Jasprit Bumrah) | sixes hit in IPL 2024 -> season_summary(year=2024)
highest total at the Kotla -> venue_stats(name=Kotla) | most runs 2008 to 2025 -> batting_leaders(year=2008-2025)
Orange Cap = batting_leaders sort runs; Purple Cap = bowling_leaders sort wkts; maximums = sixes; scalps = wickets.
first six overs = powerplay, overs 16-20 = death (phase_leaders; "who bowled best" -> role=bowl). Keep the question's year.
Other over ranges or one innings -> over_range_leaders (overs 12-20 chasing -> from_over=12, to_over=20, innings=2).
fastest to 2000 runs -> milestones | Rohit's 100th match -> player_match | won by under 5 runs -> match_results.
{hints}
If no tool answers exactly, call none: never substitute a different question."""


# Tools every planning call carries, and tools added only when the wording calls for them. Each call
# sends its schemas, and Groq's free tier counts tokens: the gated tools are ~40% of the schema text.
CORE_TOOLS = {MATCH_ROUTE, "batting_leaders", "bowling_leaders", "player_profile", "batter_vs_bowler", "phase_leaders",
              "slice_stats", "records", "champions", "team_seasons", "head_to_head"}
GATED_TOOLS = [
    ("match_results", r"\bwon by\b|\bmargin|\bdefend|\bchas|\btarget|\bties?\b|\btied\b|\bsuper overs?\b|\bneeded\b"
                      r"|\brequired\b|\bclose (?:finish|win|game)|\b\d{3}\s*(?:\+|or more|plus)|\bconceded\b|\btotals? of\b"),
    ("over_range_leaders", r"\bovers?\s*\d|\b\d{1,2}(?:st|nd|rd|th) over\b|\b(?:first|last|final) (?:over|\w+ overs)\b"
                           r"|\binnings\b|\bchasing\b|\bbatting (?:first|second)\b"),
    ("milestones", r"\b(?:fastest|quickest|fewest|least|soonest|slowest)\b"),
    ("player_match", r"\b\d{1,3}(?:st|nd|rd|th) (?:ipl )?(?:match|game)\b"),
    ("venue_stats", r"\b(?:ground|grounds|venue|venues|stadium|pitch|surface|par)\b|\bat (?:the )?[a-z]+"),
    ("ground_report", r"\b(?:ground|venue|stadium|pitch|surface|conditions|par)\b"),
    ("season_summary", r"\bseasons?\b|\byear\b|\boverall\b|\bin (?:ipl )?20\d\d\b"),
    ("hattricks", r"\bhat[- ]?tricks?\b"),
    ("toss_stats", r"\btoss\b"),
    ("awards", r"\b(?:man|player) of the match\b|\bpotm\b|\bmotm\b|\bawards?\b"),
    ("impact_subs", r"\bimpact (?:player|sub)"),
    ("partnerships", r"\bpartnerships?\b|\bstands?\b|\bpairs?\b|\bduos?\b|\bopening\b"),
    ("home_record", r"\bhome\b|\baway\b"),
]


def tools_for(question: str) -> list[dict[str, Any]]:
    """The tool schemas one planning call needs: the core set plus any the wording points to."""
    text = question.casefold()
    wanted = set(CORE_TOOLS) | {name for name, pattern in GATED_TOOLS if re.search(pattern, text)}
    return [t for t in TOOLS if t["function"]["name"] in wanted]


def llm_plan(router: Any, question: str, history: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], str, str]:
    """(calls, provider, model). An empty list means the match path. Raises RuntimeError if no LLM answers."""
    context = [
        {"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("content", ""))[:300]}
        for item in (history or [])[-4:]
    ]
    asked = " ".join([question, *(m["content"] for m in context if m["role"] == "user")])
    tools = tools_for(asked)
    messages = [{"role": "system", "content": planner_prompt(tools=tools)}, *context, {"role": "user", "content": question}]
    calls, provider, model = router.plan(messages, tools)
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
            calls, dropped = ground_calls(calls, question, entities, history, index)
            calls, invalid = _valid_calls(calls, index)
            if dropped or invalid:
                note = f"Dropped LLM calls not grounded in the question or not valid: {'; '.join(dropped + invalid)}."
        except RuntimeError as exc:
            note = f"LLM planner unavailable ({str(exc)[:100]})."
    if mode in ("hybrid", "rules"):
        rule_calls = rule_plan(question, entities, index)
        if rule_calls and prefer_rules(calls, rule_calls):
            planner = "rules" if not calls else f"rules (over {planner})"
            calls = rule_calls
    calls, uncovered = slices.enforce_coverage(calls, question)
    if any(c.get("converted_from") for c in calls):
        note = (note + " " if note else "") + "Rewrote " + ", ".join(sorted({c["converted_from"] for c in calls if c.get(
            "converted_from")})) + " as a slice so every filter in the question is applied."
    return orient_matchups(calls, question, index), planner, note


BOWLER_FIRST = re.compile(r"\b(bowling|bowled|bowls) (to|at)\b|\bagainst the bat of\b")
BATTER_FIRST = re.compile(r"\b(facing|faced|faces|batting against|batted against|bats against)\b")


def orient_matchups(calls: list[dict[str, Any]], question: str, index: PlayerIndex | None) -> list[dict[str, Any]]:
    """Put the batter and bowler the right way round, whatever the word order ("Bumrah vs Kohli").
    The wording decides when it says ("X bowling to Y", "X facing Y"); otherwise the player who bats
    more than he bowls is the batter."""
    if index is None:
        return calls
    text = question.casefold()
    out = []
    for call in calls:
        args = dict(call.get("args") or {})
        if call["name"] == "batter_vs_bowler" and args.get("batter") and args.get("bowler"):
            batter, _ = index.lookup(str(args["batter"]))
            bowler, _ = index.lookup(str(args["bowler"]))
            if batter and bowler:
                named = index.scan(question)[0]
                first = named[0].player_id if named else None
                if BOWLER_FIRST.search(text) and first is not None:
                    swap = first == batter.player_id
                elif BATTER_FIRST.search(text) and first is not None:
                    swap = first == bowler.player_id
                else:
                    swap = bowler.bat_share > batter.bat_share + 0.15
                if swap:
                    args["batter"], args["bowler"] = args["bowler"], args["batter"]
        out.append({**call, "args": args})
    return out


YEAR_TOOLS = {t["function"]["name"] for t in TOOLS if "year" in t["function"]["parameters"]["properties"]}


def _valid_calls(calls: list[dict[str, Any]], index: PlayerIndex | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop calls the archive cannot run (a player name as a team, a bad sort). A name that is unknown or
    shared stays: the answer says so, or asks which player."""
    if index is None:
        return calls, []
    kept, invalid = [], []
    for call in calls:
        try:
            to_argv(call, index)
        except (UnknownPlayer, AmbiguousName):
            pass
        except PlanError as exc:
            invalid.append(f"{call['name']} ({exc})")
            continue
        kept.append(call)
    return kept, invalid


def ground_calls(calls: list[dict[str, Any]], question: str, entities: dict[str, Any],
                 history: list[dict[str, str]] | None = None,
                 index: PlayerIndex | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only what the question supports: a player the planner names must be named in the question
    (or the recent conversation), and a year must be one the question states. The same idea as the
    numeric grounding check, applied to the plan."""
    context = _norm(" ".join([question, *(str(h.get("content", "")) for h in (history or [])[-4:]
                                          if h.get("role") != "assistant")]))
    words = set(context.split())
    named = {p.player_id for p in index.scan(context)[0]} if index else set()
    named_teams = set(entities.get("teams") or []) | set(_teams(" ".join(
        [question, *(str(h.get("content", "")) for h in (history or [])[-4:] if h.get("role") != "assistant")]).casefold()))
    years = sorted(entities.get("years") or [])
    kept, dropped = [], []
    for call in calls:
        args = dict(call.get("args") or {})
        player_keys = ("batter", "bowler") if call["name"] == "batter_vs_bowler" else \
            ("name",) if call["name"] == "player_profile" else ()
        missing = []
        for key in player_keys:
            if not args.get(key):
                continue
            tokens = _norm(str(args[key])).split()
            player = index.lookup(str(args[key]))[0] if index else None
            if tokens and tokens[-1] in words:
                # "Pandya" alone is two players: keep the question's bare surname so the answer asks which one,
                # instead of the first name the planner guessed.
                if index and not set(tokens[:-1]) & words and index.lookup(tokens[-1])[0] is None:
                    args[key] = tokens[-1]
            elif not (player and player.player_id in named):   # "Rohit's runs" names Rohit Sharma by first name
                missing.append(str(args[key]))
        if call["name"] == "awards" and args.get("player") and str(args["player"]).upper() in TEAM_CODES:
            args.setdefault("team", str(args.pop("player")).upper())   # awards(player="CSK") meant the team
        if call["name"] in ("slice_stats", "awards"):
            names = args.get("player") or []
            names = [names] if isinstance(names, str) else list(names)
            names += [args["vs"]] if args.get("vs") else []
            for value in names:
                tokens = _norm(str(value)).split()
                player = index.lookup(str(value))[0] if index else None
                if not (tokens and tokens[-1] in words) and not (player and player.player_id in named):
                    missing.append(str(value))
        if missing:
            dropped.append(f"{call['name']}({', '.join(missing)})")
            continue
        for key in ("team", "opp"):                   # a team the question (or the recent conversation) never names
            if args.get(key):
                try:
                    code = _team(args[key])
                except PlanError:
                    code = None
                if code not in named_teams:
                    args.pop(key)
        if args.get("innings") and not re.search(r"\b(second|2nd|first|1st)\s+innings\b|\bchas|\bbatting (first|second)\b"
                                                 r"|\b(bowling|fielding) (first|second)\b|\bdefend|\btarget", question.casefold()):
            args.pop("innings")                       # "the 1st over of the innings" is not "the first innings"
        last = re.search(r"\b(?:last|final)\s+(\d|two|three|four|five|six)\s+overs\b", question.casefold())
        if last and call["name"] == "match_results" and args.get("kind") == "needed_at_over":
            k = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(last.group(1)) or int(last.group(1))
            args["over"] = 21 - k                     # "last 4 overs" starts at over 17, whatever the planner said
        if call["name"] == "match_results":
            low = question.casefold()
            args.pop("stage", None)
            args.update(_stage_of(low))               # "in how many finals..." must not count every match
            if args.get("kind") != "needed_at_over":
                args.pop("over", None)                # only needed_at_over takes an over
            if args.get("kind") in ("scored_over", "conceded_over", "chased_over"):
                total = re.search(r"\b(?:over|above|more than|at least)\s+(\d{3})\b|\b(\d{3})\s*(?:or more|plus|\+)", low)
                if total:
                    args["value"] = int(total.group(1) or total.group(2))   # "200 or more", wherever the planner put it
                if args["kind"] != "chased_over":
                    args.pop("innings", None)
                    args.update(_innings_of(low))     # "while bowling first" is innings 1
            upto = re.search(r"\b(\d{2,3})\s+or\s+(?:less|lesser|lower|fewer|below|under)\b", low)
            if upto and args.get("kind") == "defended_under" and int(args.get("value") or 0) == int(upto.group(1)):
                args["value"] = int(upto.group(1)) + 1   # "120 or less" includes 120
        year = str(args.get("year") or "").strip()
        if not year and years and call["name"] in YEAR_TOOLS:
            args["year"] = _year_arg(years)["year"]          # the planner dropped the question's year
        elif year and YEAR_PATTERN.match(year):
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
    if any(c.get("definition") or c.get("keep") for c in rule_calls):
        return True                                   # a stated definition or a deliberate comparison: the rule decides
    player_tools = ("player_profile", "batter_vs_bowler")    # venue_stats also has a "name": a ground, not a player
    named = {str(v).casefold() for c in rule_calls if c["name"] in player_tools
             for k, v in (c.get("args") or {}).items() if k in ("name", "batter", "bowler")}
    looked_at = {str(v).casefold() for c in llm_calls if c["name"] in player_tools
                 for k, v in (c.get("args") or {}).items() if k in ("name", "batter", "bowler")}
    surnames = {n.split()[-1] for n in looked_at if n.split()}
    precise = {"over_range_leaders", "match_results", "milestones", "player_match", *slices.SLICE_TOOL_NAMES}
    if rule_calls[0]["name"] in precise and [(c["name"], (c.get("args") or {}).get("kind")) for c in llm_calls] != \
            [(c["name"], (c.get("args") or {}).get("kind")) for c in rule_calls]:
        return True                                   # over numbers, margins and milestones: the explicit rule wins
    if named and any(n.split()[-1] not in surnames for n in named if n.split()):
        return True                                   # the question names a player the LLM plan never looks at
    if any(c["name"] == "batter_vs_bowler" for c in rule_calls) and not any(c["name"] == "batter_vs_bowler" for c in llm_calls):
        return True                                   # "Narine vs Rohit" is a matchup, not two profiles
    same_tool = {c["name"] for c in llm_calls} == {c["name"] for c in rule_calls}
    return same_tool and len(rule_calls) > len(llm_calls)


BAT_WORDS = {"sixes": "sixes", "six": "sixes", "fours": "fours", "boundaries": "fours", "fifties": "fifties",
             "half centuries": "fifties", "half-centuries": "fifties", "hundreds": "hundreds", "centuries": "hundreds",
             "tons": "hundreds", "strike rate": "sr", "average": "avg", "highest score": "hs", "runs": "runs",
             "orange cap": "runs", "run scorer": "runs", "run-scorer": "runs", "top scorer": "runs",
             "run getter": "runs", "aggregate": "runs", "maximums": "sixes", "maximum": "sixes", "six hitter": "sixes"}
BOWL_WORDS = {"economy": "econ", "economical": "econ", "purple cap": "wkts", "wickets": "wkts",
              "wicket taker": "wkts", "wicket-taker": "wkts", "four-wicket": "4w", "4 wicket": "4w",
              "scalps": "wkts", "dismissals": "wkts"}
# Role words alone ("leading batter", "top bowler") mean the default sort; a specific word wins over them.
GENERIC_BAT = ("batter", "batters", "batsman", "batsmen")
GENERIC_BOWL = ("bowler", "bowlers")
PHASES = {"powerplay": "powerplay", "power play": "powerplay", "middle overs": "middle", "death": "death",
          "death overs": "death", "slog": "death"}
PHASE_OVERS = [(re.compile(r"\b(first|1st|opening) (six|6) overs\b|\bovers? 1 (to|and) 6\b"), "powerplay"),
               (re.compile(r"\bovers? 7 (to|and) 15\b"), "middle"),
               (re.compile(r"\bovers? 16 (to|and) 20\b|\b(last|final) (four|4|five|5) overs\b"), "death")]
LEADER_WORDS = ("most", "top", "highest", "best", "leading", "lowest", "who has", "who hit", "who scored",
                "who took", "orange cap", "purple cap", "leaderboard", "fewest", "biggest")
RECORD_WORDS = ("highest total", "highest team total", "biggest total", "fastest fifty", "fastest 50",
                "best bowling figures", "best figures", "highest individual", "records", "in an innings",
                "single innings", "in a single", "in one innings", "knock", "innings total", "team total",
                "team score", "individual innings", "individual score")
TITLE_WORDS = ("titles", "title", "trophy", "trophies", "championship", "championships", "champions",
               "runner up", "runners up", "lifted")
# Team names fans use. Only read as a team when not a place ("at Chennai" is the ground).
TEAM_NAMES = {"chennai": "CSK", "super kings": "CSK", "mumbai": "MI", "kolkata": "KKR", "knight riders": "KKR",
              "bengaluru": "RCB", "bangalore": "RCB", "royal challengers": "RCB", "hyderabad": "SRH",
              "sunrisers": "SRH", "rajasthan": "RR", "royals": "RR", "punjab": "PBKS", "delhi": "DC",
              "capitals": "DC", "gujarat": "GT", "titans": "GT", "lucknow": "LSG", "super giants": "LSG"}
MATCH_WORDS = ("final", "qualifier", "eliminator", "match on", "scorecard", "ball by ball", "ball-by-ball",
               "commentary", "momentum", "this match", "that match", "the match")


def _phase_in(text: str) -> str | None:
    """The phase a question names: "at the death", "powerplay", "overs 16 to 20"."""
    return next((PHASES[w] for w in sorted(PHASES, key=len, reverse=True) if _has(text, w)), None) \
        or next((name for pattern, name in PHASE_OVERS if pattern.search(text)), None)


@lru_cache(maxsize=4096)
def _word_pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z]){re.escape(word)}(?![a-z])")


def _has(text: str, *words: str) -> bool:
    # The cheap substring test skips the regex for the ~95% of words that are not in the text at all.
    return any(w in text and _word_pattern(w).search(text) for w in words)


def _year_arg(years: list[int]) -> dict[str, str]:
    if not years:
        return {}
    return {"year": str(years[0]) if len(years) == 1 else f"{min(years)}-{max(years)}"}


def _teams(text: str) -> list[str]:
    """Teams named in this message: codes and full names, then city and short names that are not a place."""
    teams, _ = _team_entities(text, fuzzy=False)
    for name in sorted(TEAM_NAMES, key=len, reverse=True):
        for match in re.finditer(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text):
            before = text[:match.start()].split()[-1:] or [""]
            if before[0] not in ("at", "in", "the") and TEAM_NAMES[name] not in teams:
                teams.append(TEAM_NAMES[name])
    return teams


def rule_plan(question: str, entities: dict[str, Any], index: PlayerIndex | None) -> list[dict[str, Any]]:
    """Keyword planner for when no LLM is available. Returns [] for the match path."""
    text = question.casefold().replace("-", " ")
    teams = _teams(text)                                # only teams named in this message
    year = _year_arg(entities.get("years") or [])
    players, ambiguous = index.scan(question) if index else ([], [])
    leader = _has(text, *LEADER_WORDS) or re.search(r"\brecord (team|individual|innings|score|total)", text) is not None

    if _has(text, "who won the ipl", "won the ipl", "ipl champion", *TITLE_WORDS) or re.search(r"who won (the )?ipl", text) \
            or re.search(r"\bwon (the )?(\d{4} )?(edition|season|tournament|competition)\b", text):
        if teams and _has(text, "how many", "number of", "count", *TITLE_WORDS):
            return [{"name": "team_seasons", "args": {"team": teams[0], **year}}]
        return [{"name": "champions", "args": year}]
    # One match: a final, a scorecard, a match on screen.
    if _has(text, *MATCH_WORDS) and not leader:
        return []
    if len(players) >= 2 and _has(text, "against", "vs", "v", "versus", "facing", "faced", "bowling to", "bowled to"):
        return [{"name": "batter_vs_bowler", "args": {"batter": players[0].name, "bowler": players[1].name, **year}}]
    if len(players) >= 2 and _has(text, "compare", "better", "comparison"):
        return [{"name": "player_profile", "args": {"name": p.name, **year}} for p in players[:2]]
    # Milestones and Nth match ("fastest to 2000 runs", "least balls to 100 wickets", "Rohit's 100th match").
    mile = re.search(r"\b(fastest|quickest|least|fewest|soonest)\b(.*?)\b(runs|wickets|fifties|half centuries|hundreds"
                     r"|centuries|tons|sixes|fours|ducks)\b", text)
    values = [int(v) for v in re.findall(r"\b(\d{1,5})\b", mile.group(2))] if mile else []
    if mile and values:                                # "fastest to 2000, 3000 runs": one query per milestone
        stat = {"half centuries": "fifties", "centuries": "hundreds", "tons": "hundreds"}.get(mile.group(3), mile.group(3))
        by = "balls" if _has(text, "balls", "deliveries") and stat in ("runs", "wickets") else "innings"
        return [{"name": "milestones", "args": {"stat": stat, "value": v, "by": by}} for v in values[:MAX_CALLS]]
    if (compared := slices.comparison_calls(text, players, year)):
        return compared
    if len(players) == 1 and _has(text, "teams", "team", "franchise", "franchises", "played for", "represented"):
        return [{"name": "player_profile", "args": {"name": players[0].name}}]     # its note lists his teams
    nth = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:ipl\s+)?(?:match|game)\b", text)
    if nth and len(players) == 1:
        return [{"name": "player_match", "args": {"name": players[0].name, "nth": int(nth.group(1))}}]
    # Hat-tricks, awards, impact subs, partnerships, the toss; then evaluative words with a stated definition.
    if (family := slices.family_calls(text, teams, players, year)):
        return family
    if (defined := slices.definition_calls(text, year, slices._team_roles(text, teams))):
        return defined
    # Filters the older tools lack. About players or a player metric, this wins over the match-count rules
    # below ("best strike rate chasing 200+" is not "how many 200+ chases").
    sliced = slices.slice_rule(question, players, teams, year)
    if sliced and (players or _has(text, "strike rate", "average", "economy", "runs scored", "batter", "batters",
                                   "batsman", "batsmen", "bowler", "bowlers", "run rate", "sixes", "fours", "dot balls")):
        return sliced
    # Close finishes and targets.
    margin = re.search(r"\b(?:less|fewer)\s+than\s+(\d{1,3})\s+(runs?|wickets?)\b|\b(?:under|below)\s+(\d{1,3})\s+(runs?|wickets?)\b"
                       r"|\bby\s+(\d)\s+(runs?|wickets?)\b", text)
    if margin and _has(text, "won", "win", "wins", "margin", "matches", "games"):
        g = margin.groups()
        value, unit = (int(g[0]), g[1]) if g[0] else (int(g[2]), g[3]) if g[2] else (int(g[4]) + 1, g[5])
        return [{"name": "match_results", "args": {"kind": "close_runs" if unit.startswith("run") else "close_wickets",
                                                   "value": value, **year, **_stage_of(text)}}]
    target = re.search(r"\b(?:under|below|less than)\s+(\d{2,3})\b", text)
    upto = re.search(r"\b(\d{2,3})\s+or\s+(?:less|lesser|lower|fewer|below|under)\b", text)
    if (target or upto) and _has(text, "defend", "defended", "defending"):
        value = int(target.group(1)) if target else int(upto.group(1)) + 1     # "120 or less" = under 121
        return [{"name": "match_results", "args": {"kind": "defended_under", "value": value, **year, **_stage_of(text)}}]
    total = re.search(r"\b(?:over|above|more than|at least)\s+(\d{3})\b|\b(\d{3})\s*(?:or more|plus|\+)", text)
    if total and _has(text, "conceded", "concede", "conceding", "leaked") and not _has(text, "chased", "chase"):
        return [{"name": "match_results", "args": {"kind": "conceded_over", "value": int(total.group(1) or total.group(2)),
                                                   **_innings_of(text), **year, **_stage_of(text)}}]
    if total and _has(text, "scored", "score", "posted", "made", "totals", "totalled") and _has(text, "team", "teams", "times") \
            and not _has(text, "chased", "chase", "player", "batter", "batsman"):
        return [{"name": "match_results", "args": {"kind": "scored_over", "value": int(total.group(1) or total.group(2)),
                                                   **_innings_of(text), **year, **_stage_of(text)}}]
    last = re.search(r"\b(?:last|final)\s+(\d|three|four|five)\s+overs\b", text)
    if last and _has(text, "chase", "chased", "chasing", "chases", "required", "needed", "need"):
        k = {"three": 3, "four": 4, "five": 5}.get(last.group(1)) or int(last.group(1))
        return [{"name": "match_results", "args": {"kind": "needed_at_over", "over": 21 - k, **year, **_stage_of(text)}}]
    chased = re.search(r"\b(?:over|above|more than|at least)\s+(\d{3})\b|\b(\d{3})\s*(?:or more|plus|\+)", text)
    if chased and _has(text, "chased", "chase", "chasing"):
        return [{"name": "match_results", "args": {"kind": "chased_over", "value": int(chased.group(1) or chased.group(2)), **year, **_stage_of(text)}}]
    if _has(text, "tied", "tie", "ties", "super over", "super overs") and _has(text, "how many", "matches", "games", "times"):
        return [{"name": "match_results", "args": {"kind": "tied", **year, **_stage_of(text)}}]
    if sliced:
        return sliced
    # Any over range or one innings: "overs 12-20 in the second innings", "the 1st over".
    rng = OVER_RANGE.search(question.casefold())
    if rng and not players and (leader or _has(text, "strike rate", "economy", "wickets", "runs")):
        if rng.group(1):
            lo, hi = int(rng.group(1)), int(rng.group(2))
        elif rng.group(3):
            lo = hi = int(rng.group(3))
        else:
            lo = hi = 1 if rng.group(4) == "first" else 20
        inn = 2 if re.search(r"\b(second|2nd) innings\b|\bchas", text) else \
            1 if re.search(r"\b(first|1st) innings\b|\bbatting first\b", text) else None
        standard = {(1, 6): "powerplay", (7, 15): "middle", (16, 20): "death"}.get((min(lo, hi), max(lo, hi)))
        if standard and not inn:
            rng = None                                # exactly a phase: the phase rule below answers it
    if rng and not players and (leader or _has(text, "strike rate", "economy", "wickets", "runs")):
        bowl = _has(text, "wickets", "economy", "economical", "bowler", "bowlers", "bowled")
        sort = ("wkts" if _has(text, "wickets") else "econ") if bowl else \
            ("runs" if _has(text, "runs") and not _has(text, "strike rate") else "sr")
        return [{"name": "over_range_leaders", "args": {"from_over": min(lo, hi), "to_over": max(lo, hi),
                                                        "role": "bowl" if bowl else "bat", "sort": sort,
                                                        **({"innings": inn} if inn else {}), **year}}]
    phase = next((PHASES[w] for w in sorted(PHASES, key=len, reverse=True) if _has(text, w)), None) \
        or next((name for pattern, name in PHASE_OVERS if pattern.search(text)), None)
    one_player = len(players) == 1 and not leader      # "Dhoni's strike rate at the death" is his profile, not a ranking
    if phase and not one_player and (leader or _has(text, "bowler", "bowlers", "batter", "batters", "batsmen", "economy", "strike rate")):
        role = "bowl" if _has(text, "bowler", "bowlers", "bowling", "bowled", "bowl", "economy", "economical",
                              "wickets") else "bat"
        return [{"name": "phase_leaders", "args": {"phase": phase, "role": role, **year}}]
    if leader:
        bowl = next((BOWL_WORDS[w] for w in sorted(BOWL_WORDS, key=len, reverse=True) if _has(text, w)), None) \
            or ("wkts" if _has(text, *GENERIC_BOWL) else None)
        bat = next((BAT_WORDS[w] for w in sorted(BAT_WORDS, key=len, reverse=True) if _has(text, w)), None) \
            or ("runs" if _has(text, *GENERIC_BAT) else None)
        if _has(text, *RECORD_WORDS) or re.search(r"\brecord (team|individual|innings|score|total)", text):
            return [{"name": "records", "args": year}]
        team = {"team": teams[0]} if len(teams) == 1 else {}
        if bowl and not (bat and bat != "runs" and not _has(text, "bowler", "bowlers")):
            return [{"name": "bowling_leaders", "args": {"sort": bowl, **team, **year}}]
        if bat:
            return [{"name": "batting_leaders", "args": {"sort": bat, **team, **year}}]
    if len(teams) == 2 and _has(text, "head to head", "head-to-head", "h2h", "record", "how many times",
                                "rivalry", "won more", "against"):
        return [{"name": "head_to_head", "args": {"team1": teams[0], "team2": teams[1], **year}}]
    ground = next((g for g in sorted(GROUND_ALIASES, key=len, reverse=True) if _has(text, g)), "")
    if ground and _has(text, "pitch", "conditions", "ground report", "report", "favour", "favor", "spin", "spinners",
                       "pace", "seamers", "par", "nature", "character", "slow", "flat", "batting friendly",
                       "bowling friendly", "how does it play", "play like"):
        return [{"name": "ground_report", "args": {"name": GROUND_ALIASES[ground], **year}}]
    if _has(text, "stadium", "ground", "venue", "pitch at", "chasing at", "batting first at", *GROUND_ALIASES) \
            and _has(text, "average", "score", "chasing", "chase", "bat first", "batting first", "total", "stats", "record"):
        ground = next((g for g in sorted(GROUND_ALIASES, key=len, reverse=True) if _has(text, g)), "")
        return [{"name": "venue_stats", "args": {"name": GROUND_ALIASES.get(ground, ground), **year}}]
    player_words = ("stats", "record", "career", "how many", "runs", "wickets", "wicket", "perform", "performed",
                    "season", "seasons", "strike rate", "sr", "economy", "average", "profile", "bowling", "batting",
                    "numbers", "how did", "do in", "fared", "form", "tally", "count", "scalps", "dismissed", "score",
                    "scores", "how fast", "total", "aggregate", *BAT_WORDS, *BOWL_WORDS)
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
