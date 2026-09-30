"""Canonical IPL archive and match-detail data access.

The UI consumes this module instead of depending on Cricinfo response shapes.  Every
provider call is best-effort and the public functions always return a stable contract.
"""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Any

from backend.apis.demo_data import demo_scorecard
from backend.src.impact_lab import impact_leaderboard, match_par, momentum_series, pitch_profile, turning_points
from backend.src.scorecard_helpers import extract_commentary_text, innings_team_name


IPL_SERIES = {
    2025: {"name": "IPL 2025", "slug": "ipl-2025-1449924"},
    2024: {"name": "Indian Premier League 2024", "slug": "indian-premier-league-2024-1410320"},
    2023: {"name": "Indian Premier League 2023", "slug": "indian-premier-league-2023-1345038"},
}

DEMO_MATCH_SLUG = "royal-challengers-bengaluru-vs-punjab-kings-final-1473511"


def _client():
    from cricdata import CricinfoClient

    return CricinfoClient()


def _first(*values: Any, default: Any = None) -> Any:
    return next((value for value in values if value not in (None, "", [])), default)


def _team(raw: dict[str, Any]) -> dict[str, Any]:
    team = raw.get("team") or raw.get("teamInfo") or raw
    score = raw.get("score") or raw.get("scoreText") or raw.get("displayScore")
    if not score and raw.get("runs") is not None:
        score = f"{raw['runs']}/{raw.get('wickets', 0)}"
    score_info = _first(raw.get("scoreInfo"), raw.get("scoreDetails"), raw.get("inningNumbers"), default="")
    if isinstance(score_info, list):
        score_info = ", ".join(str(item) for item in score_info)
    elif isinstance(score_info, dict):
        score_info = _first(score_info.get("displayValue"), score_info.get("text"), default="")
    return {
        "id": str(_first(team.get("id"), team.get("objectId"), default="")),
        "name": _first(team.get("longName"), team.get("name"), team.get("displayName"), default="Team"),
        "abbr": _first(team.get("abbreviation"), team.get("shortName"), team.get("name"), default="—"),
        "color": _first(team.get("color"), team.get("primaryColor"), default="#b8f34a"),
        "score": score or "—",
        "score_info": str(score_info),
    }


def normalize_match(raw: dict[str, Any], season: int, series_slug: str) -> dict[str, Any]:
    match_id = str(_first(raw.get("objectId"), raw.get("id"), default=""))
    slug = str(_first(raw.get("slug"), raw.get("matchSlug"), default=f"match-{match_id}"))
    if match_id and not slug.endswith(match_id):
        slug = f"{slug}-{match_id}"
    teams = [_team(item) for item in (raw.get("teams") or raw.get("teamInfo") or [])][:2]
    title = f"{teams[0]['abbr']} vs {teams[1]['abbr']}" if len(teams) == 2 else _first(raw.get("title"), raw.get("longName"), default="")
    ground = raw.get("ground") or raw.get("venue") or {}
    venue = _first(ground.get("longName"), ground.get("name"), raw.get("venueName"), default="Venue unavailable")
    status_text = _first(raw.get("statusText"), raw.get("result"), raw.get("status"), default="Status unavailable")
    return {
        "id": match_id or slug,
        "season": season,
        "series_slug": series_slug,
        "match_slug": slug,
        "title": title or "IPL match",
        "label": _first(raw.get("title"), raw.get("longName"), default="IPL match"),
        "date": _first(raw.get("startDate"), raw.get("startTime"), raw.get("date"), default=""),
        "start_time": _first(raw.get("startTime"), raw.get("startDate"), default=""),
        "status": _first(raw.get("state"), raw.get("status"), default="result"),
        "status_text": status_text,
        "venue": venue,
        "teams": teams,
        "series_name": IPL_SERIES.get(season, {}).get("name", f"IPL {season}"),
    }


