"""Canonical IPL archive and match-detail data access.

The UI consumes this module instead of depending on Cricinfo response shapes.  Every
provider call is best-effort and the public functions always return a stable contract.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import os
from pathlib import Path
import threading
import re
from typing import Any

from backend.apis.demo_data import demo_scorecard
from backend.src import cricsheet
from backend.src.cricsheet_scorecard import derive_scorecard, toss_text
from backend.src.ipl_teams import slugify, team_code, team_color
from backend.src.momentum_shifts import match_shifts
from backend.src.impact_lab import impact_leaderboard, match_par, momentum_series, pitch_profile, turning_points
from backend.src.scorecard_helpers import extract_commentary_text, innings_team_name


IPL_SERIES = {
    2025: {"name": "IPL 2025", "slug": "ipl-2025-1449924"},
    2024: {"name": "Indian Premier League 2024", "slug": "indian-premier-league-2024-1410320"},
    2023: {"name": "Indian Premier League 2023", "slug": "indian-premier-league-2023-1345038"},
}

COVERAGE_LABELS = {
    "cricsheet": "Every delivery (Cricsheet)", "full": "Full ball feed", "commentary": "Available commentary",
    "demo": "Demo fallback", "overs-only": "Over summaries only", "unavailable": "Unavailable",
}
COVERAGE_NOTES = {
    "cricsheet": "Every delivery from Cricsheet's structured record. Descriptions are generated from that record; there is no broadcast commentary text.",
    "full": "Every delivery from ESPN's play-by-play feed, with commentary text.",
    "commentary": "Reconstructed from Cricinfo commentary pages, which can be partial for older matches.",
    "demo": "Local sample deliveries while live providers are unavailable.",
    "overs-only": "Only over aggregates are available for this match.",
    "unavailable": "No delivery-level data is available; the scorecard remains usable.",
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


def all_seasons() -> list[int]:
    """Every season with data: the full Cricsheet archive plus ESPN-mapped seasons."""
    return sorted(set(cricsheet.seasons()) | set(IPL_SERIES), reverse=True)


def series_slug_for(season: int) -> str:
    return IPL_SERIES.get(season, {}).get("slug", f"ipl-{season}")


def cricsheet_match(entry: dict[str, Any]) -> dict[str, Any]:
    """Archive card for a Cricsheet match; slugs mirror Cricinfo's URL scheme."""
    season = entry.get("season") or 0
    totals = {row["team"]: row for row in entry.get("innings", [])}
    teams = []
    for name in entry.get("teams", [])[:2]:
        total = totals.get(name)
        teams.append({
            "id": slugify(name), "name": name, "abbr": team_code(name), "color": team_color(name),
            "score": f"{total['runs']}/{total['wickets']}" if total else "—",
            "score_info": f"{total['overs']:g} ov" if total else "",
        })
    names = entry.get("teams", ["", ""])
    stage = entry.get("stage", "")
    slug_parts = [slugify(names[0]), "vs", slugify(names[1] if len(names) > 1 else ""), slugify(stage), entry["id"]]
    return {
        "id": entry["id"],
        "season": season,
        "series_slug": series_slug_for(season),
        "match_slug": "-".join(part for part in slug_parts if part),
        "title": " vs ".join(team["abbr"] for team in teams),
        "label": stage or "Match",
        "date": (entry.get("dates") or [""])[0],
        "start_time": (entry.get("dates") or [""])[0],
        "status": "result",
        "status_text": entry.get("result", ""),
        "venue": ", ".join(part for part in (entry.get("venue", ""), entry.get("city", "")) if part and part not in entry.get("venue", "")) or entry.get("venue", ""),
        "teams": teams,
        "series_name": f"IPL {season}",
        "toss": entry.get("toss", ""),
        "player_of_match": entry.get("player_of_match", []),
    }


def _matches_query(match: dict[str, Any], needle: str) -> bool:
    haystack = " ".join([
        match["title"], match["status_text"], match["venue"], match.get("label", ""),
        *(f"{team['name']} {team['abbr']}" for team in match["teams"]),
    ]).casefold()
    return needle in haystack or all(token in haystack for token in needle.split())


