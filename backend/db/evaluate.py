"""Check the ball-by-ball data against the official scorecard.

    python -m backend.db.evaluate                 # summary for everything stored
    python -m backend.db.evaluate --season 2025 --show 10

The scorecard is rebuilt from `balls` (views innings_card / batting_card /
bowling_card) and compared field by field with ref_* (the Cricinfo card).
Two independent sources agreeing is strong evidence the balls are complete
and the parsing rules (wides, no-balls, byes, bowler wickets) are right.

Levels checked
  coverage   ingest status per match ('partial' = fewer items than the API's count)
  innings    runs, wickets, legal balls per innings (super overs excluded)
  batting    runs, balls faced, 4s, 6s per batter
  bowling    legal balls, runs conceded, wickets per bowler
  match      a match passes only if every row at every level matches
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import duckdb

from . import store

FILTER = "WHERE (? IS NULL OR m.season = ?)"

INNINGS_SQL = f"""
SELECT r.match_id, m.season, r.inning,
       r.runs AS ref_runs, d.runs, r.wickets AS ref_wkts, d.wickets, r.balls AS ref_balls, d.balls,
       (d.runs IS NOT DISTINCT FROM r.runs AND d.wickets IS NOT DISTINCT FROM r.wickets
        AND d.balls IS NOT DISTINCT FROM r.balls) AS ok
FROM ref_innings r
JOIN matches m USING (match_id)
JOIN ingest_log l USING (match_id)
LEFT JOIN innings_card d USING (match_id, inning)
{FILTER} AND NOT r.is_super_over AND l.status IN ('ok', 'partial')
"""

BATTING_SQL = f"""
SELECT r.match_id, m.season, r.inning, r.player,
       r.runs AS ref_runs, coalesce(d.runs, 0) AS runs, r.balls AS ref_balls, coalesce(d.balls, 0) AS balls,
       r.fours AS ref_4s, coalesce(d.fours, 0) AS fours, r.sixes AS ref_6s, coalesce(d.sixes, 0) AS sixes,
       -- a batter dismissed without facing (e.g. non-striker run out) has no balls: 0 (0) is correct
       (coalesce(d.runs, 0) = r.runs AND coalesce(d.balls, 0) = r.balls
        AND coalesce(d.fours, 0) = r.fours AND coalesce(d.sixes, 0) = r.sixes) AS ok
FROM ref_batting r
JOIN matches m USING (match_id)
JOIN ingest_log l USING (match_id)
JOIN ref_innings ri ON ri.match_id = r.match_id AND ri.inning = r.inning AND NOT ri.is_super_over
LEFT JOIN batting_card d ON d.match_id = r.match_id AND d.inning = r.inning AND d.player_id = r.player_id
{FILTER} AND l.status IN ('ok', 'partial')
"""

BOWLING_SQL = f"""
SELECT r.match_id, m.season, r.inning, r.player,
       r.balls AS ref_balls, d.balls, r.conceded AS ref_conceded, d.conceded, r.wickets AS ref_wkts, d.wickets,
       (d.balls IS NOT DISTINCT FROM r.balls AND d.conceded IS NOT DISTINCT FROM r.conceded
        AND d.wickets IS NOT DISTINCT FROM r.wickets) AS ok