def _demo_match() -> dict[str, Any]:
    return {
        "id": "1473511",
        "season": 2025,
        "series_slug": IPL_SERIES[2025]["slug"],
        "match_slug": DEMO_MATCH_SLUG,
        "title": "RCB vs PBKS",
        "label": "Final",
        "date": "2025-06-03T14:00:00Z",
        "start_time": "2025-06-03T14:00:00Z",
        "status": "result",
        "status_text": "RCB won by 6 runs",
        "venue": "Narendra Modi Stadium, Ahmedabad",
        "teams": [
            {"id": "335970", "name": "Royal Challengers Bengaluru", "abbr": "RCB", "color": "#e43f49", "score": "190/9", "score_info": "20 overs"},
            {"id": "335973", "name": "Punjab Kings", "abbr": "PBKS", "color": "#d64b6a", "score": "184/7", "score_info": "20 overs"},
        ],
        "series_name": "IPL 2025",
    }


@lru_cache(maxsize=8)
def _series_matches(series_slug: str) -> tuple[dict[str, Any], ...]:
    payload = _client().series_matches(series_slug)
    matches = payload.get("content", {}).get("matches") if isinstance(payload, dict) else payload
    if not matches and isinstance(payload, dict):
        matches = payload.get("matches")
    return tuple(matches or [])


