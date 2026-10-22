"""Planner side of the slice compiler (backend/db/analytics.py).

Three jobs, all driven by one reading of the question's wording (`parse_filters`):

1. Rules: route questions with filters the older tools lack (chasing, finals, won matches, after
   winning the toss, at a ground, required rate, wickets in hand, a batter's first N balls,
   targets) to `slice_stats`, and the new families (hat-tricks, awards, toss, impact subs,
   partnerships) to their tools.
2. Definitions: evaluative words with no single metric ("best finisher", "biggest hitter") get an
   explicit, stated definition instead of a silent guess.
3. Coverage guard: every filter the wording asks for must be in the executed plan. A slice call
   gets the missing filter; a leaderboard, phase, over-range, profile or matchup call is rewritten
   as a slice that has it; anything else reports the filter as a gap instead of answering a
   nearby question (the "in finals" bug, in general form).
"""

from __future__ import annotations

import re
from typing import Any

from backend.db import analytics

# ----------------------------------------------------------------- tool schemas (kept short: Groq counts tokens)

def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []}}}


_YEAR = {"type": "string"}          # the planner prompt explains years, teams and names once
_TEAM = {"type": "string"}
_PLAYER = {"type": "string"}

SLICE_TOOLS = [
    _fn("slice_stats", "Any metric on any slice of balls when no other tool has the filter. split compares; "
        "adjust = vs league that season; shrink = sample-adjusted rank.", {
            "subject": {"type": "string", "enum": ["batter", "bowler", "batting_team", "bowling_team", "league"]},
            "metric": {"type": "string", "enum": sorted(analytics.METRICS)},
            "player": {"type": "array", "items": _PLAYER}, "vs": _PLAYER, "team": _TEAM, "opp": _TEAM,
            "overs": {"type": "string", "description": "e.g. 17-20"},
            "phase": {"type": "string", "enum": ["powerplay", "middle", "death"]},
            "innings": {"type": "integer", "enum": [1, 2]}, "stage": {"type": "string", "enum": ["final", "playoff"]},
            "result": {"type": "string", "enum": ["won", "lost"]}, "toss": {"type": "string", "enum": ["won", "lost"]},
            "venue": {"type": "string"}, "target_min": {"type": "integer"}, "target_max": {"type": "integer"},
            "rrr_min": {"type": "number"}, "rrr_max": {"type": "number"},
            "wickets_in_hand": {"type": "string", "description": "e.g. 0-3"},
            "batter_balls": {"type": "string", "description": "e.g. 1-10"},
            "split": {"type": "string", "enum": sorted(analytics.SPLITS)},
            "adjust": {"type": "boolean"}, "shrink": {"type": "boolean"},
            "order": {"type": "string", "enum": ["best", "worst"]}, "min_balls": {"type": "integer"}, "year": _YEAR},
        ["subject", "metric"]),
    _fn("hattricks", "Every hat-trick; who has the most.", {"team": _TEAM, "year": _YEAR}),
    _fn("toss_stats", "Toss decisions and how often the toss winner won: overall, or by season, venue or team.",
        {"by": {"type": "string", "enum": ["season", "venue", "team"]}, "venue": {"type": "string"}, "year": _YEAR}),
    _fn("awards", "Player-of-the-match awards: the leaders, or one player's count.",
        {"player": _PLAYER, "team": _TEAM, "year": _YEAR}),
    _fn("impact_subs", "Impact-player substitutes (2023 on): most used, and their runs or wickets.",
        {"team": _TEAM, "sort": {"type": "string", "enum": ["times", "runs", "wkts"]}, "year": _YEAR}),
    _fn("partnerships", "Highest partnerships, or most runs by a pair (by=pair); wicket 1 = opening stand.",
        {"wicket": {"type": "integer"}, "by": {"type": "string", "enum": ["single", "pair"]}, "team": _TEAM,
         "innings": {"type": "integer", "enum": [1, 2]}, "year": _YEAR}),
]
SLICE_TOOL_NAMES = {t["function"]["name"] for t in SLICE_TOOLS}

PLANNER_HINTS = """Filters other tools lack -> slice_stats(subject, metric, filters): Kohli in finals; economy after winning the toss;
runs at Wankhede; strike rate chasing 180+ (target_min=180); required rate above 12 (rrr_min=12); first 10 balls (batter_balls=1-10).
Kohli chasing vs batting first -> slice_stats(subject=batter, metric=average, player=[Virat Kohli], split=innings).
"best"/"most effective" (a judgment) -> shrink=true; "highest"/"most" (literal) -> no shrink. Era-relative -> adjust=true.
hat-tricks -> hattricks | player of the match -> awards | toss -> toss_stats | impact player -> impact_subs | partnerships, opening pairs -> partnerships."""

