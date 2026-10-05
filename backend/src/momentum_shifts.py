"""Detect momentum shifts from deliveries, commentary, and the chase equation.

Each detector looks for a pattern a commentator would call a shift, measured
against the innings' own scoring rate so a 2008 slog and a 2025 chase are
judged on their own terms:

- wicket cluster   several wickets in a short window (collapse)
- scoring burst    a run of overs well above the innings rate
- squeeze          a long spell of dots and no boundaries
- chase swing      the required rate jumping or collapsing within two overs
- missed chance    commentary mentions a drop, missed stumping, or overturned review

Magnitude is expressed in runs: runs above/below the innings rate for the
window, with each wicket valued at ``WICKET_VALUE`` runs (a common T20
approximation). Everything returned is traceable to specific deliveries.
"""

from __future__ import annotations

import re
from typing import Any

WICKET_VALUE = 8
# Above this required rate a T20 chase is effectively decided; swings there
# are noise, not momentum.
LIVE_CHASE_RATE = 18
CHANCE_PATTERN = re.compile(r"\b(dropped|drops|put down|grassed|missed (?:the )?(?:stumping|run[- ]out|chance)|spilled|reprieve|overturned)\b", re.I)


def _legal(ball: dict[str, Any]) -> bool:
    event = str(ball.get("event", ""))
    return "wd" not in event and "nb" not in event


def _text(ball: dict[str, Any]) -> str:
    return ball.get("commentary") or ball.get("text") or ""