def list_ipl_matches(season: int = 2025, query: str = "", limit: int = 24, offset: int = 0) -> dict[str, Any]:
    if season not in IPL_SERIES:
        raise ValueError(f"Unsupported IPL season: {season}")
    series_slug = IPL_SERIES[season]["slug"]
    provider = "cricdata / ESPNcricinfo"
    try:
        matches = [normalize_match(item, season, series_slug) for item in _series_matches(series_slug)]
    except Exception:
        matches = [_demo_match()] if season == 2025 else []
        provider = "local demo fallback"
    needle = query.strip().casefold()
    if needle:
        matches = [
            match for match in matches
            if needle in " ".join([
                match["title"], match["status_text"], match["venue"],
                match.get("label", ""),
                *(f"{team['name']} {team['abbr']}" for team in match["teams"]),
            ]).casefold()
        ]
    total = len(matches)
    page = matches[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
    return {
        "season": season,
        "series": IPL_SERIES[season],
        "query": query,
        "matches": page,
        "pagination": {"offset": max(0, offset), "limit": limit, "total": total, "has_more": offset + len(page) < total},
        "source": provider,
    }


def search_ipl_archive(query: str, limit: int = 12) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    years = [year for year in IPL_SERIES if str(year) in query] or list(IPL_SERIES)
    stopwords = {
        "a", "all", "and", "for", "find", "from", "in", "ipl", "match", "matches",
        "me", "of", "please", "season", "show", "the", "to", "versus", "vs",
        *(str(year) for year in IPL_SERIES),
    }
    tokens = [token for token in re.findall(r"[a-z0-9]+", query.casefold()) if token not in stopwords]
    for season in years:
        page = list_ipl_matches(season, query="", limit=100)
        for match in page["matches"]:
            searchable = " ".join([
                match["title"], match.get("label", ""), match["status_text"], match["venue"],
                *(f"{team['name']} {team['abbr']}" for team in match["teams"]),
            ]).casefold()
            if not tokens or all(token in searchable for token in tokens):
                results.append(match)
        if len(results) >= limit:
            break
    return results[:limit]


def _safe_call(client: Any, method: str, *args: str) -> tuple[Any, bool]:
    try:
        return getattr(client, method)(*args), True
    except Exception:
        return {}, False


def _player_name(row: dict[str, Any]) -> str:
    player = row.get("player") or row.get("athlete") or {}
    return str(_first(player.get("longName"), player.get("displayName"), player.get("name"), row.get("playerName"), default="Unknown"))


def _normalize_innings(scorecard: dict[str, Any]) -> list[dict[str, Any]]:
    raw_innings = scorecard.get("content", {}).get("innings", []) or []
    innings = []
    for number, inning in enumerate(raw_innings, 1):
        batters = []
        for row in inning.get("inningBatsmen", []) or []:
            if row.get("runs") is None:
                continue
            batters.append({
                "name": _player_name(row), "dismissal": _first(row.get("dismissalText"), row.get("dismissal"), default=""),
                "runs": row.get("runs", 0), "balls": row.get("balls", 0), "fours": row.get("fours", 0),
                "sixes": row.get("sixes", 0), "strike_rate": _first(row.get("strikerate"), row.get("strikeRate"), default="—"),
                "not_out": not bool(row.get("isOut", False)),
            })
        bowlers = []
        for row in inning.get("inningBowlers", []) or []:
            if row.get("overs") is None:
                continue
            bowlers.append({
                "name": _player_name(row), "overs": row.get("overs", "—"), "maidens": row.get("maidens", 0),
                "runs": _first(row.get("conceded"), row.get("runs"), row.get("runsConceded"), default="—"),
                "wickets": row.get("wickets", 0), "economy": row.get("economy", "—"), "dots": row.get("dots", 0),
            })
        innings.append({
            "number": number, "team": innings_team_name(inning), "runs": inning.get("runs", 0),
            "wickets": inning.get("wickets", 0), "overs": inning.get("overs", 0), "extras": inning.get("extras", 0),
            "batters": batters, "bowlers": bowlers,
        })
    return innings


def _normalize_commentary(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    comments = payload.get("content", {}).get("comments") or payload.get("comments") or []
    balls = []
    for index, row in enumerate(comments):
        over = _first(row.get("oversActual"), row.get("over"), row.get("overNumber"), default="")
        ball_number = row.get("ballNumber")
        label = str(over) if over != "" else ""
        if ball_number is not None and "." not in label:
            label = f"{label}.{ball_number}"
        total_runs = _first(row.get("totalRuns"), row.get("runs"), default=0)
        wicket = bool(row.get("isWicket") or row.get("dismissals"))
        event = "W" if wicket else "6" if row.get("isSix") else "4" if row.get("isFour") else str(total_runs)
        balls.append({
            "id": str(_first(row.get("id"), row.get("objectId"), default=f"ball-{index}")),
            "inning": int(_first(row.get("inningNumber"), row.get("inning"), default=1)),
            "over": _first(row.get("overNumber"), over, default=0), "ball": ball_number,
            "label": label or "—", "event": event, "runs": total_runs,
            "batter_runs": _first(row.get("batsmanRuns"), row.get("batterRuns"), default=0),
            "wicket": wicket, "boundary": bool(row.get("isFour") or row.get("isSix")),
            "title": row.get("title", ""), "text": extract_commentary_text(row) or row.get("text", ""),
            "score": row.get("score", ""), "win_probability": row.get("winProbability"),
        })
    def order(item: dict[str, Any]) -> tuple[int, float]:
        try:
            return item["inning"], float(item["label"])
        except (TypeError, ValueError):
            return item["inning"], 0.0
    return sorted(balls, key=order)


def _demo_balls() -> list[dict[str, Any]]:
    rows = [
        (2, "18.4", "4", 4, "Hazlewood to Iyer", "Driven through cover for four."),
        (2, "18.5", "W", 0, "Hazlewood to Iyer", "Caught at long-on. A decisive late wicket."),
        (2, "18.6", "1", 1, "Hazlewood to Wadhera", "Single to keep the strike moving."),
        (2, "19.1", "1", 1, "Krunal to Wadhera", "Worked to deep square leg."),
        (2, "19.2", "0", 0, "Krunal to Shashank", "Dot ball, squeezed back to the bowler."),
        (2, "19.3", "6", 6, "Krunal to Shashank", "Launched over long-on for six."),
        (2, "19.4", "W", 0, "Krunal to Shashank", "Taken in the deep. RCB close in."),
        (2, "19.5", "2", 2, "Krunal to Arshdeep", "Two runs into the gap."),
        (2, "19.6", "1", 1, "Krunal to Arshdeep", "A single ends the match; RCB win by six runs."),
    ]
    return [
        {"id": f"demo-{index}", "inning": inning, "over": int(label.split(".")[0]), "ball": int(label.split(".")[1]),
         "label": label, "event": event, "runs": runs, "batter_runs": runs, "wicket": event == "W",
         "boundary": event in {"4", "6"}, "title": title, "text": text, "score": "", "win_probability": None}
        for index, (inning, label, event, runs, title, text) in enumerate(rows)
    ]


def _analytics(scorecard: dict[str, Any]) -> dict[str, Any]:
    innings = scorecard.get("content", {}).get("innings", []) or []
    par = match_par(innings)
    return {
        "par": par,
        "impact": impact_leaderboard(innings, par)[:10] if par else [],
        "turning_points": turning_points(innings, top_n=5),
        "momentum": [{"team": innings_team_name(row), "values": [round(value, 2) for value in momentum_series(row)]} for row in innings],
        "pitch": pitch_profile(innings),
    }


def _match_from_detail(info: dict[str, Any], scorecard: dict[str, Any], series_slug: str, match_slug: str) -> dict[str, Any]:
    raw = scorecard.get("match") or info.get("match") or info
    season = next((year for year, item in IPL_SERIES.items() if item["slug"] == series_slug), 2025)
    match = normalize_match(raw, season, series_slug)
    match["match_slug"] = match_slug
    if not match["teams"]:
        match["teams"] = [_team({"team": row.get("team", {}), "runs": row.get("runs"), "wickets": row.get("wickets")}) for row in scorecard.get("content", {}).get("innings", [])[:2]]
    return match


def get_match_detail(series_slug: str, match_slug: str) -> dict[str, Any]:
    try:
        client = _client()
    except Exception:
        client = None
    info, info_ok = _safe_call(client, "match_info", series_slug, match_slug)
    scorecard, score_ok = _safe_call(client, "match_scorecard", series_slug, match_slug)
    commentary, commentary_ok = _safe_call(client, "match_commentary", series_slug, match_slug)
    ball_payload, ball_ok = _safe_call(client, "match_ball_by_ball", series_slug, match_slug)
    overs, overs_ok = _safe_call(client, "match_overs", series_slug, match_slug)
    partnerships, partnerships_ok = _safe_call(client, "match_partnerships", series_slug, match_slug)
    fall_of_wickets, fow_ok = _safe_call(client, "match_fall_of_wickets", series_slug, match_slug)

    if not score_ok or not scorecard:
        scorecard = demo_scorecard()
        match = _demo_match()
        match["series_slug"] = series_slug
        match["match_slug"] = match_slug
        source = "local demo fallback"
    else:
        match = _match_from_detail(info if isinstance(info, dict) else {}, scorecard, series_slug, match_slug)
        source = "cricdata / ESPNcricinfo"

    balls = _normalize_commentary(ball_payload) if ball_ok else []
    coverage = "full" if balls else ""
    if not balls:
        balls = _normalize_commentary(commentary)
        coverage = "commentary" if balls else ""
    if not balls and source == "local demo fallback":
        balls = _demo_balls()
        coverage = "demo"
    if not coverage:
        coverage = "overs-only" if overs_ok and overs else "unavailable"

    support = scorecard.get("content", {}).get("supportInfo", {}) or {}
    awards = [
        _player_name(row) for row in (support.get("playersOfTheMatch") or support.get("playersOfTheSeries") or [])
    ]
    return {
        "match": match,
        "innings": _normalize_innings(scorecard),
        "balls": balls,
        "ball_coverage": {
            "level": coverage,
            "label": {"full": "Full ball feed", "commentary": "Available commentary", "demo": "Demo fallback", "overs-only": "Over summaries only", "unavailable": "Unavailable"}[coverage],
            "note": "Ball data falls back from the dedicated feed to commentary, then over summaries; coverage can be partial for historical matches.",
        },
        "overs": overs if overs_ok else [],
        "partnerships": partnerships if partnerships_ok else [],
        "fall_of_wickets": fall_of_wickets if fow_ok else [],
        "toss": _first(info.get("tossText") if isinstance(info, dict) else None, info.get("toss") if isinstance(info, dict) else None, default="Unavailable"),
        "player_awards": awards,
        "analytics": _analytics(scorecard),
        "sources": [
            {"name": "Match info", "provider": source, "available": bool(info_ok or source.endswith("fallback"))},
            {"name": "Scorecard", "provider": source, "available": True},
            {"name": "Ball-by-ball", "provider": {"full": "dedicated feed", "commentary": "commentary fallback", "demo": source}.get(coverage, "unavailable"), "available": bool(balls)},
        ],
    }
