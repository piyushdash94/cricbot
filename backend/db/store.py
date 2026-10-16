"""DuckDB store: one file, a few plain tables, and SQL views for scorecards.

    from backend.db.store import connect
    con = connect()                       # data/cricbot.duckdb by default
    con.sql("SELECT * FROM batting_card WHERE match_id = 1473511").show()

Tables
  matches          one row per match (ESPN fixture list, gaps filled from Cricsheet)
  cricsheet_balls  one row per delivery from Cricsheet (backend.db.cricsheet_load)
  balls            one row per delivery from ESPN play-by-play, with commentary text
                   (backend.db.ingest, optional)
  ref_innings / ref_batting / ref_bowling
                   the official Cricinfo scorecard, kept for dismissal text and as the
                   answer key in backend.db.evaluate (filled by the ESPN ingest)
  players          display name per player_id (Cricinfo ids, shared by both sources)
  ingest_log       what happened the last time each match was ESPN-ingested

Views
  deliveries       every delivery, one source per match: Cricsheet when loaded, else ESPN
  innings_card, batting_card, bowling_card            scorecards rebuilt from `balls` (ESPN)
  cs_innings_card, cs_batting_card, cs_bowling_card   the same, rebuilt from `cricsheet_balls`
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .parse import BALL_COLUMNS

DEFAULT_DB = Path(os.environ.get("CRICBOT_DB", Path(__file__).resolve().parents[2] / "data" / "cricbot.duckdb"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id BIGINT PRIMARY KEY, season VARCHAR, series_path VARCHAR, match_path VARCHAR,
    title VARCHAR, start_date DATE, venue VARCHAR, team1 VARCHAR, team2 VARCHAR, winner VARCHAR,
    status_text VARCHAR, format VARCHAR, stage VARCHAR, is_cancelled BOOLEAN
);
CREATE TABLE IF NOT EXISTS balls (
    match_id BIGINT, inning INTEGER, sequence BIGINT, over_number INTEGER, ball_in_over INTEGER, over_label VARCHAR,
    batting_team VARCHAR, batter_id BIGINT, batter VARCHAR, non_striker VARCHAR, bowler_id BIGINT, bowler VARCHAR,
    runs_total INTEGER, score_value INTEGER, runs_bat INTEGER, extras INTEGER, wide_runs INTEGER, noball_runs INTEGER,
    bye_runs INTEGER, legbye_runs INTEGER,
    is_legal BOOLEAN, is_wide BOOLEAN, is_noball BOOLEAN, is_four BOOLEAN, is_six BOOLEAN,
    is_wicket BOOLEAN, bowler_wicket BOOLEAN, dismissal_type VARCHAR, player_out_id BIGINT, player_out VARCHAR,
    short_text VARCHAR, commentary VARCHAR,
    PRIMARY KEY (match_id, inning, sequence)
);
CREATE TABLE IF NOT EXISTS ref_innings (
    match_id BIGINT, inning INTEGER, team VARCHAR, runs INTEGER, wickets INTEGER, balls INTEGER,
    extras INTEGER, byes INTEGER, legbyes INTEGER, wides INTEGER, noballs INTEGER, penalties INTEGER,
    is_super_over BOOLEAN, PRIMARY KEY (match_id, inning)
);
CREATE TABLE IF NOT EXISTS ref_batting (
    match_id BIGINT, inning INTEGER, position INTEGER, player_id BIGINT, player VARCHAR,
    runs INTEGER, balls INTEGER, fours INTEGER, sixes INTEGER, is_out BOOLEAN, dismissal VARCHAR
);
CREATE TABLE IF NOT EXISTS ref_bowling (
    match_id BIGINT, inning INTEGER, player_id BIGINT, player VARCHAR,
    balls INTEGER, conceded INTEGER, wickets INTEGER, wides INTEGER, noballs INTEGER, dots INTEGER
);
CREATE TABLE IF NOT EXISTS ingest_log (
    match_id BIGINT PRIMARY KEY, status VARCHAR, ball_items INTEGER, expected_items INTEGER,
    error VARCHAR, ingested_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS cricsheet_balls (
    match_id BIGINT, inning INTEGER, sequence BIGINT, over_number INTEGER, ball_in_over INTEGER, over_label VARCHAR,
    batting_team VARCHAR, batter_id BIGINT, batter VARCHAR, non_striker VARCHAR, bowler_id BIGINT, bowler VARCHAR,
    runs_total INTEGER, score_value INTEGER, runs_bat INTEGER, extras INTEGER, wide_runs INTEGER, noball_runs INTEGER,
    bye_runs INTEGER, legbye_runs INTEGER, penalty_runs INTEGER,
    is_legal BOOLEAN, is_wide BOOLEAN, is_noball BOOLEAN, is_four BOOLEAN, is_six BOOLEAN,
    is_wicket BOOLEAN, bowler_wicket BOOLEAN, dismissal_type VARCHAR, player_out_id BIGINT, player_out VARCHAR,
    short_text VARCHAR, commentary VARCHAR,
    PRIMARY KEY (match_id, inning, sequence)
);
CREATE TABLE IF NOT EXISTS players (
    player_id BIGINT, name VARCHAR, source VARCHAR, PRIMARY KEY (player_id, source)
);

CREATE OR REPLACE VIEW deliveries AS
SELECT 'cricsheet' AS source, * FROM cricsheet_balls
UNION ALL BY NAME
SELECT 'espn' AS source, * FROM balls WHERE match_id NOT IN (SELECT DISTINCT match_id FROM cricsheet_balls);
"""