# ----------------------------------------------------------------- reading the wording

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
N = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
EXTENDED = {"result", "toss", "rrr_min", "rrr_max", "wickets_in_hand", "batter_balls", "target_min", "target_max",
            "stage", "venue", "opp"}
WORDS = {"overs": "these specific overs", "phase": "this phase", "innings": "one innings (batting first or chasing)",
         "stage": "finals or playoffs only", "result": "only matches won or lost", "toss": "the toss result",
         "venue": "one ground", "target_min": "the size of the target", "target_max": "the size of the target",
         "rrr_min": "the required rate", "rrr_max": "the required rate", "wickets_in_hand": "wickets in hand",
         "batter_balls": "a batter's first balls", "opp": "one opposition team"}


def _n(token: str) -> int:
    return NUM.get(token) or int(token)


def parse_filters(question: str) -> dict[str, Any]:
    """The slice filters the wording asks for. Years and teams are grounded elsewhere."""
    from . import stats as S
    text = question.casefold().replace("–", "-")
    f: dict[str, Any] = {}
    if (m := re.search(rf"\b(?:last|final|closing)\s+{N}\s+overs\b", text)):
        f["overs"] = f"{21 - _n(m.group(1))}-20"
    elif (m := re.search(rf"\b(?:first|opening)\s+{N}\s+overs\b", text)):
        f["overs"] = f"1-{_n(m.group(1))}"
    elif (m := re.search(r"\bovers?\s*(\d{1,2})\s*(?:-|to|and)\s*(\d{1,2})\b", text)):
        f["overs"] = f"{m.group(1)}-{m.group(2)}"
    elif (m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\s+over\b", text)):
        f["overs"] = m.group(1)
    elif re.search(r"\b(?:first|opening)\s+over\b", text):
        f["overs"] = "1"
    elif re.search(r"\b(?:last|final)\s+over\b", text):
        f["overs"] = "20"
    if f.get("overs") in ("1-6", "7-15", "16-20"):
        f["phase"] = {"1-6": "powerplay", "7-15": "middle", "16-20": "death"}[f.pop("overs")]
    elif "overs" not in f and (ph := S._phase_in(text.replace("-", " "))):
        f["phase"] = ph
    if re.search(r"\b(successful|winning|won)\s+chases?\b|\bchasing successfully\b|\bchases? (?:that were )?won\b", text):
        f.update(innings=2, result="won")
    elif re.search(r"\b(failed|unsuccessful|losing|lost)\s+chases?\b", text):
        f.update(innings=2, result="lost")
    else:
        f.update(S._innings_of(text))
    if "result" not in f:
        if re.search(r"\bin (?:wins|victories|winning causes?|won matches|matches (?:they|he|we) won|a win)\b"
                     r"|\bwhen (?:they|he|the team) won\b", text):
            f["result"] = "won"
        elif re.search(r"\bin (?:losses|defeats|losing causes?|lost matches|matches (?:they|he) lost|a loss|a defeat)\b"
                       r"|\bin a losing cause\b|\bwhen (?:they|he|the team) lost\b", text):
            f["result"] = "lost"
    if re.search(r"\b(?:winning|won|wins|win) the toss\b", text):
        f["toss"] = "won"
    elif re.search(r"\b(?:losing|lost|loses|lose) the toss\b", text):
        f["toss"] = "lost"
    f.update(S._stage_of(text))
    for alias in sorted(S.GROUND_ALIASES, key=len, reverse=True):
        if re.search(rf"\b(?:at|in)\s+(?:the\s+)?{re.escape(alias)}\b", text):
            f["venue"] = alias
            break
    if (m := re.search(r"\b(?:required|asking)\s+(?:run\s+)?rate\s+(?:is\s+|was\s+)?"
                       r"(above|over|more than|greater than|of more than|exceeding|>|below|under|less than|<)\s*(\d{1,2}(?:\.\d+)?)", text)):
        f["rrr_max" if m.group(1) in ("below", "under", "less than", "<") else "rrr_min"] = float(m.group(2))
    if (m := re.search(rf"\b{N}\s+(?:or\s+(fewer|less|more)\s+)?wickets?\s+(?:in hand|left|remaining)\b", text)):
        k = _n(m.group(1))
        f["wickets_in_hand"] = f"0-{k}" if m.group(2) in ("fewer", "less") else f"{k}-10" if m.group(2) == "more" else f"{k}-{k}"
    if (m := re.search(rf"\bfirst\s+{N}\s+(?:balls|deliveries)\b", text)):
        f["batter_balls"] = f"1-{_n(m.group(1))}"
    if (m := re.search(r"\b(?:chasing|chases?|chased|targets?)\s+(?:of\s+)?(above|over|more than|at least|under|below|less than)?\s*"
                       r"(\d{3})\s*(\+|plus|or more|and above|or less|or fewer|and below)?", text)):
        qual, value, tail = m.group(1), int(m.group(2)), m.group(3)
        if qual in ("under", "below", "less than"):
            f["target_max"] = value - 1
        elif tail in ("or less", "or fewer", "and below"):
            f["target_max"] = value
        else:
            f["target_min"] = value + 1 if qual in ("above", "over", "more than") else value
        f.setdefault("innings", 2)
    roles = _team_roles(text, S._teams(text))
    stat_words = r"\b(batters?|bowlers?|batsm[ae]n|players?|runs|wickets|strike rate|economy|averages?|sixes|fours|scored|scorers?|taken)\b"
    # Two teams and no player or metric words is a rivalry question ("KKR against MI: who has won more?").
    about_stats = re.search(stat_words, text) or (not roles.get("team") and re.search(r"\b(who|whose)\b", text))
    if roles.get("opp") and about_stats and not re.search(r"\bhead[- ]to[- ]head\b|\brecord\b|\bresults?\b|\brivalry\b"
                                                         r"|\bwon more\b|\bwins? against\b|\bbeaten\b|\bbeat\b|\bleads?\b", text):
        f["opp"] = roles["opp"]                           # "most runs against CSK" (not "for CSK")
    split = parse_split(text)
    for key in {"innings": ("innings",), "result": ("result",), "toss": ("toss",), "phase": ("phase", "overs"),
                "venue": ("venue",)}.get(split or "", ()):
        f.pop(key, None)                                  # "chasing vs batting first" compares, it does not filter
    return f


SPLIT_WORDS = [
    (r"\bchasing (?:vs\.?|versus|or|and|compared (?:to|with)) batting first\b|\bbatting first (?:vs\.?|versus|or|and|compared (?:to|with)) chasing\b"
     r"|\bbatting first and second\b|\bfirst (?:vs|or|and) second innings\b", "innings"),
    (r"\b(?:by|each|every|per) season\b|\bseason by season\b|\bover the (?:years|seasons)\b|\btrend\b|\bevery year\b|\beach year\b", "season"),
    (r"\b(?:by|each|every|per) phase\b|\bacross (?:the )?phases\b", "phase"),
    (r"\b(?:by|each|every|per) (?:venue|ground)\b|\bat each (?:venue|ground)\b", "venue"),
    (r"\bagainst (?:each|every) (?:team|side|opposition)\b|\bby opposition\b", "opposition"),
    (r"\b(?:wins? (?:vs\.?|versus|and|or) (?:losses|defeats))\b|\bwhen (?:they|he) (?:win|won) (?:vs|and|or) (?:lose|lost)\b", "result"),
]


def parse_split(text: str) -> str | None:
    return next((name for pattern, name in SPLIT_WORDS if re.search(pattern, text)), None)


BOWL_WORDS = re.compile(r"\b(bowlers?|bowled|economy|economical|wickets?|scalps|conceded|conceding|spells?|dot balls bowled)\b")
TEAM_WORDS = re.compile(r"\b(teams?|sides?|franchises?)\b")
LEAGUE_WORDS = re.compile(r"\b(league|overall|across the ipl|in the ipl overall|ipl average|league average|scoring (?:changed|grown|risen))\b")


def subject_of(text: str, players: list[Any], teams: list[str]) -> str:
    plain = re.sub(r"\b(bowling|fielding|batting) (first|second)\b", " ", text)
    bowl = bool(BOWL_WORDS.search(plain)) and not re.search(r"\b(batters?|batsm[ae]n|scored|runs scored)\b", plain)
    if players:
        return "bowler" if bowl else "batter"
    if (TEAM_WORDS.search(plain) or re.search(r"\b(?:team )?totals?\b", plain)) \
            and not re.search(r"\b(batters?|batsm[ae]n|bowlers?|players?)\b", plain):
        return "bowling_team" if bowl else "batting_team"
    who = re.search(r"\b(who|whose|which (?:batter|bowler|player)s?|batters?|bowlers?|batsm[ae]n)\b", plain)
    if not who and (LEAGUE_WORDS.search(plain) or parse_split(plain) == "season"
                    or re.search(r"\b(changed|trend|grown|risen|fallen|evolved)\b", plain)
                    or re.search(r"\b(?:run|scoring) rate\b|\bruns per over\b", plain)):
        return "league"
    return "bowler" if bowl else "batter"


def explicit_metric(text: str, subject: str) -> str | None:
    """The metric the wording names, or None when it names none (so a converted call keeps its own)."""
    metric = metric_of(text, subject, default=None)
    return metric


def metric_of(text: str, subject: str, default: str | None = "auto") -> str | None:
    rules = [
        (r"\bballs per boundary\b", "balls_per_boundary"), (r"\bboundary (?:%|percent|percentage|rate)\b", "boundary_pct"),
        (r"\bdot[- ]ball (?:%|percent|percentage)\b|\bdot balls?\b|\bdot %", "dot_pct"),
        (r"\bsixes per\b|\bsix (?:rate|frequency|%|percentage)\b|\bsix[- ]hitting rate\b", "six_pct"),
        (r"\beconomy\b|\beconomical\b|\bruns per over\b", "economy"),
        (r"\bstrike[- ]rate\b|\bscoring rate\b|\b(?:fastest|quickest) scor|\bscor\w* (?:the )?(?:fastest|quickest)\b"
         r"|\b(?:fastest|quickest)\b(?! (?:to|ball|delivery|fifty|50|hundred|century))", "strike_rate"),
        (r"\brun[- ]rate\b", "run_rate"), (r"\baverages?\b", "average"),
        (r"\bsixes\b|\bmaximums\b|\bsix\b", "sixes"), (r"\bfours\b|\bboundaries\b", "fours"),
        (r"\bwickets?\b|\bscalps\b|\bdismissals\b", "wickets"), (r"\bruns\b|\bscored\b|\bscorers?\b", "runs"),
    ]
    if re.search(r"\bhighest (?:individual |team )?(?:score|scores|totals?|knocks?|innings)\b|\bbiggest (?:score|total|knock)s?\b"
                 r"|\bbest (?:individual )?(?:score|innings|knock)s?\b", text) and subject in ("batter", "batting_team"):
        return "high_score"
    metric = next((m for p, m in rules if re.search(p, text)), None)
    if default is None and metric is None and not re.search(
            r"\bhighest (?:individual |team )?(?:score|scores|totals?|knocks?|innings)\b|\bbiggest (?:score|total|knock)s?\b"
            r"|\bbest (?:individual )?(?:score|innings|knock)s?\b", text):
        return None
    if subject in ("bowler", "bowling_team"):
        metric = analytics.BOWLER_ALIASES.get(metric or "", metric)
        if re.search(r"\bstrike[- ]rate\b", text):
            metric = "bowling_strike_rate"
        return metric if metric in analytics.SIDE_METRICS[subject] else ("economy" if subject == "bowling_team" else "wickets")
    if subject in ("batting_team", "league") and metric in (None, "strike_rate"):
        return "run_rate"
    return metric if metric in analytics.SIDE_METRICS[subject] else "runs"


def order_of(text: str, metric: str) -> str:
    higher = analytics.METRICS[metric][3]
    if re.search(r"\b(worst|poorest)\b", text):
        return "worst"
    if re.search(r"\b(lowest|least|fewest|slowest)\b", text):
        return "worst" if higher else "best"
    return "best"


def _team_roles(text: str, teams: list[str]) -> dict[str, str]:
    """'for MI' / 'MI batters' -> team; 'against CSK' / 'vs CSK' -> opp."""
    from . import stats as S
    names = {code: [code.casefold()] + [n for n, c in S.TEAM_NAMES.items() if c == code] for code in teams}
    cut = re.search(r"\b(?:against|vs\.?|versus|facing)\b", text)
    out: dict[str, str] = {}
    for code, aliases in names.items():
        pos = min((m.start() for a in aliases for m in re.finditer(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text)), default=-1)
        role = "opp" if cut and pos > cut.start() else "team"
        out.setdefault(role, code)
    return out


def _evaluative(text: str) -> bool:
    return bool(re.search(r"\b(best|better|most effective|greatest|finest|most valuable|most dangerous)\b", text))


# ----------------------------------------------------------------- definitions for evaluative words

DEFINITIONS = [
    (r"\b(?:best|greatest|top|ipl's best|ideal)\s+finishers?\b|\bfinishers?\b",
     "Finisher = batting in overs 16-20 of chases that were won. Ranked by strike rate, shrunk toward the league "
     "so a short hot streak cannot top it; average in the same situation is the second view.",
     [{"subject": "batter", "metric": "strike_rate", "overs": "16-20", "innings": 2, "result": "won", "shrink": True,
       "min_balls": 60},
      {"subject": "batter", "metric": "average", "overs": "16-20", "innings": 2, "result": "won", "min_balls": 60}]),
    (r"\b(?:biggest|best|cleanest|most destructive)\s+(?:six[- ]hitters?|hitters?|power[- ]hitters?)\b|\bpower[- ]hitters?\b",
     "Six-hitting = sixes per 100 balls faced, shrunk toward the league (min 120 balls); total sixes is the second view.",
     [{"subject": "batter", "metric": "six_pct", "shrink": True, "min_balls": 120},
      {"subject": "batter", "metric": "sixes"}]),
    (r"\bunder pressure\b|\bpressure (?:chases|situations)\b|\bin (?:tight|tough) chases\b",
     "Pressure = chasing when the required rate is above 10 an over. Ranked by strike rate, shrunk toward the league "
     "(min 60 balls); average is the second view.",
     [{"subject": "batter", "metric": "strike_rate", "innings": 2, "rrr_min": 10, "shrink": True, "min_balls": 60},
      {"subject": "batter", "metric": "average", "innings": 2, "rrr_min": 10, "min_balls": 60}]),
]


def definition_calls(text: str, year: dict[str, str], extra: dict[str, Any]) -> list[dict[str, Any]]:
    for pattern, definition, specs in DEFINITIONS:
        if re.search(pattern, text):
            return [{"name": "slice_stats", "args": {**spec, **extra, **year}, "definition": definition} for spec in specs]
    return []


# ----------------------------------------------------------------- keyword rules

def family_calls(text: str, teams: list[str], players: list[Any], year: dict[str, str]) -> list[dict[str, Any]]:
    from . import stats as S
    team = {"team": teams[0]} if teams else {}
    if re.search(r"\bhat[- ]?tricks?\b", text):
        return [{"name": "hattricks", "args": {**team, **year}}]
    if re.search(r"\b(?:man|player) of the match\b|\bpotm\b|\bmotm\b|\bpom awards?\b", text):
        return [{"name": "awards", "args": {**({"player": players[0].name} if players else team), **year}}]
    if re.search(r"\bimpact (?:player|sub|substitute)s?\b", text):
        sort = "runs" if re.search(r"\bruns\b|\bscored\b", text) else "wkts" if re.search(r"\bwickets?\b", text) else "times"
        return [{"name": "impact_subs", "args": {**team, "sort": sort, **year}}]
    if re.search(r"\bpartnerships?\b|\bstands?\b|\bopening (?:pair|pairs|duo|combination)s?\b|\bbatting pairs?\b|\bduos?\b", text):
        args: dict[str, Any] = {**team, **year}
        if re.search(r"\bopening\b|\bfirst wicket\b|\b1st wicket\b", text):
            args["wicket"] = 1
        elif (m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th) wicket\b", text)):
            args["wicket"] = int(m.group(1))
        if re.search(r"\bpairs?\b|\bduos?\b|\btogether\b|\bcombinations?\b|\bmost runs\b", text):
            args["by"] = "pair"
        args.update(S._innings_of(text))
        return [{"name": "partnerships", "args": args}]
    if re.search(r"\btoss\b", text) and not re.search(
            r"\b(runs|wickets|economy|strike rate|average|run rate|sixes|batters?|bowlers?|who)\b", text):
        args = dict(year)
        venue = next((a for a in sorted(S.GROUND_ALIASES, key=len, reverse=True)
                      if re.search(rf"\b(?:at|in)\s+(?:the\s+)?{re.escape(a)}\b", text)), None)
        if venue:
            args["venue"] = venue
        elif re.search(r"\b(?:venues?|grounds?|stadiums?)\b", text):
            args["by"] = "venue"
        if re.search(r"\b(?:by|each|every|per) season\b|\bover the years\b|\btrend\b|\beach year\b", text):
            args["by"] = "season"
        elif teams or re.search(r"\b(?:which|each|every) teams?\b", text):
            args["by"] = "team"
        return [{"name": "toss_stats", "args": args}]
    return []


def slice_rule(question: str, players: list[Any], teams: list[str], year: dict[str, str]) -> list[dict[str, Any]]:
    """A slice when the wording needs a filter, split or adjustment that no older tool has."""
    text = question.casefold().replace("-", " ").replace("–", " ")
    raw = question.casefold()
    f = parse_filters(raw)
    split = parse_split(text)
    adjust = bool(re.search(r"\bera\b|\badjusted\b|\brelative to (?:the )?(?:league|era|season|average)\b|\bahead of (?:his|their) time\b", text))
    roles = _team_roles(text, teams)
    about_players = bool(players) or bool(re.search(r"\b(who|whose|which (?:batter|bowler|player)s?|batters?|bowlers?"
                                                    r"|batsm[ae]n|players?)\b", text))
    keys = set(f) & EXTENDED
    if "venue" in keys and not about_players:
        keys.discard("venue")                             # a ground's scoring, totals, par: venue_stats / ground_report
    subject_guess = subject_of(text, players, teams)
    innings_level = metric_of(text, subject_guess) == "high_score" and bool(f)
    if not (keys or split or adjust or innings_level):
        return []
    if set(f) <= {"stage", "innings"} and not players and not _evaluative(text) and \
            not re.search(r"\b(most|highest|top|who|which (?:batter|bowler|player))\b", text):
        return []                                         # match_results and the match path handle the rest
    subject = subject_of(text, players, teams)
    metric = metric_of(text, subject)
    if split and players and metric in ("runs", "wickets") and not re.search(r"\b(runs|wickets)\b", text):
        metric = "average" if subject == "batter" else "economy"   # a comparison wants a rate, not a total
    args: dict[str, Any] = {"subject": subject, "metric": metric, **f, **year, **roles}
    if players:
        vs = len(players) >= 2 and re.search(r"\b(?:against|vs\.?|versus|facing|to)\b", text)
        args["player"] = [players[0].name] if vs else [p.name for p in players[:3]]
        if vs:
            args["vs"] = players[1].name
    if split:
        args["split"] = split
    if adjust and analytics.METRICS[metric][1]:
        args["adjust"] = True
    if _evaluative(text) and not players and analytics.METRICS[metric][1] == "B":
        args["shrink"] = True
    args["order"] = order_of(text, metric)
    if (m := re.search(r"\bmin(?:imum)?\.?\s+(?:of\s+)?(\d{2,4})\s+balls\b", text)):
        args["min_balls"] = int(m.group(1))
    return [{"name": "slice_stats", "args": args}]


# ----------------------------------------------------------------- coverage guard

# Filters each tool honours (or answers by construction: a ground report is about one ground).
CAPS: dict[str, set[str]] = {
    "slice_stats": set(WORDS),
    "over_range_leaders": {"overs", "innings", "phase"},
    "phase_leaders": {"phase"},
    "match_results": {"stage", "innings", "target_min", "target_max", "overs", "result"},
    "venue_stats": {"venue", "innings"}, "ground_report": {"venue", "phase", "innings"},
    "head_to_head": {"innings", "opp"}, "team_seasons": {"stage", "innings"},
    "batter_vs_bowler": set(), "player_profile": {"phase"},
    "toss_stats": {"toss", "venue"}, "partnerships": {"innings"},
    "champions": {"stage"},
}
BAT_SORT = {"runs": "runs", "sr": "strike_rate", "avg": "average", "sixes": "sixes", "fours": "fours"}
BOWL_SORT = {"wkts": "wickets", "econ": "economy", "avg": "bowling_average", "sr": "bowling_strike_rate"}


def _as_slice(call: dict[str, Any], question: str) -> dict[str, Any] | None:
    """The same question as a slice, when the call's tool maps onto a subject and metric."""
    args = dict(call.get("args") or {})
    text = question.casefold()
    year = {"year": args["year"]} if args.get("year") else {}
    name = call["name"]
    if name == "batting_leaders" and (args.get("sort") or "runs") in BAT_SORT:
        return {"subject": "batter", "metric": BAT_SORT[args.get("sort") or "runs"],
                **({"team": args["team"]} if args.get("team") else {}), **year}
    if name == "bowling_leaders" and (args.get("sort") or "wkts") in BOWL_SORT:
        return {"subject": "bowler", "metric": BOWL_SORT[args.get("sort") or "wkts"],
                **({"team": args["team"]} if args.get("team") else {}), **year}
    if name == "phase_leaders":
        bowl = args.get("role") == "bowl"
        return {"subject": "bowler" if bowl else "batter", "metric": "economy" if bowl else "strike_rate",
                "phase": args.get("phase"), **year}
    if name == "over_range_leaders":
        bowl = args.get("role") == "bowl"
        sort = {"sr": "strike_rate", "runs": "runs", "econ": "economy", "wkts": "wickets"}.get(
            args.get("sort") or ("econ" if bowl else "sr"), "economy" if bowl else "strike_rate")
        lo, hi = args.get("from_over"), args.get("to_over") or args.get("from_over")
        return {"subject": "bowler" if bowl else "batter", "metric": sort,
                **({"overs": f"{lo}-{hi}"} if lo else {}), **({"innings": args["innings"]} if args.get("innings") else {}), **year}
    if name == "records":
        subject = "batting_team" if re.search(r"\btotals?\b|\bteam\b", text) else "batter"
        if metric_of(text, subject) == "high_score":
            return {"subject": subject, "metric": "high_score", **year}
        return None
    if name == "season_summary":
        return {"subject": "league", "metric": metric_of(text, "league"), **year}
    if name == "player_profile" and args.get("name"):
        subject = "bowler" if BOWL_WORDS.search(re.sub(r"\b(bowling|batting) (first|second)\b", " ", text)) else "batter"
        return {"subject": subject, "metric": metric_of(text, subject), "player": [args["name"]], **year}
    if name == "batter_vs_bowler" and args.get("batter") and args.get("bowler"):
        metric = metric_of(text, "batter")
        return {"subject": "batter", "metric": "strike_rate" if metric == "runs" and "runs" not in text else metric,
                "player": [args["batter"]], "vs": args["bowler"], **year}
    return None


def missing(question: str, call: dict[str, Any], f: dict[str, Any] | None = None) -> dict[str, Any]:
    f = parse_filters(question) if f is None else f
    have = dict(call.get("args") or {})
    caps = CAPS.get(call["name"], set())
    out = {}
    for key, value in f.items():
        if key not in caps:
            out[key] = value
        elif call["name"] == "slice_stats" and have.get(key) in (None, ""):
            out[key] = value                              # a slice must carry it, not just be able to
    return out


ERA = re.compile(r"\bera\b|\badjusted\b|\brelative to (?:the )?(?:league|era|season|average)\b|\bahead of (?:his|their) time\b")
LEADER_TOOLS = ("phase_leaders", "over_range_leaders", "batting_leaders", "bowling_leaders")


def ground_slice(args: dict[str, Any], question: str) -> dict[str, Any]:
    """Keep only what the wording supports: a planner's split, era adjustment or aggregate metric must be asked for."""
    text = question.casefold()
    args = dict(args)
    for key in ("adjust", "shrink"):
        if isinstance(args.get(key), str):
            args[key] = args[key].strip().casefold() == "true"     # some models send "true"
    if args.get("split") and parse_split(text.replace("-", " ")) != args["split"]:
        args.pop("split")                                 # "most runs in finals" is not a split by innings
    if args.get("adjust") and not ERA.search(text):
        args.pop("adjust")
    if args.get("subject") in ("batter", "batting_team") and metric_of(text, args["subject"]) == "high_score":
        args["metric"] = "high_score"                     # "highest total while chasing" is one innings, not a sum
    return args


def enforce_coverage(calls: list[dict[str, Any]], question: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Repair calls so every filter the wording asks for is applied; report the ones no tool can apply."""
    f = parse_filters(question)
    text = question.casefold()
    fixed = []
    for call in calls:
        if call["name"] == "slice_stats":
            call = {**call, "args": ground_slice(call.get("args") or {}, question)}
        elif call["name"] in LEADER_TOOLS and subject_of(text, [], []) in ("batting_team", "bowling_team", "league") \
                and (converted := _as_slice(call, question)) is not None:
            subject = subject_of(text, [], [])            # "run rate in the death overs" is the league, not batters
            converted.update(subject=subject, metric=metric_of(text, subject))
            call = {"name": "slice_stats", "args": {**converted, **f}, "converted_from": call["name"]}
        fixed.append(call)
    calls = fixed
    if not f or not calls:
        return calls, []
    out, gaps = [], []
    for call in calls:
        lack = missing(question, call, f)
        if not lack:
            out.append(call)
            continue
        if call["name"] == "slice_stats":
            out.append({**call, "args": {**(call.get("args") or {}), **lack}})
            continue
        converted = _as_slice(call, question)
        if converted is not None and call["name"] in ("phase_leaders", "over_range_leaders", "batting_leaders", "bowling_leaders"):
            subject = subject_of(question.casefold(), [], [])
            if subject in ("batting_team", "bowling_team", "league"):
                converted.update(subject=subject, metric=metric_of(question.casefold(), subject))
            elif (named := explicit_metric(question.casefold(), converted["subject"])):
                converted["metric"] = named               # "most sixes in the powerplay": sixes, not the tool's default
        if converted is not None:
            new = {"name": "slice_stats", "args": {**converted, **f}, "converted_from": call["name"]}
            if new["args"]["metric"] not in analytics.SIDE_METRICS[new["args"]["subject"]]:
                new["args"]["metric"] = metric_of(question.casefold(), new["args"]["subject"])
            out.append(new)
            continue
        out.append(call)
        gaps.extend(WORDS[k] for k in lack if WORDS[k] not in gaps)
    return out, gaps


def coverage_gaps(question: str, calls: list[dict[str, Any]]) -> list[str]:
    """Filters the wording asks for that the final plan still does not apply (for the gap reply)."""
    f = parse_filters(question)
    if not f or not calls:
        return []
    labels: list[str] = []
    for call in calls:
        for key in missing(question, call, f):
            if WORDS[key] not in labels and call["name"] not in ("slice_stats",):
                labels.append(WORDS[key])
    return labels


# ----------------------------------------------------------------- tool calls -> argv

def to_argv(name: str, args: dict[str, Any], index: Any) -> list[str]:
    from . import stats as S
    if name == "slice_stats":
        subject = args.get("subject") if args.get("subject") in analytics.SIDES else "batter"
        metric = args.get("metric")
        if subject in ("bowler", "bowling_team"):
            metric = analytics.BOWLER_ALIASES.get(metric, metric)
        if metric not in analytics.SIDE_METRICS[subject]:
            raise S.PlanError(f"metric {metric!r} does not fit subject {subject}")
        argv = ["slice", "--subject", subject, "--metric", metric]
        players = args.get("player") or []
        if isinstance(players, str):
            players = [players]
        for p in players[:3]:
            argv += ["--player", S._player(p, index)[0] if index else str(p)]
        if args.get("vs"):
            argv += ["--vs", S._player(args["vs"], index)[0] if index else str(args["vs"])]
        for key in ("team", "opp"):
            if args.get(key):
                argv += [f"--{key}", S._team(args[key])]
        for key, pattern in (("overs", r"^\d{1,2}(-\d{1,2})?$"), ("wickets_in_hand", r"^\d{1,2}-\d{1,2}$"),
                             ("batter_balls", r"^\d{1,3}-\d{1,3}$")):
            if args.get(key) not in (None, ""):
                value = str(args[key]).replace(" ", "")
                if not re.match(pattern, value):
                    raise S.PlanError(f"{key} must look like {'17-20' if key == 'overs' else '1-10'}")
                argv += [f"--{key.replace('_', '-')}", value]
        for key, choices in (("phase", ("powerplay", "middle", "death")), ("stage", ("final", "playoff")),
                             ("result", ("won", "lost")), ("toss", ("won", "lost")), ("split", tuple(analytics.SPLITS)),
                             ("order", ("best", "worst"))):
            if args.get(key) in choices:
                argv += [f"--{key}", args[key]]
        if args.get("innings") in (1, 2, "1", "2"):
            argv += ["--innings", str(args["innings"])]
        if args.get("venue"):
            argv += ["--venue", S.GROUND_ALIASES.get(str(args["venue"]).casefold().strip(), str(args["venue"]))]
        for key, low, high in (("target_min", 1, 400), ("target_max", 1, 400), ("min_balls", 0, 5000)):
            if (v := S._int(args, key, low, high)) is not None:
                argv += [f"--{key.replace('_', '-')}", str(v)]
        for key in ("rrr_min", "rrr_max"):
            if args.get(key) not in (None, ""):
                argv += [f"--{key.replace('_', '-')}", str(float(args[key]))]
        if args.get("adjust") is True:
            argv.append("--adjust")
        if args.get("shrink") is True:
            argv.append("--shrink")
        return argv + S._year(args) + ["--limit", "25" if args.get("split") in ("season", "venue", "opposition") else "10"]
    team = ["--team", S._team(args["team"])] if args.get("team") else []
    if name == "hattricks":
        return ["hattricks", *team, *S._year(args)]
    if name == "toss_stats":
        by = ["--by", args["by"]] if args.get("by") in ("season", "venue", "team") else []
        venue = ["--venue", S.GROUND_ALIASES.get(str(args["venue"]).casefold().strip(), str(args["venue"]))] \
            if args.get("venue") else []
        return ["toss", *by, *venue, *S._year(args)]
    if name == "awards":
        player = ["--player", S._player(args["player"], index)[0] if index else str(args["player"])] if args.get("player") else []
        return ["awards", *player, *team, *S._year(args)]
    if name == "impact_subs":
        sort = ["--sort", args["sort"]] if args.get("sort") in ("times", "runs", "wkts") else []
        return ["impact", *team, *sort, *S._year(args)]
    if name == "partnerships":
        extra = []
        if (w := S._int(args, "wicket", 1, 10)):
            extra += ["--wicket", str(w)]
        if args.get("by") in ("single", "pair"):
            extra += ["--by", args["by"]]
        if args.get("innings") in (1, 2, "1", "2"):
            extra += ["--innings", str(args["innings"])]
        return ["partnerships", *extra, *team, *S._year(args)]
    raise S.PlanError(f"unknown tool {name}")


def key_column(result: dict[str, Any]) -> str | None:
    """The column a one-line answer leads with."""
    name, args = result["name"], result.get("args") or {}
    block = next((b for b in result.get("blocks", []) if b["type"] == "table"), None)
    cols = block["columns"] if block else []
    if name == "slice_stats":
        return next((c for c in cols if c.endswith("(shrunk)")), None) or next((c for c in cols if c.endswith(" index")), None) \
            or (analytics.METRICS.get(args.get("metric") or "", (None,) * 5)[4])
    return {"awards": "awards", "partnerships": "runs",
            "impact_subs": {"runs": "runs", "wkts": "wkts"}.get(args.get("sort") or "", "times_subbed_in"),
            "toss_stats": "toss_winner_won_pct"}.get(name)
