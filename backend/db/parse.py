"""Pure parsing: cricdata payloads -> flat rows for DuckDB.

No network or database code here, so everything is unit-testable with
recorded JSON fixtures.

Two independent sources are parsed:
  * ESPN play-by-play (one item per delivery, incl. wides/no-balls)  -> balls
  * Cricinfo scorecard page (the official card)                      -> ref_*

`backend.db.evaluate` rebuilds the scorecard from `balls` and checks it
against `ref_*`, which is how we know the ball data is complete and correct.
"""

from __future__ import annotations

from typing import Any


def _athlete(state: dict[str, Any] | None) -> tuple[int | None, str]:
    athlete = (state or {}).get("athlete") or {}
    raw_id = athlete.get("id")
    return (int(raw_id) if str(raw_id or "").isdigit() else None, athlete.get("displayName") or athlete.get("name") or "")


# --------------------------------------------------------------- matches

def match_row(raw: dict[str, Any], series_path: str, season: str) -> dict[str, Any]:
    """One row of `matches` from a series_matches() entry."""
    teams = raw.get("teams") or []

    def team(i: int) -> str:
        if i >= len(teams):
            return ""
        t = teams[i].get("team") or {}
        return t.get("abbreviation") or t.get("name") or t.get("longName") or ""

    ground = raw.get("ground") or {}
    winner_id = raw.get("winnerTeamId")
    winner = ""
    for t in teams:
        info = t.get("team") or {}
        if winner_id is not None and info.get("id") == winner_id:
            winner = info.get("abbreviation") or info.get("name") or ""
    return {
        "match_id": int(raw["objectId"]),
        "season": str(season),
        "series_path": series_path,
        "match_path": f"{raw.get('slug')}-{raw['objectId']}",
        "title": raw.get("title") or "",
        "start_date": (raw.get("startDate") or "")[:10] or None,
        "venue": ground.get("longName") or ground.get("name") or "",
        "team1": team(0),
        "team2": team(1),
        "winner": winner,
        "status_text": raw.get("statusText") or "",
        "format": raw.get("format") or "",
        "stage": raw.get("stage") or "",
        "is_cancelled": bool(raw.get("isCancelled")),
    }


def is_ingestable(raw: dict[str, Any]) -> bool:
    """T20, finished and not cancelled. No-result matches are kept if any balls were bowled."""
    return (
        (raw.get("format") or "").upper() == "T20"
        and not raw.get("isCancelled")
        and (raw.get("stage") == "FINISHED" or raw.get("state") == "POST")
    )


# ----------------------------------------------------------------- balls

BALL_COLUMNS = (
    "match_id", "inning", "sequence", "over_number", "ball_in_over", "over_label",
    "batting_team", "batter_id", "batter", "non_striker", "bowler_id", "bowler",
    "runs_total", "score_value", "runs_bat", "extras", "wide_runs", "noball_runs", "bye_runs", "legbye_runs",
    "is_legal", "is_wide", "is_noball", "is_four", "is_six",
    "is_wicket", "bowler_wicket", "dismissal_type", "player_out_id", "player_out",
    "short_text", "commentary",
)


