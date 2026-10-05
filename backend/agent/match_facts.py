"""Turn a resolved match into grounded facts (for the LLM) and a chat card (for the UI).

Every number here comes from ``get_match_detail``: the same scorecard,
deliveries, and commentary the match workspace shows.
"""

from __future__ import annotations

import re
from typing import Any

from backend.src.ipl_data import get_match_detail
from backend.src.momentum_shifts import describe


def resolve_match(entities: dict[str, Any], results: list[dict[str, Any]], ui_context: dict[str, Any]) -> dict[str, str] | None:
    """Pick the single match a question is about, or None for list/unclear questions."""
    if entities.get("intent") == "list_matches":
        return None
    visible = {"series_slug": ui_context.get("series_slug"), "match_slug": ui_context.get("match_slug")}
    visible_teams = sorted(str(team).upper() for team in ui_context.get("teams") or [])
    named = bool(entities.get("teams") or entities.get("years") or entities.get("match_terms") or entities.get("venues"))
    # Nothing new named, or exactly the teams on screen: the visible match.
    if visible["series_slug"] and visible["match_slug"]:
        same_teams = sorted(entities.get("teams") or []) == visible_teams
        if not named or (same_teams and not entities.get("years") and not entities.get("match_terms")):
            return visible
    matches = [row for row in results if row.get("type") == "match" and (row.get("action") or {}).get("type") == "open_match"]
    if not matches:
        return None
    # A named stage or a team pair in one season usually means one fixture;
    # otherwise only answer when the archive returned a single candidate.
    if len(matches) == 1 or entities.get("match_terms") or (len(entities.get("teams") or []) == 2 and entities.get("years")):
        action = matches[0]["action"]
        return {"series_slug": action["series_slug"], "match_slug": action["match_slug"]}
    return None


def _over_requests(question: str) -> list[int]:
    numbers = re.findall(r"\b(?:over|overs)\s+(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+over\b", question.casefold())
    return sorted({int(a or b) for a, b in numbers if 0 < int(a or b) <= 20})


def _batting_line(row: dict[str, Any]) -> str:
    out = "not out" if row["not_out"] else (row["dismissal"]["long"] if isinstance(row["dismissal"], dict) else row["dismissal"]) or "out"
    return f"{row['name']} {row['runs']}{'*' if row['not_out'] else ''} ({row['balls']}b, {row['fours']}x4, {row['sixes']}x6) {out}"


def _bowling_line(row: dict[str, Any]) -> str:
    return f"{row['name']} {row['wickets']}/{row['runs']} ({row['overs']} ov, econ {row['economy']}, {row['dots']} dots)"


