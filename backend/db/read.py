"""Read-side helpers: serve stored matches in the shapes the API/UI already use."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import store


def match_id_from_slug(match_slug: str) -> int | None:
    found = re.search(r"(\d+)$", match_slug or "")
    return int(found.group(1)) if found else None


def _records(con, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _overs(balls: int | None) -> str:
    balls = int(balls or 0)
    return f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)


def match_detail(match_slug: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Stored match in get_match_detail()'s shape, or None if not ingested (status ok/partial)."""
    match_id = match_id_from_slug(match_slug)
    path = Path(db_path or store.DEFAULT_DB)
    if match_id is None or not path.exists():
        return None
    con = store.connect(path, read_only=True)
    try:
        found = _records(con, "SELECT m.*, l.status FROM matches m JOIN ingest_log l USING (match_id) "
                              "WHERE match_id = ? AND l.status IN ('ok', 'partial')", [match_id])
        if not found:
            return None
        m = found[0]
        innings_rows = _records(con, "SELECT * FROM ref_innings WHERE match_id = ? ORDER BY inning", [match_id])
        batting = _records(con, "SELECT * FROM ref_batting WHERE match_id = ? ORDER BY inning, position", [match_id])
        bowling = _records(con, "SELECT * FROM ref_bowling WHERE match_id = ? ORDER BY inning", [match_id])
        balls = _records(con, """
            SELECT match_id || '-' || inning || '-' || sequence AS id, inning, over_label AS label,
                   CASE WHEN is_wicket THEN 'W' WHEN is_six THEN '6' WHEN is_four THEN '4'
                        ELSE CAST(runs_total AS VARCHAR) END AS event,
                   runs_total AS runs, runs_bat AS batter_runs, is_wicket AS wicket, (is_four OR is_six) AS boundary,
                   short_text AS title, commentary AS text,
                   CAST(sum(runs_total) OVER w AS VARCHAR) || '/' ||
                   CAST(count(*) FILTER (WHERE is_wicket) OVER w AS VARCHAR) AS score,
                   NULL AS win_probability, batter, bowler, dismissal_type, is_legal
            FROM balls WHERE match_id = ?
            WINDOW w AS (PARTITION BY inning ORDER BY sequence ROWS UNBOUNDED PRECEDING)
            ORDER BY inning, sequence""", [match_id])
    finally:
        con.close()

    innings = []
    for inn in innings_rows:
        n = inn["inning"]
        innings.append({
            "number": n, "team": inn["team"], "runs": inn["runs"], "wickets": inn["wickets"],
            "overs": _overs(inn["balls"]), "extras": inn["extras"], "is_super_over": inn["is_super_over"],
            "batters": [{"name": b["player"], "dismissal": b["dismissal"], "runs": b["runs"], "balls": b["balls"],
                         "fours": b["fours"], "sixes": b["sixes"],
                         "strike_rate": round(100 * b["runs"] / b["balls"], 2) if b["balls"] else "—",
                         "not_out": not b["is_out"]} for b in batting if b["inning"] == n],
            "bowlers": [{"name": b["player"], "overs": _overs(b["balls"]), "maidens": None, "runs": b["conceded"],
                         "wickets": b["wickets"], "economy": round(6 * b["conceded"] / b["balls"], 2) if b["balls"] else "—",
                         "dots": b["dots"]} for b in bowling if b["inning"] == n],
        })
    scores = {inn["team"]: f"{inn['runs']}/{inn['wickets']}" for inn in innings if not inn["is_super_over"]}
    teams = [{"id": "", "name": t, "abbr": t, "color": "#b8f34a", "score": scores.get(t, "—"), "score_info": ""}
             for t in (m["team1"], m["team2"]) if t]
    return {
        "match": {
            "id": str(match_id), "season": m["season"], "series_slug": m["series_path"], "match_slug": m["match_path"],
            "title": " vs ".join(t["abbr"] for t in teams) or m["title"], "label": m["title"],
            "date": str(m["start_date"] or ""), "start_time": str(m["start_date"] or ""), "status": "result",
            "status_text": m["status_text"], "venue": m["venue"], "teams": teams, "series_name": f"IPL {m['season']}",
        },
        "innings": innings,
        "balls": balls,
        "ball_coverage": {
            "level": "full", "label": "Full ball feed",
            "note": f"{len(balls)} deliveries from the local DuckDB archive, reconciled against the official scorecard.",
        },
        "sources": [
            {"name": "Match info", "provider": "local DuckDB archive", "available": True},
            {"name": "Scorecard", "provider": "local DuckDB archive (Cricinfo card)", "available": True},
            {"name": "Ball-by-ball", "provider": "local DuckDB archive (ESPN play-by-play)", "available": bool(balls)},
        ],
    }