def list_ipl_matches(season: int = 2025, query: str = "", limit: int = 24, offset: int = 0) -> dict[str, Any]:
    archived = [entry for entry in cricsheet.entries() if entry.get("season") == season]
    if archived:
        matches = [cricsheet_match(entry) for entry in sorted(archived, key=lambda entry: (entry.get("dates") or [""])[0], reverse=True)]
        provider = "Cricsheet"
    elif season in IPL_SERIES:
        series_slug = IPL_SERIES[season]["slug"]
        provider = "cricdata / ESPNcricinfo"
        try:
            matches = [normalize_match(item, season, series_slug) for item in _series_matches(series_slug)]
        except Exception:
            matches = [_demo_match()] if season == 2025 else []
            provider = "local demo fallback"
    else:
        raise ValueError(f"Unsupported IPL season: {season}")
    needle = query.strip().casefold()
    if needle:
        matches = [match for match in matches if _matches_query(match, needle)]
    total = len(matches)
    page = matches[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
    return {
        "season": season,
        "series": {"name": f"IPL {season}", "slug": series_slug_for(season)},
        "query": query,
        "matches": page,
        "pagination": {"offset": max(0, offset), "limit": limit, "total": total, "has_more": offset + len(page) < total},
        "source": provider,
    }


def search_ipl_archive(query: str, limit: int = 12) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seasons = all_seasons()
    years = [year for year in seasons if str(year) in query] or seasons
    stopwords = {
        "a", "all", "and", "for", "find", "from", "in", "ipl", "match", "matches",
        "me", "of", "please", "season", "show", "the", "to", "versus", "vs",
        *(str(year) for year in seasons),
    }
    tokens = [token for token in re.findall(r"[a-z0-9]+", query.casefold()) if token not in stopwords]
    for season in years:
        try:
            page = list_ipl_matches(season, query="", limit=100)
        except ValueError:
            continue
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


CACHE_DIR = Path(os.environ.get("CRICBOT_CACHE_DIR") or Path(__file__).resolve().parents[1] / "data" / "cache")
# cricdata scrapes Cricinfo without a contract; keep pressure low.
_PROVIDER_SLOTS = threading.BoundedSemaphore(2)


def _cached_call(client: Any, method: str, series_slug: str, match_slug: str) -> tuple[Any, bool]:
    """Fetch once per completed match, then serve from disk.

    Finished matches do not change, so successful responses are kept
    indefinitely; failures are never cached and are retried next time.
    """
    match_id = _match_id(match_slug) or slugify(match_slug)
    path = CACHE_DIR / method / f"{match_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        pass
    with _PROVIDER_SLOTS:
        value, ok = _safe_call(client, method, series_slug, match_slug)
    if ok and value:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass
    return value, ok


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


def _normalize_espn_balls(payload: Any) -> list[dict[str, Any]]:
    """Normalize ESPN play-by-play (``list[list[BallItem]]``) to the ball contract."""
    if not isinstance(payload, list):
        return []
    balls = []
    for group_index, group in enumerate(payload, start=1):
        for index, item in enumerate(group if isinstance(group, list) else []):
            if not isinstance(item, dict):
                continue
            over = item.get("over") or {}
            actual = _first(over.get("actual"), over.get("overs"))
            if actual is not None:
                label = f"{float(actual):.1f}"
            elif over.get("number") is not None:
                # ESPN numbers overs from 1; labels use completed overs.
                label = f"{int(over['number']) - 1}.{over.get('ball', 0)}"
            else:
                label = "—"
            play = str((item.get("playType") or {}).get("description", "")).casefold()
            runs = int(_first(item.get("scoreValue"), over.get("runs"), default=0) or 0)
            wicket = bool((item.get("dismissal") or {}).get("dismissal")) or play == "out"
            six, four = play == "six", play == "four"
            if wicket:
                event = "W"
            elif "wide" in play:
                event = f"{runs}wd"
            elif "no ball" in play:
                event = f"{runs}nb"
            else:
                event = str(runs)
            bowler = ((item.get("bowler") or {}).get("athlete") or {}).get("displayName", "")
            batter = ((item.get("batsman") or {}).get("athlete") or {}).get("displayName", "")
            innings = item.get("innings") or {}
            balls.append({
                "id": str(_first(item.get("id"), default=f"espn-{group_index}-{index}")),
                "inning": int(_first(innings.get("number"), item.get("period"), default=group_index)),
                "over": int(label.split(".")[0]) if label != "—" else 0,
                "ball": over.get("ball"),
                "label": label, "event": event, "runs": runs,
                "batter_runs": (item.get("batsman") or {}).get("runs", runs if not wicket else 0),
                "wicket": wicket, "boundary": four or six,
                "title": f"{bowler} to {batter}" if bowler and batter else item.get("shortText", ""),
                "text": " ".join(part for part in (item.get("preText"), item.get("text"), item.get("postText")) if part).strip(),
                "score": "", "win_probability": None,
                "batter": batter, "bowler": bowler,
            })
    def order(item: dict[str, Any]) -> tuple[int, float, str]:
        try:
            return item["inning"], float(item["label"]), item["id"]
        except (TypeError, ValueError):
            return item["inning"], 0.0, item["id"]
    return sorted(balls, key=order)


def _cricsheet_balls(match: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    path = cricsheet.find_match(match.get("start_time") or match.get("date", ""), [team.get("name", "") for team in match.get("teams", [])])
    if not path:
        return [], ""
    try:
        loaded = cricsheet.load_deliveries(path)
    except (OSError, ValueError):
        return [], ""
    return loaded["balls"], loaded["file"]


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


def _match_id(match_slug: str) -> str:
    found = re.search(r"(\d+)$", match_slug or "")
    return found.group(1) if found else ""


def merge_commentary(balls: list[dict[str, Any]], commentary: list[dict[str, Any]]) -> int:
    """Attach ESPN commentary text to Cricsheet deliveries; returns balls enriched.

    Deliveries are aligned per innings by order when both feeds have the same
    count, otherwise by over label and occurrence (wides repeat a label).
    """
    enriched = 0
    for inning in {ball["inning"] for ball in balls}:
        ours = [ball for ball in balls if ball["inning"] == inning]
        theirs = [ball for ball in commentary if ball["inning"] == inning and ball.get("text")]
        if not theirs:
            continue
        if len(ours) == len(theirs):
            pairs = list(zip(ours, theirs))
        else:
            by_label: dict[tuple[str, int], dict[str, Any]] = {}
            seen: dict[str, int] = {}
            for ball in theirs:
                seen[ball["label"]] = seen.get(ball["label"], 0) + 1
                by_label[(ball["label"], seen[ball["label"]])] = ball
            seen = {}
            pairs = []
            for ball in ours:
                seen[ball["label"]] = seen.get(ball["label"], 0) + 1
                if (match := by_label.get((ball["label"], seen[ball["label"]]))):
                    pairs.append((ball, match))
        for ours_ball, their_ball in pairs:
            ours_ball["commentary"] = their_ball["text"]
            enriched += 1
    return enriched


def consistency_check(canonical: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any]:
    """Compare innings totals from two independent sources."""
    reference_innings = _normalize_innings(reference) if reference else []
    if not reference_innings:
        return {"status": "single-source", "checks": [], "note": "Only one source was available to check this match."}
    checks = []
    for ours, theirs in zip(canonical, reference_innings):
        same = ours["runs"] == theirs["runs"] and ours["wickets"] == theirs["wickets"]
        checks.append({
            "innings": ours["number"], "team": ours["team"],
            "canonical": f"{ours['runs']}/{ours['wickets']}", "reference": f"{theirs['runs']}/{theirs['wickets']}", "match": same,
        })
    ok = bool(checks) and all(check["match"] for check in checks) and len(canonical) == len(reference_innings)
    return {
        "status": "verified" if ok else "mismatch",
        "checks": checks,
        "note": "Cricsheet totals match the ESPNcricinfo scorecard." if ok else "Sources disagree; Cricsheet deliveries are shown.",
    }


def internal_check(info: dict[str, Any], innings: list[dict[str, Any]]) -> dict[str, Any]:
    """Check the recorded result against the scorecard derived from the same deliveries.

    A win by runs must equal the first-innings margin; a win by wickets must equal
    10 minus wickets lost in the chase. Rain-adjusted (DLS/VJD) results and ties
    are skipped because their margins are not plain arithmetic on the totals.
    """
    outcome = info.get("outcome", {})
    by = outcome.get("by", {})
    winner = outcome.get("winner")
    if not winner or outcome.get("method") or len(innings) < 2:
        return {"name": "result vs scorecard", "status": "skipped"}
    first, second = innings[0], innings[1]
    winner_code = team_code(winner)
    if "runs" in by:
        expected = first["runs"] - second["runs"]
        ok = first["team"] == winner_code and expected == by["runs"]
        detail = f"{by['runs']} runs recorded; totals give {expected}"
    elif "wickets" in by:
        expected = 10 - second["wickets"]
        ok = second["team"] == winner_code and expected == by["wickets"] and second["runs"] > first["runs"]
        detail = f"{by['wickets']} wickets recorded; chase lost {second['wickets']}"
    else:
        return {"name": "result vs scorecard", "status": "skipped"}
    return {"name": "result vs scorecard", "status": "ok" if ok else "mismatch", "detail": detail}


def _from_cricsheet(series_slug: str, match_slug: str, entry: dict[str, Any], payload: dict[str, Any], client: Any) -> dict[str, Any]:
    scorecard = derive_scorecard(payload)
    match = cricsheet_match(entry)
    match["series_slug"], match["match_slug"] = series_slug, match_slug
    balls = cricsheet.load_deliveries(cricsheet.data_dir() / entry["file"])["balls"]
    innings = _normalize_innings(scorecard)

    # Independent cross-check and commentary text; both are optional.
    with ThreadPoolExecutor(max_workers=2) as pool:
        reference_job = pool.submit(_cached_call, client, "match_scorecard", series_slug, match_slug)
        commentary_job = pool.submit(_cached_call, client, "match_ball_by_ball", series_slug, match_slug)
        reference, reference_ok = reference_job.result()
        commentary_payload, _ = commentary_job.result()
    enriched = merge_commentary(balls, _normalize_espn_balls(commentary_payload))
    consistency = consistency_check(innings, reference if reference_ok and reference else None)
    internal = internal_check(payload.get("info", {}), innings)
    consistency["internal"] = internal
    if internal["status"] == "mismatch":
        consistency["status"] = "mismatch"
        consistency["note"] = f"The recorded result disagrees with the deliveries ({internal['detail']})."
    elif internal["status"] == "ok" and consistency["status"] == "single-source":
        consistency["note"] = "Result matches the scorecard derived from every delivery; no second source was reachable to cross-check."

    raw_innings = scorecard["content"]["innings"]
    return {
        "match": match,
        "innings": innings,
        "balls": balls,
        "ball_coverage": {"level": "cricsheet", "label": COVERAGE_LABELS["cricsheet"], "note": COVERAGE_NOTES["cricsheet"] + (f" ESPN commentary added to {enriched} deliveries." if enriched else "")},
        "overs": [row["inningOvers"] for row in raw_innings],
        "partnerships": [row["inningPartnerships"] for row in raw_innings],
        "fall_of_wickets": [row["inningFallOfWickets"] for row in raw_innings],
        "extras": [row["extrasBreakdown"] for row in raw_innings],
        "toss": toss_text(payload.get("info", {})),
        "player_awards": payload.get("info", {}).get("player_of_match", []),
        "analytics": {**_analytics(scorecard), "shifts": match_shifts(balls, innings)},
        "consistency": consistency,
        "commentary_count": enriched,
        "sources": [
            {"name": "Match info", "provider": f"Cricsheet ({entry['file']})", "available": True},
            {"name": "Scorecard", "provider": "Derived from Cricsheet deliveries", "available": True},
            {"name": "Ball-by-ball", "provider": f"Cricsheet ({entry['file']})", "available": bool(balls)},
            {"name": "Commentary", "provider": "ESPN play-by-play" if enriched else "unavailable", "available": bool(enriched)},
            {"name": "Cross-check", "provider": "ESPNcricinfo scorecard" if reference_ok and reference else "unavailable", "available": consistency["status"] == "verified"},
        ],
    }


def get_match_detail(series_slug: str, match_slug: str) -> dict[str, Any]:
    try:
        client = _client()
    except Exception:
        client = None
    match_id = _match_id(match_slug)
    entry = cricsheet.by_id(match_id) if match_id else None
    payload = cricsheet.load(match_id) if entry else None
    if entry and payload:
        return _from_cricsheet(series_slug, match_slug, entry, payload, client)

    info, info_ok = _safe_call(client, "match_info", series_slug, match_slug)
    scorecard, score_ok = _safe_call(client, "match_scorecard", series_slug, match_slug)
    overs, overs_ok = _safe_call(client, "match_overs", series_slug, match_slug)

    if not score_ok or not scorecard:
        scorecard = demo_scorecard()
        match = _demo_match()
        match["series_slug"] = series_slug
        match["match_slug"] = match_slug
        source = "local demo fallback"
    else:
        match = _match_from_detail(info if isinstance(info, dict) else {}, scorecard, series_slug, match_slug)
        source = "cricdata / ESPNcricinfo"

    # Ball ladder for matches outside the Cricsheet archive (new or live games).
    balls, cricsheet_file = _cricsheet_balls(match)
    coverage, ball_provider = ("cricsheet", f"Cricsheet ({cricsheet_file})") if balls else ("", "")
    if not balls:
        ball_payload, _ = _safe_call(client, "match_ball_by_ball", series_slug, match_slug)
        balls = _normalize_espn_balls(ball_payload)
        coverage, ball_provider = ("full", "ESPN play-by-play") if balls else ("", "")
    if not balls:
        commentary, _ = _safe_call(client, "match_commentary", series_slug, match_slug)
        balls = _normalize_commentary(commentary)
        coverage, ball_provider = ("commentary", "Cricinfo commentary") if balls else ("", "")
    if not balls and source == "local demo fallback":
        balls = _demo_balls()
        coverage, ball_provider = "demo", source
    if not coverage:
        coverage = "overs-only" if overs_ok and overs else "unavailable"
        ball_provider = "unavailable"

    support = scorecard.get("content", {}).get("supportInfo", {}) or {}
    awards = [
        _player_name(row) for row in (support.get("playersOfTheMatch") or support.get("playersOfTheSeries") or [])
    ]
    return {
        "match": match,
        "innings": _normalize_innings(scorecard),
        "balls": balls,
        "ball_coverage": {"level": coverage, "label": COVERAGE_LABELS[coverage], "note": COVERAGE_NOTES[coverage]},
        "overs": overs if overs_ok else [],
        "partnerships": [],
        "fall_of_wickets": [],
        "extras": [],
        "toss": _first(info.get("tossText") if isinstance(info, dict) else None, info.get("toss") if isinstance(info, dict) else None, default="Unavailable"),
        "player_awards": awards,
        "analytics": {**_analytics(scorecard), "shifts": match_shifts(balls, _normalize_innings(scorecard)) if coverage in {"cricsheet", "full"} else []},
        "consistency": {"status": "demo" if source.endswith("fallback") else "single-source", "checks": [], "note": "Demo data is not checked." if source.endswith("fallback") else "Only one source was available to check this match."},
        "commentary_count": sum(1 for ball in balls if ball.get("text")) if coverage in {"full", "commentary"} else 0,
        "sources": [
            {"name": "Match info", "provider": source, "available": bool(info_ok or source.endswith("fallback"))},
            {"name": "Scorecard", "provider": source, "available": True},
            {"name": "Ball-by-ball", "provider": ball_provider, "available": bool(balls)},
        ],
    }