def _key_moments(balls: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    wickets = [ball for ball in balls if ball.get("wicket")]
    sixes = [ball for ball in balls if ball.get("boundary") and str(ball.get("event")) == "6"]
    death = [ball for ball in balls if int(ball.get("over") or 0) >= 18 and ball.get("inning") == max((b["inning"] for b in balls), default=1)]
    picked: list[dict[str, Any]] = []
    for ball in [*wickets, *death[-4:], *sixes]:
        if ball not in picked:
            picked.append(ball)
    picked = picked[:limit]
    return sorted(picked, key=lambda ball: (ball["inning"], int(ball.get("over") or 0), ball.get("id", "")))


def build_match_context(target: dict[str, str], question: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """Return (facts text, chat card, result row) for one match."""
    try:
        detail = get_match_detail(target["series_slug"], target["match_slug"])
    except Exception:
        return None
    match = detail["match"]
    innings = detail["innings"]
    lines = [
        f"Match: {match['title']} ({', '.join(team['name'] for team in match['teams'])}), {match.get('label') or 'IPL match'}, IPL {match['season']}",
        f"Date: {(match.get('date') or '')[:10] or 'unknown'} · Venue: {match.get('venue') or 'unknown'}",
        f"Result: {match.get('status_text') or 'unknown'}",
        f"Toss: {detail.get('toss') or 'unknown'}",
        f"Player of the match: {', '.join(detail.get('player_awards') or []) or 'unknown'}",
    ]
    for inning in innings:
        lines.append(f"Innings {inning['number']} – {inning['team']}: {inning['runs']}/{inning['wickets']} in {inning['overs']} overs (extras {inning['extras']})")
        lines.append("  Batting: " + "; ".join(_batting_line(row) for row in inning["batters"]))
        lines.append("  Bowling: " + "; ".join(_bowling_line(row) for row in inning["bowlers"]))
    for number, overs in enumerate(detail.get("overs") or [], 1):
        if overs and isinstance(overs, list) and isinstance(overs[0], dict) and "overRuns" in overs[0]:
            lines.append(f"  Innings {number} runs per over: " + ", ".join(f"{row['overNumber']}:{row['overRuns']}{'w' * row['overWickets']}" for row in overs))
    for number, fall in enumerate(detail.get("fall_of_wickets") or [], 1):
        if fall and isinstance(fall, list) and isinstance(fall[0], dict) and "runs" in fall[0]:
            lines.append(f"  Innings {number} fall of wickets: " + ", ".join(f"{row['runs']}-{row['wicket']} ({row['player']}, {row['over']} ov)" for row in fall))
    impact = detail.get("analytics", {}).get("impact", [])[:3]
    if impact:
        lines.append("Impact (runs above par): " + "; ".join(f"{row['player']} {row['impact']:+.1f} ({row['role']})" for row in impact))
    shifts = detail.get("analytics", {}).get("shifts", [])
    if shifts:
        lines.append("MOMENTUM SHIFTS (detected from deliveries; swing = runs gained, a wicket = 8 runs):")
        lines.extend(f"  {describe(event)}" for event in shifts)
    moments = _key_moments(detail.get("balls") or [])
    if moments:
        lines.append("Key deliveries:")
        for ball in moments:
            text = ball.get("commentary") or ball.get("text") or ""
            lines.append(f"  Inns {ball['inning']} {ball['label']} {ball.get('title', '')}: {ball['event']} – {text} (score {ball.get('score') or 'n/a'})")
    balls = detail.get("balls") or []
    requested = [(None, over) for over in _over_requests(question)]
    if re.search(r"\b(last|final|closing)\s+over\b", question.casefold()):
        # The last over each innings actually reached (a chase can end early).
        for number in sorted({ball["inning"] for ball in balls}):
            last = max(int(ball.get("over") or 0) for ball in balls if ball["inning"] == number)
            requested.append((number, last + 1))
    for inning_number, over in requested:
        deliveries = [ball for ball in balls if int(ball.get("over") or -1) == over - 1 and (inning_number is None or ball["inning"] == inning_number)]
        if deliveries:
            lines.append(f"Over {over}{f' of innings {inning_number}' if inning_number else ''} deliveries:")
            for ball in deliveries:
                lines.append(f"  Inns {ball['inning']} {ball['label']} {ball.get('title', '')}: {ball['event']} – {ball.get('commentary') or ball.get('text', '')} (score {ball.get('score') or 'n/a'})")
    coverage = detail.get("ball_coverage", {})
    lines.append(f"Data: {coverage.get('label', 'unknown')} · {detail.get('consistency', {}).get('note', '')}")

    card = {
        "type": "match",
        "match": match,
        "toss": detail.get("toss"),
        "player_of_match": detail.get("player_awards") or [],
        "innings": [
            {
                "team": inning["team"], "score": f"{inning['runs']}/{inning['wickets']}", "overs": inning["overs"],
                "top_batters": [
                    {"name": row["name"], "runs": row["runs"], "balls": row["balls"], "not_out": row["not_out"]}
                    for row in sorted(inning["batters"], key=lambda row: (-row["runs"], row["balls"]))[:3]
                ],
                "top_bowlers": [
                    {"name": row["name"], "wickets": row["wickets"], "runs": row["runs"], "overs": row["overs"]}
                    for row in sorted(inning["bowlers"], key=lambda row: (-row["wickets"], float(row["runs"] or 0)))[:2]
                ],
            }
            for inning in innings
        ],
        "key_moments": [
            {"inning": ball["inning"], "label": ball["label"], "event": ball["event"], "title": ball.get("title", ""),
             "text": ball.get("commentary") or ball.get("text", ""), "wicket": ball.get("wicket", False), "boundary": ball.get("boundary", False)}
            for ball in moments[:5]
        ],
        "momentum_shifts": [
            {key: event.get(key) for key in ("type", "inning", "innings_team", "from", "to", "headline", "favours", "magnitude", "score_before", "score_after", "decisive")}
            for event in shifts
        ],
        "coverage": coverage,
        "consistency": detail.get("consistency", {}),
        "ball_count": len(detail.get("balls") or []),
        "commentary_count": detail.get("commentary_count", 0),
    }
    row = {
        "id": f"ipl:{match['series_slug']}:{match['match_slug']}", "type": "match", "title": match["title"],
        "subtitle": match.get("status_text", ""), "meta": f"IPL {match['season']} · {match.get('venue', '')}",
        "action": {"type": "open_match", "series_slug": match["series_slug"], "match_slug": match["match_slug"]},
    }
    return "\n".join(lines), card, row


def match_list_card(results: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    rows = [row for row in results if row.get("type") == "match" and (row.get("action") or {}).get("type") == "open_match"]
    if not rows:
        return None
    return {
        "type": "match_list",
        "title": title,
        "matches": [
            {"title": row["title"], "subtitle": row["subtitle"], "meta": row["meta"], "date": row.get("date", ""),
             "label": row.get("match_label", ""), "series_slug": row["action"]["series_slug"], "match_slug": row["action"]["match_slug"]}
            for row in rows[:30]
        ],
    }


def deterministic_summary(card: dict[str, Any]) -> str:
    """Plain recap used when no LLM is reachable or its answer fails grounding."""
    match = card["match"]
    parts = [f"{match['title']}, {match.get('label') or 'IPL'} {match['season']}: {match.get('status_text') or 'result unavailable'}."]
    for inning in card["innings"]:
        bat = inning["top_batters"][0] if inning["top_batters"] else None
        bowl = inning["top_bowlers"][0] if inning["top_bowlers"] else None
        text = f"{inning['team']} {inning['score']} ({inning['overs']} ov)"
        if bat:
            text += f", top score {bat['name']} {bat['runs']}{'*' if bat['not_out'] else ''} off {bat['balls']}"
        if bowl and bowl["wickets"]:
            text += f"; best bowling {bowl['name']} {bowl['wickets']}/{bowl['runs']}"
        parts.append(text + ".")
    if card.get("player_of_match"):
        parts.append(f"Player of the match: {', '.join(card['player_of_match'])}.")
    return " ".join(parts)