def _sequence(balls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach legal-ball index and running score to one innings' deliveries."""
    runs = wickets = legal = 0
    rows = []
    for ball in balls:
        runs += int(ball.get("runs") or 0)
        wickets += 1 if ball.get("wicket") else 0
        if _legal(ball):
            legal += 1
        rows.append({**ball, "_legal": legal, "_runs": runs, "_wickets": wickets})
    return rows


def _window(rows: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    chunk = rows[start:end + 1]
    before = rows[start - 1] if start > 0 else {"_runs": 0, "_wickets": 0, "_legal": 0}
    legal = chunk[-1]["_legal"] - before["_legal"]
    return {
        "from": chunk[0]["label"], "to": chunk[-1]["label"],
        "runs": chunk[-1]["_runs"] - before["_runs"],
        "wickets": chunk[-1]["_wickets"] - before["_wickets"],
        "balls": legal,
        "score_before": f"{before['_runs']}/{before['_wickets']}",
        "score_after": f"{chunk[-1]['_runs']}/{chunk[-1]['_wickets']}",
        "boundaries": sum(1 for ball in chunk if ball.get("boundary")),
        "dots": sum(1 for ball in chunk if _legal(ball) and int(ball.get("runs") or 0) == 0),
        "dismissed": [ball.get("title", "").split(" to ")[-1] for ball in chunk if ball.get("wicket")],
        "bowlers": sorted({ball.get("bowler") or ball.get("title", "").split(" to ")[0] for ball in chunk if ball.get("title") or ball.get("bowler")}),
        "evidence": [
            {"label": ball["label"], "event": ball["event"], "text": _text(ball)}
            for ball in chunk if ball.get("wicket") or ball.get("boundary")
        ][:4],
    }


def innings_shifts(balls: list[dict[str, Any]], *, batting: str, bowling: str, target: int | None = None, ball_limit: int = 120) -> list[dict[str, Any]]:
    rows = _sequence(balls)
    if len(rows) < 12:
        return []
    total_legal = max(rows[-1]["_legal"], 1)
    rate = rows[-1]["_runs"] / total_legal  # runs per legal ball, this innings
    events: list[dict[str, Any]] = []

    # Collapse: wickets falling close together. Successive wickets no more
    # than 9 legal balls apart form a cluster; 3+ wickets, or 2 inside 6 balls.
    wicket_rows = [index for index, row in enumerate(rows) if row.get("wicket")]
    groups: list[list[int]] = []
    for index in wicket_rows:
        if groups and rows[index]["_legal"] - rows[groups[-1][-1]]["_legal"] <= 9:
            groups[-1].append(index)
        else:
            groups.append([index])
    for group in groups:
        window = _window(rows, group[0], group[-1])
        if len(group) >= 3 or (len(group) == 2 and window["balls"] <= 6):
            expected = rate * max(window["balls"], 1)
            events.append({
                "type": "wicket_cluster", "innings_team": batting, **window, "favours": bowling,
                "magnitude": round(window["wickets"] * WICKET_VALUE + (expected - window["runs"]), 1),
                "headline": f"{window['wickets']} wickets for {window['runs']} runs in {window['balls']} balls",
            })

    # Over-level runs: bursts and squeezes are consecutive overs well above or
    # well below the innings' own rate.
    overs: dict[int, list[int]] = {}
    for index, row in enumerate(rows):
        overs.setdefault(int(row.get("over") or 0), []).append(index)
    over_rate = rate * 6

    def runs_of(numbers: list[int], test) -> list[list[int]]:
        spans, current = [], []
        for number in sorted(numbers):
            if test(number) and (not current or number == current[-1] + 1):
                current.append(number)
            else:
                if current:
                    spans.append(current)
                current = [number] if test(number) else []
        if current:
            spans.append(current)
        return spans

    def over_window(span: list[int]) -> dict[str, Any]:
        return _window(rows, overs[span[0]][0], overs[span[-1]][-1])

    def over_runs(number: int) -> int:
        return over_window([number])["runs"]

    for span in runs_of(list(overs), lambda n: over_runs(n) >= max(10, 1.4 * over_rate)):
        window = over_window(span)
        if (len(span) >= 2 or window["runs"] >= 20) and window["wickets"] <= 1:
            expected = rate * window["balls"]
            events.append({
                "type": "scoring_burst", "innings_team": batting, **window, "favours": batting,
                "magnitude": round(window["runs"] - expected - window["wickets"] * WICKET_VALUE, 1),
                "headline": f"{window['runs']} runs in {len(span)} over{'s' if len(span) > 1 else ''} with {window['boundaries']} boundaries",
            })
    for span in runs_of(list(overs), lambda n: over_window([n])["boundaries"] == 0 and over_runs(n) <= 0.6 * over_rate):
        window = over_window(span)
        if len(span) >= 2:
            expected = rate * window["balls"]
            events.append({
                "type": "squeeze", "innings_team": batting, **window, "favours": bowling,
                "magnitude": round(expected - window["runs"] + window["wickets"] * WICKET_VALUE, 1),
                "headline": f"only {window['runs']} runs in {len(span)} overs ({window['dots']} dots, no boundary)",
            })

    # Chase: compare runs scored over two overs with what the equation asked
    # for at the start of them. Skip once the chase is beyond reach.
    if target:
        ends = [index for index, row in enumerate(rows) if _legal(row) and row["_legal"] % 6 == 0]
        for first, last in zip(ends, ends[2:]):
            start_row = rows[first]
            remaining = ball_limit - start_row["_legal"]
            needed = target - start_row["_runs"]
            if remaining <= 0 or needed <= 0:
                continue
            required = 6 * needed / remaining
            if required > LIVE_CHASE_RATE:
                continue
            window = _window(rows, first + 1, last)
            par = required * window["balls"] / 6
            wickets_cost = window["wickets"] * WICKET_VALUE
            swing = round(window["runs"] - par - wickets_cost, 1)
            if abs(swing) >= 12:
                after = rows[last]
                left = ball_limit - after["_legal"]
                required_after = round(6 * (target - after["_runs"]) / left, 2) if left > 0 else None
                events.append({
                    "type": "chase_swing", "innings_team": batting, **window,
                    "favours": batting if swing > 0 else bowling, "magnitude": abs(swing),
                    "headline": f"{window['runs']} runs and {window['wickets']} wicket{'s' if window['wickets'] != 1 else ''} when {round(par)} were needed at {round(required, 2)} an over",
                    "required_rate_before": round(required, 2), "required_rate_after": required_after,
                })

    # Commentary-only signals: chances that never reach the scorecard.
    for index, row in enumerate(rows):
        text = _text(row)
        if text and CHANCE_PATTERN.search(text):
            window = _window(rows, index, index)
            events.append({
                "type": "missed_chance", "innings_team": batting, **window,
                "favours": batting, "magnitude": 6.0,
                "headline": f"chance missed at {row['label']}",
                "evidence": [{"label": row["label"], "event": row["event"], "text": text}],
            })

    # Strongest first, so overlapping weaker readings are dropped below.
    events.sort(key=lambda event: -event["magnitude"])
    kept: list[dict[str, Any]] = []
    for event in events:
        # The same passage seen by several detectors is one shift: keep the strongest.
        if not any(_overlaps(event, other) and (event["type"] == other["type"] or event["favours"] == other["favours"]) for other in kept):
            kept.append(event)
    return kept


def _position(label: str) -> float:
    try:
        return float(label)
    except (TypeError, ValueError):
        return 0.0


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return _position(a["from"]) <= _position(b["to"]) and _position(b["from"]) <= _position(a["to"])


def match_shifts(balls: list[dict[str, Any]], innings: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Top momentum shifts across the match, ordered by when they happened."""
    teams = [inning["team"] for inning in innings]
    events = []
    for number, inning in enumerate(innings, 1):
        inning_balls = [ball for ball in balls if ball.get("inning") == number]
        bowling = next((team for team in teams if team != inning["team"]), "")
        target = innings[0]["runs"] + 1 if number == 2 and innings else None
        for event in innings_shifts(inning_balls, batting=inning["team"], bowling=bowling, target=target):
            events.append({**event, "inning": number})
    strongest = sorted(events, key=lambda event: -event["magnitude"])[:limit]
    if strongest:
        strongest[0]["decisive"] = True
    return sorted(strongest, key=lambda event: (event["inning"], float(event["from"]) if re.fullmatch(r"\d+\.\d+", str(event["from"])) else 0))


def describe(event: dict[str, Any]) -> str:
    """One factual line for the LLM facts sheet."""
    who = f"{event['innings_team']} innings"
    line = f"[{event['type'].replace('_', ' ')}] {who}, overs {event['from']}–{event['to']}: {event['headline']}; score {event['score_before']} → {event['score_after']}; favoured {event['favours']} (swing {event['magnitude']} runs)"
    if event.get("dismissed"):
        line += f"; out: {', '.join(name for name in event['dismissed'] if name)}"
    if event.get("decisive"):
        line += " [largest shift]"
    for item in event.get("evidence", [])[:2]:
        if item.get("text"):
            line += f" | {item['label']} {item['event']}: {item['text'][:140]}"
    return line