# The same three scorecard views per source; Cricsheet also records penalty runs (not the bowler's).
CARD_VIEWS = """
CREATE OR REPLACE VIEW {prefix}innings_card AS
SELECT match_id, inning, any_value(batting_team) AS team,
       sum(runs_total) AS runs, count(*) FILTER (WHERE is_wicket) AS wickets,
       count(*) FILTER (WHERE is_legal) AS balls,
       sum(extras) AS extras, sum(wide_runs) AS wides, sum(noball_runs) AS noballs,
       sum(bye_runs) AS byes, sum(legbye_runs) AS legbyes
FROM {table} GROUP BY match_id, inning;

CREATE OR REPLACE VIEW {prefix}batting_card AS
SELECT match_id, inning, batter_id AS player_id, any_value(batter) AS player,
       sum(runs_bat) AS runs, count(*) FILTER (WHERE NOT is_wide) AS balls,
       count(*) FILTER (WHERE is_four) AS fours, count(*) FILTER (WHERE is_six) AS sixes,
       min(sequence) AS first_ball
FROM {table} GROUP BY match_id, inning, batter_id;

CREATE OR REPLACE VIEW {prefix}bowling_card AS
SELECT match_id, inning, bowler_id AS player_id, any_value(bowler) AS player,
       count(*) FILTER (WHERE is_legal) AS balls,
       sum(runs_total - bye_runs - legbye_runs{penalty}) AS conceded,
       count(*) FILTER (WHERE bowler_wicket) AS wickets,
       count(*) FILTER (WHERE is_wide) AS wides, count(*) FILTER (WHERE is_noball) AS noballs,
       count(*) FILTER (WHERE is_legal AND runs_total = 0) AS dots
FROM {table} GROUP BY match_id, inning, bowler_id;
"""
SCHEMA += "".join(CARD_VIEWS.format(prefix=prefix, table=table, penalty=penalty)
                  for prefix, table, penalty in (("", "balls", ""), ("cs_", "cricsheet_balls", " - penalty_runs")))

REF_COLUMNS = {
    "ref_innings": ("match_id", "inning", "team", "runs", "wickets", "balls", "extras", "byes", "legbyes",
                    "wides", "noballs", "penalties", "is_super_over"),
    "ref_batting": ("match_id", "inning", "position", "player_id", "player", "runs", "balls", "fours", "sixes",
                    "is_out", "dismissal"),
    "ref_bowling": ("match_id", "inning", "player_id", "player", "balls", "conceded", "wickets", "wides",
                    "noballs", "dots"),
}
MATCH_COLUMNS = ("match_id", "season", "series_path", "match_path", "title", "start_date", "venue", "team1",
                 "team2", "winner", "status_text", "format", "stage", "is_cancelled")


def connect(path: str | Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path or DEFAULT_DB)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


def _insert(con: duckdb.DuckDBPyConnection, table: str, columns: Iterable[str], rows: list[dict[str, Any]]) -> None:
    columns = tuple(columns)
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [tuple(r.get(c) for c in columns) for r in rows],
    )


def upsert_matches(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ids = [r["match_id"] for r in rows]
    con.execute("DELETE FROM matches WHERE match_id IN (SELECT unnest(?))", [ids])
    _insert(con, "matches", MATCH_COLUMNS, rows)


def replace_match_data(
    con: duckdb.DuckDBPyConnection,
    match_id: int,
    balls: list[dict[str, Any]],
    reference: dict[str, list[dict[str, Any]]],
    log: dict[str, Any],
) -> None:
    """Atomically replace everything stored for one match (idempotent re-ingest)."""
    con.execute("BEGIN")
    try:
        for table in ("balls", *REF_COLUMNS, "ingest_log"):
            con.execute(f"DELETE FROM {table} WHERE match_id = ?", [match_id])
        _insert(con, "balls", BALL_COLUMNS, balls)
        for table, cols in REF_COLUMNS.items():
            _insert(con, table, cols, reference.get(table, []))
        _insert(con, "ingest_log", ("match_id", "status", "ball_items", "expected_items", "error"),
                [{"match_id": match_id, **log}])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def completed_match_ids(con: duckdb.DuckDBPyConnection) -> set[int]:
    return {row[0] for row in con.execute("SELECT match_id FROM ingest_log WHERE status = 'ok'").fetchall()}