FROM ref_bowling r
JOIN matches m USING (match_id)
JOIN ingest_log l USING (match_id)
JOIN ref_innings ri ON ri.match_id = r.match_id AND ri.inning = r.inning AND NOT ri.is_super_over
LEFT JOIN bowling_card d ON d.match_id = r.match_id AND d.inning = r.inning AND d.player_id = r.player_id
{FILTER} AND l.status IN ('ok', 'partial')
"""


IRREGULAR_OVERS_SQL = """
SELECT DISTINCT match_id, inning FROM (
    SELECT match_id, inning, over_number, count(*) FILTER (WHERE is_legal) AS legal
    FROM balls GROUP BY ALL
) WHERE legal <> 6
"""


def _rows(con: duckdb.DuckDBPyConnection, sql: str, season: str | None) -> list[dict[str, Any]]:
    cur = con.execute(sql, [season, season])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def evaluate(db_path: str | None = None, season: str | None = None) -> dict[str, Any]:
    con = store.connect(db_path, read_only=True)
    coverage = con.execute(
        """SELECT count(*) AS listed,
                  count(*) FILTER (WHERE l.status = 'ok') AS ok,
                  count(*) FILTER (WHERE l.status = 'partial') AS partial,
                  count(*) FILTER (WHERE l.status = 'no_balls') AS no_balls,
                  count(*) FILTER (WHERE l.status = 'error') AS errors,
                  count(*) FILTER (WHERE l.status IS NULL) AS not_ingested
           FROM matches m LEFT JOIN ingest_log l USING (match_id) """ + FILTER, [season, season]
    ).fetchone()
    levels = {name: _rows(con, sql, season) for name, sql in
              (("innings", INNINGS_SQL), ("batting", BATTING_SQL), ("bowling", BOWLING_SQL))}
    irregular = {(m, i) for m, i in con.execute(IRREGULAR_OVERS_SQL).fetchall()}
    partial_ids = {r[0] for r in con.execute("SELECT match_id FROM ingest_log WHERE status = 'partial'").fetchall()}
    con.close()

    # Umpire miscounts (7- or 5-ball overs): the feed records every delivery, the card rounds to
    # whole overs. Explained when runs/wickets and EVERY batter's balls faced match (so no delivery
    # is missing or duplicated) and the innings has an over with != 6 legal balls.
    bad_batting = {(r["match_id"], r["inning"]) for r in levels["batting"] if not r["ok"]}
    explained_innings = {
        (r["match_id"], r["inning"]) for r in levels["innings"]
        if not r["ok"] and r["runs"] == r["ref_runs"] and r["wickets"] == r["ref_wkts"]
        and (r["match_id"], r["inning"]) in irregular and (r["match_id"], r["inning"]) not in bad_batting
    }
    for name, rows in levels.items():
        for r in rows:
            key = (r["match_id"], r["inning"])
            r["explained"] = bool(not r["ok"] and key in explained_innings and (
                name == "innings" or (name == "bowling" and r["conceded"] == r["ref_conceded"]
                                      and r["wickets"] == r["ref_wkts"])))
            if r["explained"]:
                r["reason"] = "umpire over miscount (over with != 6 legal balls)"

    bad_matches = {r["match_id"] for rows in levels.values() for r in rows if not r["ok"]}
    unexplained = {r["match_id"] for rows in levels.values() for r in rows if not r["ok"] and not r["explained"]}
    evaluated = {r["match_id"] for r in levels["innings"]}
    summary = {
        "coverage": dict(zip(("listed", "ok", "partial", "no_balls", "errors", "not_ingested"), coverage)),
        # partial = ESPN's count is above the items served; reconciled = scorecard still matches exactly
        "partial_reconciled": len((partial_ids & evaluated) - unexplained),
        "levels": {
            name: {"rows": len(rows), "exact": sum(r["ok"] for r in rows),
                   "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 4) if rows else None}
            for name, rows in levels.items()
        },
        "match": {"evaluated": len(evaluated), "fully_correct": len(evaluated - bad_matches),
                  "accuracy": round(len(evaluated - bad_matches) / len(evaluated), 4) if evaluated else None,
                  "explained": len(bad_matches - unexplained), "unexplained": len(unexplained)},
        "mismatches": {name: [r for r in rows if not r["ok"]] for name, rows in levels.items()},
    }
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--season", default=None)
    ap.add_argument("--show", type=int, default=5, help="mismatch rows to print per level")
    ap.add_argument("--json", default=None, help="write the full report (incl. all mismatches) here")
    args = ap.parse_args()
    report = evaluate(args.db, args.season)
    print("coverage:", report["coverage"])
    if report["coverage"]["partial"]:
        print(f"partial  {report['partial_reconciled']}/{report['coverage']['partial']} reconcile with the "
              "official scorecard (feed count off, no balls missing)")
    for name, s in report["levels"].items():
        print(f"{name:8s} {s['exact']}/{s['rows']} exact  ({s['accuracy']})")
    m = report["match"]
    print(f"match    {m['fully_correct']}/{m['evaluated']} fully correct  ({m['accuracy']})  "
          f"| explained {m['explained']}  | UNEXPLAINED {m['unexplained']}")
    for name, rows in report["mismatches"].items():
        for r in [r for r in rows if not r["explained"]][: args.show]:   # explained ones are in --json
            print(f"  [{name}] {r}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2, default=str)


if __name__ == "__main__":
    main()