def ball_rows(match_id: int, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ESPN play-by-play items (any nesting by innings already removed)."""
    rows: dict[str, dict[str, Any]] = {}
    for it in items:
        over = it.get("over") or {}
        if not over.get("ball") and not (it.get("shortText") or "").strip():
            continue                                   # placeholder item (over 0, ball 0, no text) - not a delivery
        play = ((it.get("playType") or {}).get("description") or "").lower()
        dismissal = it.get("dismissal") or {}
        dismissal_type = (dismissal.get("type") or "").lower()
        # "retired not out (hurt)" is flagged as a dismissal by ESPN but is not a wicket; "retired out" is.
        is_wicket = bool(dismissal.get("dismissal")) and not dismissal_type.startswith("retired not out")
        batter_id, batter = _athlete(it.get("batsman"))
        _, non_striker = _athlete(it.get("otherBatsman"))
        bowler_id, bowler = _athlete(it.get("bowler"))
        out_id, out_name = _athlete(dismissal.get("batsman")) if dismissal.get("dismissal") else (None, "")

        score_value = int(it.get("scoreValue") or 0)
        bat = int((it.get("batsman") or {}).get("runs") or 0)
        # over.wide / noBall / byes / legByes are this ball's extra RUNS (not flags, not cumulative).
        # They are more reliable than scoreValue: "(no ball) 4 byes" arrives with scoreValue=1, byes=4.
        wide_runs = int(over.get("wide") or 0)
        noball_runs = int(over.get("noBall") or 0)
        bye_runs = int(over.get("byes") or 0)
        legbye_runs = int(over.get("legByes") or 0)
        if not (wide_runs or noball_runs or bye_runs or legbye_runs) and score_value > bat:
            # components missing: attribute the extras from scoreValue using the play type
            spare = score_value - bat
            if play == "wide":
                wide_runs = spare
            elif play == "no ball":
                noball_runs = spare
            elif play == "leg bye":
                legbye_runs = spare
            elif play == "bye":
                bye_runs = spare
        extras = wide_runs + noball_runs + bye_runs + legbye_runs
        total = bat + extras
        is_wide = wide_runs > 0 or play == "wide"
        is_noball = noball_runs > 0 or play == "no ball"

        key = str(it.get("id"))
        rows[key] = {                                  # dict keyed by id de-duplicates repeated pages
            "match_id": match_id,
            "inning": int((it.get("innings") or {}).get("number") or it.get("period") or 0),
            "sequence": int(it.get("sequence") or 0),
            "over_number": over.get("number"),
            "ball_in_over": over.get("ball"),
            "over_label": str(over.get("actual", "")),
            "batting_team": (it.get("team") or {}).get("abbreviation") or (it.get("team") or {}).get("displayName") or "",
            "batter_id": batter_id, "batter": batter, "non_striker": non_striker,
            "bowler_id": bowler_id, "bowler": bowler,
            "runs_total": total, "score_value": score_value, "runs_bat": bat, "extras": extras,
            "wide_runs": wide_runs, "noball_runs": noball_runs, "bye_runs": bye_runs, "legbye_runs": legbye_runs,
            "is_legal": not (is_wide or is_noball),
            "is_wide": is_wide, "is_noball": is_noball,
            "is_four": play == "four", "is_six": play == "six",
            "is_wicket": is_wicket,
            "bowler_wicket": is_wicket and bool(dismissal.get("bowled")),
            "dismissal_type": dismissal.get("type") or "",   # kept even for retired hurt
            "player_out_id": out_id, "player_out": out_name,
            "short_text": it.get("shortText") or "",
            "commentary": it.get("text") or "",
        }
    return sorted(rows.values(), key=lambda r: (r["inning"], r["sequence"]))


# ------------------------------------------------------ reference scorecard

def _player(row: dict[str, Any]) -> tuple[int | None, str]:
    p = row.get("player") or {}
    oid = p.get("objectId")
    return (int(oid) if oid is not None else None, p.get("longName") or p.get("name") or "")


def scorecard_rows(match_id: int, scorecard: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Official card from the Cricinfo scorecard page -> ref_innings / ref_batting / ref_bowling."""
    innings, batting, bowling = [], [], []
    for inn in (scorecard.get("content") or {}).get("innings") or []:
        number = int(inn.get("inningNumber") or len(innings) + 1)
        team = (inn.get("team") or {})
        innings.append({
            "match_id": match_id, "inning": number,
            "team": team.get("abbreviation") or team.get("name") or "",
            "runs": inn.get("runs"), "wickets": inn.get("wickets"), "balls": inn.get("balls"),
            "extras": inn.get("extras"), "byes": inn.get("byes"), "legbyes": inn.get("legbyes"),
            "wides": inn.get("wides"), "noballs": inn.get("noballs"), "penalties": inn.get("penalties"),
            "is_super_over": bool(inn.get("isSuperOver")) or number > 2,   # T20: innings 3+ are super overs
        })
        for pos, b in enumerate(inn.get("inningBatsmen") or [], 1):
            if b.get("runs") is None:                  # did not bat
                continue
            pid, name = _player(b)
            dismissal = b.get("dismissalText")
            if isinstance(dismissal, dict):
                dismissal = dismissal.get("long") or dismissal.get("short") or ""
            batting.append({
                "match_id": match_id, "inning": number, "position": pos, "player_id": pid, "player": name,
                "runs": b.get("runs"), "balls": b.get("balls"), "fours": b.get("fours"), "sixes": b.get("sixes"),
                "is_out": bool(b.get("isOut")), "dismissal": (dismissal or "").strip(),
            })
        for b in inn.get("inningBowlers") or []:
            if b.get("balls") is None and b.get("overs") is None:
                continue
            pid, name = _player(b)
            bowling.append({
                "match_id": match_id, "inning": number, "player_id": pid, "player": name,
                "balls": b.get("balls"), "conceded": b.get("conceded"), "wickets": b.get("wickets"),
                "wides": b.get("wides"), "noballs": b.get("noballs"), "dots": b.get("dots"),
            })
    return {"ref_innings": innings, "ref_batting": batting, "ref_bowling": bowling}
