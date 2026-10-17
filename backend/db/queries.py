"""Ready-made stats queries over the local DuckDB archive.

Run from the project root (the folder that contains `backend` and `data`):

    python -m backend.db.queries seasons                         # coverage + run rates per IPL year
    python -m backend.db.queries top-batters --year 2025         # Orange Cap table
    python -m backend.db.queries top-bowlers --year 2016-2018    # a range of years
    python -m backend.db.queries top-batters                     # all time (no --year)
    python -m backend.db.queries player kohli                    # season by season + phase splits
    python -m backend.db.queries phase death --role bowl --year 2025 --min-balls 60
    python -m backend.db.queries matches --year 2016 --team RCB  # find match ids
    python -m backend.db.queries match 981019                    # full card rebuilt from balls
    python -m backend.db.queries records --year 2025             # highest totals, best figures, fastest 50s
    python -m backend.db.queries h2h MI CSK
    python -m backend.db.queries commentary 981019 --search six
    python -m backend.db.queries sql "SELECT count(*) FROM d"    # ad hoc SQL on the views below

Add --csv out.csv to any command to save the table(s), --limit N to change row counts.

Conventions (match official IPL stats):
  * Super overs are excluded from every stat.
  * Balls faced exclude wides; bowler runs exclude byes and leg byes; bowler wickets exclude
    run outs and retirements.
  * Phases: powerplay = overs 1-6, middle = 7-15, death = 16-20.
  * Year = calendar year of the edition (IPL 2020 was played in 2020 even though its
    series is labelled 2020/21).
  * Team codes are the current franchise codes: PBKS (incl. Kings XI Punjab), DC (Delhi
    Daredevils/Capitals), DCH (Deccan Chargers), SRH, RPS, GL, PWI, Kochi.

Views available to `sql`:
  d        one row per delivery (balls + year, venue, phase), super overs removed
  bat_inn  one row per batter innings: runs, balls, fours, sixes, is_out
  bowl_inn one row per bowler innings: balls, conceded, wickets
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from . import store

FRANCHISE_MACRO = """
CREATE OR REPLACE TEMP MACRO franchise(t) AS
    CASE t WHEN 'KXIP' THEN 'PBKS' WHEN 'DD' THEN 'DC' WHEN 'Kochi' THEN 'KTK' ELSE t END;
-- One name per ground, whatever it was called that season (Kotla -> Arun Jaitley Stadium, ...).
CREATE OR REPLACE TEMP MACRO ground(v) AS
    CASE split_part(v, ',', 1)
        WHEN 'Feroz Shah Kotla' THEN 'Arun Jaitley Stadium'
        WHEN 'Sardar Patel Stadium' THEN 'Narendra Modi Stadium'
        WHEN 'Punjab Cricket Association Stadium' THEN 'Punjab Cricket Association IS Bindra Stadium'
        WHEN 'Sheikh Zayed Stadium' THEN 'Zayed Cricket Stadium'
        WHEN 'Subrata Roy Sahara Stadium' THEN 'Maharashtra Cricket Association Stadium'
        WHEN 'Andhra Cricket Association-Visakhapatnam District Cricket Association Stadium'
            THEN 'Dr. Y.S. Rajasekhara Reddy ACA-VDCA Cricket Stadium'
        ELSE split_part(v, ',', 1) END;
"""


def source_views(tables: set[str]) -> str:
    """Temp views over whatever this database holds (older files may lack the Cricsheet tables)."""
    has_cs = "cricsheet_balls" in tables
    src = ("SELECT 'cricsheet' AS source, * FROM cricsheet_balls UNION ALL BY NAME "
           "SELECT 'espn' AS source, * FROM balls WHERE match_id NOT IN (SELECT DISTINCT match_id FROM cricsheet_balls)"
           if has_cs else "SELECT 'espn' AS source, *, 0 AS penalty_runs FROM balls")
    cs_names = "UNION ALL SELECT player_id, NULL, name FROM players" if "players" in tables else ""
    return f"""
CREATE OR REPLACE TEMP VIEW src AS {src};

-- ESPN spells names in full (Virat Kohli); Cricsheet uses initials (V Kohli). Same ids in both.
CREATE OR REPLACE TEMP VIEW pnames AS
SELECT player_id, coalesce(max(espn), max(cs)) AS name FROM (
    SELECT batter_id AS player_id, batter AS espn, NULL AS cs FROM balls
    UNION ALL SELECT bowler_id, bowler, NULL FROM balls
    {cs_names}
) WHERE player_id IS NOT NULL GROUP BY player_id;

CREATE OR REPLACE TEMP VIEW d AS
SELECT s.* REPLACE (coalesce(nb.name, s.batter) AS batter, coalesce(nw.name, s.bowler) AS bowler,
                    coalesce(no.name, s.player_out) AS player_out, franchise(s.batting_team) AS batting_team),
       CASE WHEN franchise(m.team1) = franchise(s.batting_team) THEN franchise(m.team2)
            ELSE franchise(m.team1) END AS bowling_team,
       s.runs_total - s.bye_runs - s.legbye_runs - coalesce(s.penalty_runs, 0) AS bowler_runs,
       year(m.start_date) AS year, m.start_date, m.venue, m.title AS match_title,
       CASE WHEN s.over_number <= 6 THEN 'powerplay' WHEN s.over_number <= 15 THEN 'middle' ELSE 'death' END AS phase
FROM src s JOIN matches m USING (match_id)
LEFT JOIN pnames nb ON nb.player_id = s.batter_id
LEFT JOIN pnames nw ON nw.player_id = s.bowler_id
LEFT JOIN pnames no ON no.player_id = s.player_out_id
WHERE s.inning <= 2;
"""


VIEWS = """
-- A batter innings exists if the player faced a ball OR was dismissed (non-striker run out for 0 off 0).
CREATE OR REPLACE TEMP VIEW bat_inn AS
WITH faced AS (
    SELECT match_id, inning, year, batter_id AS player_id, any_value(batter) AS player,
           any_value(batting_team) AS team,
           sum(runs_bat) AS runs, count(*) FILTER (WHERE NOT is_wide) AS balls,
           count(*) FILTER (WHERE is_four) AS fours, count(*) FILTER (WHERE is_six) AS sixes,
           min(sequence) AS first_ball
    FROM d GROUP BY match_id, inning, year, batter_id
), outs AS (
    SELECT match_id, inning, year, player_out_id AS player_id, any_value(player_out) AS player,
           any_value(batting_team) AS team
    FROM d WHERE is_wicket AND player_out_id IS NOT NULL GROUP BY match_id, inning, year, player_out_id
)
SELECT coalesce(f.match_id, o.match_id) AS match_id, coalesce(f.inning, o.inning) AS inning,
       coalesce(f.year, o.year) AS year, coalesce(f.player_id, o.player_id) AS player_id,
       coalesce(f.player, o.player) AS player, coalesce(f.team, o.team) AS team,
       coalesce(f.runs, 0) AS runs, coalesce(f.balls, 0) AS balls,
       coalesce(f.fours, 0) AS fours, coalesce(f.sixes, 0) AS sixes, o.player_id IS NOT NULL AS is_out,
       f.first_ball
FROM faced f FULL OUTER JOIN outs o USING (match_id, inning, player_id);

CREATE OR REPLACE TEMP VIEW bowl_inn AS
SELECT match_id, inning, year, bowler_id AS player_id, any_value(bowler) AS player,
       any_value(bowling_team) AS team,
       count(*) FILTER (WHERE is_legal) AS balls,
       sum(bowler_runs) AS conceded,
       count(*) FILTER (WHERE bowler_wicket) AS wickets
FROM d GROUP BY match_id, inning, year, bowler_id;
"""

YEARS = "year BETWEEN $y0 AND $y1"

BATTING_AGG = """
    sum(runs) AS runs, count(*) AS inns, count(*) FILTER (WHERE is_out) AS outs,
    round(sum(runs) / nullif(count(*) FILTER (WHERE is_out), 0), 2) AS avg,
    round(100.0 * sum(runs) / nullif(sum(balls), 0), 2) AS sr,
    count(*) FILTER (WHERE runs BETWEEN 50 AND 99) AS "50s", count(*) FILTER (WHERE runs >= 100) AS "100s",
    sum(fours) AS "4s", sum(sixes) AS "6s",
    max(runs) AS hs
"""

BAT_PHASE = f"""
        SELECT phase, sum(runs_bat) AS runs, count(*) FILTER (WHERE NOT is_wide) AS balls,
               round(100.0 * sum(runs_bat) / nullif(count(*) FILTER (WHERE NOT is_wide), 0), 2) AS sr,
               round(100.0 * count(*) FILTER (WHERE NOT is_wide AND runs_bat = 0) / nullif(count(*) FILTER (WHERE NOT is_wide), 0), 1) AS dot_pct,
               count(*) FILTER (WHERE is_four OR is_six) AS boundaries,
               (SELECT count(*) FROM d o WHERE o.player_out_id = $pid AND o.is_wicket AND o.phase = d.phase AND o.{YEARS}) AS outs
        FROM d WHERE batter_id = $pid AND {YEARS}
        GROUP BY phase ORDER BY list_position(['powerplay', 'middle', 'death'], phase)"""

BOWLING_AGG = """
    sum(wickets) AS wkts, sum(balls) // 6 || '.' || sum(balls) % 6 AS overs, sum(conceded) AS runs,
    round(6.0 * sum(conceded) / nullif(sum(balls), 0), 2) AS econ,
    round(sum(conceded) / nullif(sum(wickets), 0), 2) AS avg,
    round(sum(balls) / nullif(sum(wickets), 0), 1) AS sr,
    count(*) FILTER (WHERE wickets >= 4) AS "4w+",
    arg_max(wickets || '/' || conceded, wickets * 1000 - conceded) AS best
"""


# ----------------------------------------------------------------- queries

def seasons(q: "Q", a: argparse.Namespace) -> None:
    q.table("Per IPL year (balls-based stats exclude super overs)", f"""
        WITH listed AS (
            SELECT year(start_date) AS year, count(*) AS listed FROM matches GROUP BY 1
        ), inn AS (
            SELECT year, match_id, inning, sum(runs_total) AS runs, count(*) FILTER (WHERE is_legal) AS balls
            FROM d GROUP BY ALL
        )
        SELECT l.year, l.listed AS matches, count(DISTINCT i.match_id) AS with_balls,
               round(6.0 * sum(i.runs) / nullif(sum(i.balls), 0), 2) AS run_rate,
               round(avg(i.runs) FILTER (WHERE i.inning = 1), 1) AS avg_1st_inns,
               max(i.runs) AS highest,
               (SELECT count(*) FROM d WHERE d.year = l.year AND is_six) AS sixes
        FROM listed l LEFT JOIN inn i USING (year)
        WHERE l.year BETWEEN $y0 AND $y1
        GROUP BY l.year, l.listed ORDER BY l.year""")


BAT_SORTS = {"runs": ("Top run scorers", 'runs DESC, sr DESC'),
             "sr": ("Highest strike rate", 'sr DESC NULLS LAST, runs DESC'),
             "avg": ("Highest batting average", 'avg DESC NULLS LAST, runs DESC'),
             "sixes": ("Most sixes", '"6s" DESC, runs DESC'),
             "fours": ("Most fours", '"4s" DESC, runs DESC'),
             "fifties": ("Most fifties", '"50s" DESC, runs DESC'),
             "hundreds": ("Most hundreds", '"100s" DESC, runs DESC'),
             "hs": ("Highest score", 'hs DESC, runs DESC')}
BOWL_SORTS = {"wkts": ("Top wicket takers", 'wkts DESC, econ ASC'),
              "econ": ("Best economy", 'econ ASC NULLS LAST, wkts DESC'),
              "avg": ("Best bowling average", 'avg ASC NULLS LAST, wkts DESC'),
              "sr": ("Best bowling strike rate", 'sr ASC NULLS LAST, wkts DESC'),
              "4w": ("Most four-wicket hauls", '"4w+" DESC, wkts DESC')}
RATE_SORTS = {"sr", "avg", "econ"}


def _leaderboard_filters(q: "Q", a: argparse.Namespace, rate: bool, season_min: int) -> str:
    """Team filter, and a minimum-balls floor for rate stats so one short innings cannot top them."""
    q.params["team"] = q.team(getattr(a, "team", None))
    if rate and not a.min_balls:
        q.params["min_balls"] = season_min if getattr(a, "year", None) else season_min * 5
    parts = [q.span]
    if q.params["team"]:
        parts.append(f"for {q.params['team']}")
    if q.params["min_balls"]:
        parts.append(f"(min {q.params['min_balls']} balls)")
    return " ".join(parts)


def top_batters(q: "Q", a: argparse.Namespace) -> None:
    label, order = BAT_SORTS[a.sort]
    scope = _leaderboard_filters(q, a, a.sort in RATE_SORTS, 100)
    q.table(f"{label} {scope}", f"""
        SELECT player, {BATTING_AGG}
        FROM bat_inn WHERE {YEARS} AND ($team = '' OR team = $team)
        GROUP BY player_id, player HAVING sum(balls) >= $min_balls
        ORDER BY {order} LIMIT $limit""")


def top_bowlers(q: "Q", a: argparse.Namespace) -> None:
    label, order = BOWL_SORTS[a.sort]
    scope = _leaderboard_filters(q, a, a.sort in RATE_SORTS, 120)
    q.table(f"{label} {scope}", f"""
        SELECT player, {BOWLING_AGG}
        FROM bowl_inn WHERE {YEARS} AND ($team = '' OR team = $team)
        GROUP BY player_id, player HAVING sum(balls) >= $min_balls
        ORDER BY {order} LIMIT $limit""")


def _resolve_player(q: "Q", name: str) -> tuple[int, str] | None:
    """A unique player for a name or id; otherwise a note or a table of candidates, and None."""
    found = q.rows(PLAYER_SEARCH, name=str(name))
    tokens = str(name).split()
    if not found and len(tokens) >= 2:
        # Cricsheet spells first names as initials: "Jofra Archer" is "JC Archer".
        found = [r for r in q.rows(PLAYER_SEARCH, name=" ".join(tokens[1:]))
                 if r["player"][:1].casefold() == tokens[0][:1].casefold()]
    if not found:
        q.note(f"No player matching '{name}'.")
        return None
    exact = [r for r in found if r["player"].casefold() == str(name).casefold() or str(r["player_id"]) == str(name)]
    if exact:
        found = exact
    elif len(found) > 1:
        q.show(f"Several players match '{name}' - use the player_id or the full name", found)
        return None
    return found[0]["player_id"], found[0]["player"]


def player(q: "Q", a: argparse.Namespace) -> None:
    resolved = _resolve_player(q, a.name)
    if not resolved:
        return
    pid, name = resolved
    q.params["pid"] = pid
    did = q.rows(f"""SELECT (SELECT count(*) FROM bat_inn WHERE player_id = $pid AND {YEARS}) AS bat,
                            (SELECT count(*) FROM bowl_inn WHERE player_id = $pid AND {YEARS}) AS bowl""")[0]
    if not did["bat"] and not did["bowl"]:
        q.note(f"{name} has no innings {q.span}.")
        return
    # Main role first: a bowler's profile opens with bowling.
    sections = [(did["bat"], _player_batting), (did["bowl"], _player_bowling)]
    for count, show in sorted(sections, key=lambda item: -item[0]):
        if count:
            show(q, name)


def matchup(q: "Q", a: argparse.Namespace) -> None:
    """Every ball a batter faced from one bowler."""
    batter, bowler = _resolve_player(q, a.batter), _resolve_player(q, a.bowler)
    if not batter or not bowler:
        return
    q.params.update(bat=batter[0], bowl=bowler[0])
    q.table(f"{batter[1]} v {bowler[1]} {q.span} (balls exclude wides; outs = dismissed by this bowler)", f"""
        SELECT coalesce(CAST(year AS VARCHAR), 'Total') AS year,
               count(*) FILTER (WHERE NOT is_wide) AS balls, sum(runs_bat) AS runs,
               count(*) FILTER (WHERE bowler_wicket AND player_out_id = $bat) AS outs,
               round(100.0 * sum(runs_bat) / nullif(count(*) FILTER (WHERE NOT is_wide), 0), 2) AS sr,
               round(100.0 * count(*) FILTER (WHERE NOT is_wide AND runs_bat = 0)
                     / nullif(count(*) FILTER (WHERE NOT is_wide), 0), 1) AS dot_pct,
               count(*) FILTER (WHERE is_four) AS "4s", count(*) FILTER (WHERE is_six) AS "6s"
        FROM d WHERE batter_id = $bat AND bowler_id = $bowl AND {YEARS}
        GROUP BY ROLLUP (year) ORDER BY year NULLS LAST""")


def _player_batting(q: "Q", name: str) -> None:
    q.table(f"{name}: batting by year {q.span}", f"""
        SELECT coalesce(CAST(year AS VARCHAR), 'Total') AS year, {BATTING_AGG}
        FROM bat_inn WHERE player_id = $pid AND {YEARS}
        GROUP BY ROLLUP (year) ORDER BY year NULLS LAST""")
    q.table(f"{name}: batting by phase {q.span}", BAT_PHASE)


def _player_bowling(q: "Q", name: str) -> None:
    q.table(f"{name}: bowling by year {q.span}", f"""
        SELECT coalesce(CAST(year AS VARCHAR), 'Total') AS year, count(*) AS inns, {BOWLING_AGG}
        FROM bowl_inn WHERE player_id = $pid AND {YEARS}
        GROUP BY ROLLUP (year) ORDER BY year NULLS LAST""")
    q.table(f"{name}: bowling by phase {q.span}", f"""
        SELECT phase, count(*) FILTER (WHERE is_legal) AS balls, sum(bowler_runs) AS runs,
               round(6.0 * sum(bowler_runs) / nullif(count(*) FILTER (WHERE is_legal), 0), 2) AS econ,
               round(100.0 * count(*) FILTER (WHERE is_legal AND runs_total = 0) / nullif(count(*) FILTER (WHERE is_legal), 0), 1) AS dot_pct,
               count(*) FILTER (WHERE bowler_wicket) AS wkts
        FROM d WHERE bowler_id = $pid AND {YEARS}
        GROUP BY phase ORDER BY list_position(['powerplay', 'middle', 'death'], phase)""")


def phase(q: "Q", a: argparse.Namespace) -> None:
    q.params["phase"] = a.phase
    if a.role == "bat":
        q.table(f"Best {a.phase} batters by strike rate {q.span} (min {a.min_balls} balls)", f"""
            SELECT any_value(batter) AS player, sum(runs_bat) AS runs, count(*) FILTER (WHERE NOT is_wide) AS balls,
                   round(100.0 * sum(runs_bat) / count(*) FILTER (WHERE NOT is_wide), 2) AS sr,
                   count(*) FILTER (WHERE is_six) AS "6s",
                   round(100.0 * count(*) FILTER (WHERE NOT is_wide AND runs_bat = 0) / count(*) FILTER (WHERE NOT is_wide), 1) AS dot_pct
            FROM d WHERE phase = $phase AND {YEARS}
            GROUP BY batter_id HAVING count(*) FILTER (WHERE NOT is_wide) >= $min_balls
            ORDER BY sr DESC LIMIT $limit""")
    else:
        q.table(f"Best {a.phase} bowlers by economy {q.span} (min {a.min_balls} balls)", f"""
            SELECT any_value(bowler) AS player, count(*) FILTER (WHERE is_legal) AS balls,
                   sum(bowler_runs) AS runs,
                   round(6.0 * sum(bowler_runs) / count(*) FILTER (WHERE is_legal), 2) AS econ,
                   count(*) FILTER (WHERE bowler_wicket) AS wkts,
                   round(100.0 * count(*) FILTER (WHERE is_legal AND runs_total = 0) / count(*) FILTER (WHERE is_legal), 1) AS dot_pct
            FROM d WHERE phase = $phase AND {YEARS}
            GROUP BY bowler_id HAVING count(*) FILTER (WHERE is_legal) >= $min_balls
            ORDER BY econ ASC LIMIT $limit""")


def matches(q: "Q", a: argparse.Namespace) -> None:
    q.params["team"] = q.team(a.team)
    q.params["vs"] = q.team(a.vs)
    q.table(f"Matches {q.span}", f"""
        SELECT m.match_id, m.start_date AS date, m.title, franchise(m.team1) AS team1, franchise(m.team2) AS team2,
               m.status_text AS result,
               coalesce((SELECT any_value(source) FROM src WHERE src.match_id = m.match_id), 'no balls') AS data
        FROM matches m
        WHERE year(m.start_date) BETWEEN $y0 AND $y1
          AND ($team = '' OR $team IN (franchise(m.team1), franchise(m.team2)))
          AND ($vs = '' OR $vs IN (franchise(m.team1), franchise(m.team2)))
        ORDER BY m.start_date, m.match_id LIMIT $limit""")


def match(q: "Q", a: argparse.Namespace) -> None:
    q.params["mid"] = a.match_id
    info = q.rows("""SELECT m.*, (SELECT any_value(source) FROM src WHERE src.match_id = m.match_id) AS source
                     FROM matches m WHERE match_id = $mid""")
    if not info:
        q.note(f"Match {a.match_id} is not in the database. Find ids with: python -m backend.db.queries matches --year YYYY")
        return
    m = info[0]
    q.note(f"{m['title']}, {m['start_date']}, {m['venue']}\n{m['status_text']}   [balls: {m['source'] or 'none'}]")
    for (inning,) in q.con.execute("SELECT DISTINCT inning FROM d WHERE match_id = $mid ORDER BY 1", {"mid": a.match_id}).fetchall():
        q.params["inn"] = inning
        tot = q.rows("""SELECT any_value(batting_team) AS team, sum(runs_total) AS runs,
                               count(*) FILTER (WHERE is_wicket) AS wkts, count(*) FILTER (WHERE is_legal) AS balls,
                               sum(extras) AS extras, sum(wide_runs) AS w, sum(noball_runs) AS nb,
                               sum(bye_runs) AS b, sum(legbye_runs) AS lb
                        FROM d WHERE match_id = $mid AND inning = $inn""")[0]
        overs = f"{tot['balls'] // 6}.{tot['balls'] % 6}" if tot["balls"] % 6 else str(tot["balls"] // 6)
        q.note(f"=== Innings {inning}: {tot['team']} {tot['runs']}/{tot['wkts']} ({overs} ov)  "
               f"extras {tot['extras']} (w {tot['w']}, nb {tot['nb']}, b {tot['b']}, lb {tot['lb']})")
        q.table("Batting", """
            SELECT bi.player,
                   coalesce(r.dismissal,
                            CASE WHEN w.kind IS NULL THEN 'not out'
                                 WHEN w.bowler_wicket THEN w.kind || ' b ' || w.bowler ELSE w.kind END) AS dismissal,
                   bi.runs AS r, bi.balls AS b, bi.fours AS "4s", bi.sixes AS "6s",
                   round(100.0 * bi.runs / nullif(bi.balls, 0), 1) AS sr
            FROM bat_inn bi
            LEFT JOIN ref_batting r ON r.match_id = bi.match_id AND r.inning = bi.inning AND r.player_id = bi.player_id
            LEFT JOIN (SELECT player_out_id, any_value(dismissal_type) AS kind, any_value(bowler) AS bowler,
                              bool_or(bowler_wicket) AS bowler_wicket
                       FROM d WHERE match_id = $mid AND inning = $inn AND is_wicket GROUP BY player_out_id) w
                   ON w.player_out_id = bi.player_id
            WHERE bi.match_id = $mid AND bi.inning = $inn
            ORDER BY coalesce(r.position, 99), bi.first_ball NULLS LAST""")
        q.table("Bowling", """
            SELECT player, balls // 6 || '.' || balls % 6 AS o, conceded AS r, wickets AS w,
                   round(6.0 * conceded / nullif(balls, 0), 2) AS econ
            FROM bowl_inn WHERE match_id = $mid AND inning = $inn
            ORDER BY (SELECT min(sequence) FROM d WHERE d.match_id = $mid AND d.inning = $inn AND d.bowler_id = bowl_inn.player_id)""")
        q.table("Phases", """
            SELECT phase, sum(runs_total) AS runs, count(*) FILTER (WHERE is_wicket) AS wkts,
                   round(6.0 * sum(runs_total) / nullif(count(*) FILTER (WHERE is_legal), 0), 2) AS run_rate,
                   count(*) FILTER (WHERE is_four) AS "4s", count(*) FILTER (WHERE is_six) AS "6s"
            FROM d WHERE match_id = $mid AND inning = $inn
            GROUP BY phase ORDER BY list_position(['powerplay', 'middle', 'death'], phase)""")


def records(q: "Q", a: argparse.Namespace) -> None:
    q.table(f"Highest team totals {q.span}", f"""
        SELECT any_value(batting_team) AS team, sum(runs_total) || '/' || count(*) FILTER (WHERE is_wicket) AS score,
               count(*) FILTER (WHERE is_legal) // 6 || '.' || count(*) FILTER (WHERE is_legal) % 6 AS overs,
               any_value(year) AS year, any_value(match_title) AS match, match_id
        FROM d WHERE {YEARS} GROUP BY match_id, inning ORDER BY sum(runs_total) DESC LIMIT $limit""")
    q.table(f"Highest individual scores {q.span}", f"""
        SELECT player, team, runs || CASE WHEN is_out THEN '' ELSE '*' END AS score, balls,
               round(100.0 * runs / nullif(balls, 0), 1) AS sr, sixes AS "6s", year, match_id
        FROM bat_inn WHERE {YEARS} ORDER BY runs DESC, balls ASC LIMIT $limit""")
    q.table(f"Best bowling figures {q.span}", f"""
        SELECT player, wickets || '/' || conceded AS figures, balls // 6 || '.' || balls % 6 AS overs, year, match_id
        FROM bowl_inn WHERE {YEARS} ORDER BY wickets DESC, conceded ASC LIMIT $limit""")
    q.table(f"Fastest fifties {q.span} (balls faced to reach 50)", f"""
        WITH c AS (
            SELECT match_id, inning, year, batter_id, batter, runs_bat,
                   sum(runs_bat) OVER w AS cum_runs, sum(CASE WHEN is_wide THEN 0 ELSE 1 END) OVER w AS cum_balls
            FROM d WHERE {YEARS}
            WINDOW w AS (PARTITION BY match_id, inning, batter_id ORDER BY sequence ROWS UNBOUNDED PRECEDING)
        )
        SELECT any_value(batter) AS player, min(cum_balls) FILTER (WHERE cum_runs >= 50) AS balls_to_50,
               any_value(year) AS year, match_id
        FROM c GROUP BY match_id, inning, batter_id
        HAVING balls_to_50 IS NOT NULL ORDER BY balls_to_50, year LIMIT $limit""")
    q.table(f"Most sixes in an innings {q.span}", f"""
        SELECT player, team, sixes AS "6s", runs || CASE WHEN is_out THEN '' ELSE '*' END AS score, balls, year, match_id
        FROM bat_inn WHERE {YEARS} ORDER BY sixes DESC, runs DESC LIMIT $limit""")


def venues(q: "Q", a: argparse.Namespace) -> None:
    q.params["venue"] = a.name or ""
    q.table(f"Grounds {q.span}" + (f" matching '{a.name}'" if a.name else " by matches played"), f"""
        WITH inn AS (
            SELECT match_id, inning, any_value(batting_team) AS team, sum(runs_total) AS runs,
                   count(*) FILTER (WHERE is_legal) AS balls
            FROM d WHERE {YEARS} GROUP BY match_id, inning
        )
        SELECT ground(m.venue) AS ground, count(*) AS matches,
               round(avg(i1.runs), 1) AS avg_1st_inns,
               count(*) FILTER (WHERE franchise(m.winner) = i1.team) AS bat_first_won,
               count(*) FILTER (WHERE franchise(m.winner) = i2.team) AS chasing_won,
               max(greatest(i1.runs, coalesce(i2.runs, 0))) AS highest_total,
               round(6.0 * sum(i1.runs + coalesce(i2.runs, 0)) / nullif(sum(i1.balls + coalesce(i2.balls, 0)), 0), 2) AS run_rate,
               min(year(m.start_date)) AS first, max(year(m.start_date)) AS last
        FROM matches m
        JOIN inn i1 ON i1.match_id = m.match_id AND i1.inning = 1
        LEFT JOIN inn i2 ON i2.match_id = m.match_id AND i2.inning = 2
        GROUP BY ground(m.venue)
        HAVING $venue = '' OR ground(m.venue) ILIKE '%' || $venue || '%' OR bool_or(m.venue ILIKE '%' || $venue || '%')
        ORDER BY matches DESC LIMIT $limit""")


def ground_report(q: "Q", a: argparse.Namespace) -> None:
    """One ground against the league in the same seasons: what scoring, phases and dismissals look like.

    These are outcomes, not a pitch reading: they mix the surface with boundary size, dew, and who
    played there. Comparing with the league in the same seasons removes the era (2008 is not 2024)."""
    q.params["venue"] = a.name
    found = q.rows("""
        SELECT ground(venue) AS ground, count(*) AS n FROM matches
        WHERE year(start_date) BETWEEN $y0 AND $y1
          AND (ground(venue) ILIKE '%' || $venue || '%' OR venue ILIKE '%' || $venue || '%')
        GROUP BY 1 ORDER BY n DESC LIMIT 1""")
    if not found:
        q.note(f"No IPL ground matching '{a.name}' {q.span}.")
        return
    name = found[0]["ground"]
    q.params["g"] = name
    inn = f"""inn AS (
            SELECT match_id, inning, year, ground(any_value(venue)) AS ground, any_value(batting_team) AS team,
                   sum(runs_total) AS runs, count(*) FILTER (WHERE is_legal) AS balls
            FROM d WHERE {YEARS} GROUP BY match_id, inning, year),
        games AS (
            SELECT i1.match_id, i1.year, i1.ground, i1.runs AS first, i1.balls AS b1, i2.runs AS second, i2.balls AS b2,
                   franchise(m.winner) = i2.team AS chased
            FROM inn i1 JOIN matches m USING (match_id)
            LEFT JOIN inn i2 ON i2.match_id = i1.match_id AND i2.inning = 2
            WHERE i1.inning = 1)"""
    q.table(f"{name}: scoring by season, against the league {q.span}", f"""
        WITH {inn},
        league AS (SELECT year, avg(first) AS l_first, 6.0 * sum(first + coalesce(second, 0)) / sum(b1 + coalesce(b2, 0)) AS l_rr
                   FROM games GROUP BY year)
        SELECT g.year, count(*) AS matches, round(avg(first), 1) AS avg_1st_inns, round(any_value(l_first), 1) AS league_1st,
               round(avg(first) - any_value(l_first), 1) AS vs_league,
               round(6.0 * sum(first + coalesce(second, 0)) / sum(b1 + coalesce(b2, 0)), 2) AS run_rate,
               round(any_value(l_rr), 2) AS league_rr,
               round(100.0 * count(*) FILTER (WHERE chased) / nullif(count(*) FILTER (WHERE second IS NOT NULL), 0), 0) AS chase_win_pct
        FROM games g JOIN league l USING (year) WHERE g.ground = $g
        GROUP BY ROLLUP (g.year) ORDER BY g.year NULLS LAST""")
    rows = q.blocks[-1]["rows"]
    if rows and rows[-1][0] is None:
        rows[-1][0] = "Total"
        cols = q.blocks[-1]["columns"]
        total = dict(zip(cols, rows[-1]))
        # The Total's league figures must be weighted by the seasons this ground hosted, not the overall average.
        season_rows = [dict(zip(cols, r)) for r in rows[:-1]]
        weight = sum(r["matches"] for r in season_rows) or 1
        rows[-1][cols.index("league_1st")] = round(sum(r["league_1st"] * r["matches"] for r in season_rows) / weight, 1)
        rows[-1][cols.index("vs_league")] = round(total["avg_1st_inns"] - rows[-1][cols.index("league_1st")], 1)
        rows[-1][cols.index("league_rr")] = round(sum(r["league_rr"] * r["matches"] for r in season_rows) / weight, 2)
    seasons = f"""years AS (SELECT DISTINCT year FROM d WHERE ground(venue) = $g AND {YEARS})"""
    q.table(f"{name}: by phase, against the league in the same seasons {q.span}", f"""
        WITH {seasons},
        x AS (SELECT ground(venue) = $g AS here, phase, match_id, inning, is_legal, runs_total, is_four, is_six,
                     runs_total = 0 AND is_legal AS dot, is_wicket AND bowler_wicket AS wkt
              FROM d WHERE year IN (SELECT year FROM years)),
        agg AS (SELECT here, phase, 6.0 * sum(runs_total) / count(*) FILTER (WHERE is_legal) AS rr,
                       100.0 * count(*) FILTER (WHERE is_four OR is_six) / count(*) FILTER (WHERE is_legal) AS bdry,
                       100.0 * count(*) FILTER (WHERE dot) / count(*) FILTER (WHERE is_legal) AS dots,
                       count(*) FILTER (WHERE wkt) * 1.0 / count(DISTINCT (match_id, inning)) AS wkts
                FROM x GROUP BY GROUPING SETS ((here, phase), (phase)))   -- here IS NULL: the whole league
        SELECT h.phase, round(h.rr, 2) AS run_rate, round(l.rr, 2) AS league_rr,
               round(h.bdry, 1) AS boundary_pct, round(l.bdry, 1) AS league_boundary_pct,
               round(h.dots, 1) AS dot_pct, round(l.dots, 1) AS league_dot_pct,
               round(h.wkts, 2) AS wkts_per_inns, round(l.wkts, 2) AS league_wkts
        FROM agg h JOIN agg l ON l.phase = h.phase AND l.here IS NULL WHERE h.here
        ORDER BY CASE h.phase WHEN 'powerplay' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END""")
    q.table(f"{name}: how wickets fall, against the league in the same seasons {q.span}", f"""
        WITH {seasons},
        w AS (SELECT ground(venue) = $g AS here,
                     CASE WHEN dismissal_type IN ('caught', 'caught and bowled') THEN 'caught'
                          WHEN dismissal_type IN ('bowled', 'lbw', 'stumped', 'run out') THEN dismissal_type
                          ELSE 'other' END AS how
              FROM d WHERE is_wicket AND year IN (SELECT year FROM years)),
        share AS (SELECT here, how, 100.0 * count(*) / sum(count(*)) OVER (PARTITION BY here) AS pct
                  FROM (SELECT here, how FROM w UNION ALL SELECT NULL, how FROM w) GROUP BY here, how)
        SELECT h.how AS dismissal, round(h.pct, 1) AS pct, round(l.pct, 1) AS league_pct
        FROM share h JOIN share l ON l.how = h.how AND l.here IS NULL WHERE h.here AND h.how <> 'other'
        ORDER BY h.pct DESC""")
    if rows and rows[-1][0] == "Total":
        t = dict(zip(q.blocks[-3]["columns"], rows[-1]))
        n = t["matches"]
        diff = t["vs_league"]
        lean = "about level with" if abs(diff) < 4 else (f"{diff} runs above" if diff > 0 else f"{-diff} runs below")
        q.note(f"{name}: {n} matches {q.span}. First-innings scores average {t['avg_1st_inns']}, {lean} the league "
               f"average in the same seasons ({t['league_1st']}); chasing sides won {t['chase_win_pct']:.0f}% of completed "
               f"chases. These are outcomes, not a pitch reading: they mix the surface with boundary size, dew and "
               f"the teams that played there." + (" Small sample: fewer than 10 matches." if n < 10 else ""))


def champions(q: "Q", a: argparse.Namespace) -> None:
    q.table(f"IPL finals {q.span}", f"""
        SELECT year(start_date) AS year, franchise(winner) AS champion,
               CASE WHEN franchise(winner) = franchise(team1) THEN franchise(team2) ELSE franchise(team1) END AS runner_up,
               status_text AS result, ground(venue) AS ground, match_id
        FROM matches WHERE title = 'Final' AND year(start_date) BETWEEN $y0 AND $y1 ORDER BY year""")
    q.table(f"Titles by team {q.span}", f"""
        SELECT franchise(winner) AS team, count(*) AS titles, string_agg(CAST(year(start_date) AS VARCHAR), ', ' ORDER BY start_date) AS years
        FROM matches WHERE title = 'Final' AND year(start_date) BETWEEN $y0 AND $y1
        GROUP BY 1 ORDER BY titles DESC, team LIMIT $limit""")


PLAYOFF = "(title ILIKE '%final%' OR title ILIKE '%qualif%' OR title ILIKE '%eliminator%')"


def team(q: "Q", a: argparse.Namespace) -> None:
    code = q.team(a.code)
    q.params["t"] = code
    q.table(f"{code} season by season {q.span} (from the match list; no result = abandoned or tied without a winner)", f"""
        WITH g AS (
            SELECT year(start_date) AS year, franchise(winner) AS winner, title
            FROM matches
            WHERE $t IN (franchise(team1), franchise(team2)) AND year(start_date) BETWEEN $y0 AND $y1
        ), s AS (
            SELECT year, count(*) AS played, count(*) FILTER (WHERE winner = $t) AS won,
                   count(*) FILTER (WHERE winner IS NOT NULL AND winner <> '' AND winner <> $t) AS lost,
                   count(*) FILTER (WHERE winner IS NULL OR winner = '') AS no_result,
                   CASE WHEN bool_or(title = 'Final' AND winner = $t) THEN 'Champions'
                        WHEN bool_or(title = 'Final') THEN 'Runner-up'
                        WHEN bool_or({PLAYOFF}) THEN 'Playoffs'
                        ELSE 'League stage' END AS finish
            FROM g GROUP BY year
        )
        SELECT CAST(year AS VARCHAR) AS year, played, won, lost, no_result,
               round(100.0 * won / nullif(won + lost, 0), 1) AS win_pct, finish
        FROM s
        UNION ALL
        SELECT 'Total', sum(played), sum(won), sum(lost), sum(no_result),
               round(100.0 * sum(won) / nullif(sum(won + lost), 0), 1),
               count(*) FILTER (WHERE finish = 'Champions') || ' titles'
        FROM s
        ORDER BY year""")


def h2h(q: "Q", a: argparse.Namespace) -> None:
    t1, t2 = q.team(a.team1), q.team(a.team2)
    q.params.update(t1=t1, t2=t2)
    c1, c2 = (f'"{"".join(ch for ch in t if ch.isalnum())} won"' for t in (t1, t2))   # safe column labels
    q.table(f"{t1} v {t2} {q.span} - results come from the match list, so every listed season counts", f"""
        WITH g AS (
            SELECT year(start_date) AS year, franchise(winner) AS winner, status_text
            FROM matches
            WHERE year(start_date) BETWEEN $y0 AND $y1
              AND $t1 IN (franchise(team1), franchise(team2)) AND $t2 IN (franchise(team1), franchise(team2))
        )
        SELECT coalesce(CAST(year AS VARCHAR), 'Total') AS year, count(*) AS played,
               count(*) FILTER (WHERE winner = $t1) AS {c1}, count(*) FILTER (WHERE winner = $t2) AS {c2},
               count(*) FILTER (WHERE winner IS NULL OR winner = '') AS no_result
        FROM g GROUP BY ROLLUP (year) ORDER BY year NULLS LAST""")


def commentary(q: "Q", a: argparse.Namespace) -> None:
    """Commentary text exists only in the ESPN feed; Cricsheet-only matches show generated event lines."""
    q.params.update(mid=a.match_id, s=a.search or "", inn=a.inning or 0, width=0 if a.full else 140)
    has_text = q.con.execute("SELECT count(*) FROM balls WHERE match_id = $mid AND commentary <> ''",
                             {"mid": a.match_id}).fetchone()[0]
    table = "balls" if has_text else "d"
    if not has_text:
        q.note("No ESPN commentary stored for this match (python -m backend.db.ingest adds it); "
               "showing Cricsheet event lines.")
    q.table(f"Commentary, match {a.match_id}" + (f" matching '{a.search}'" if a.search else ""), f"""
        SELECT inning AS inn, over_label AS ball, short_text AS event,
               CASE WHEN $width = 0 OR length(commentary) <= $width THEN commentary
                    ELSE left(commentary, $width) || '...' END AS commentary
        FROM {table} WHERE match_id = $mid AND inning <= 2 AND ($inn = 0 OR inning = $inn)
          AND ($s = '' OR short_text ILIKE '%' || $s || '%' OR commentary ILIKE '%' || $s || '%')
        ORDER BY inning, sequence LIMIT $limit""")


def players(q: "Q", a: argparse.Namespace) -> None:
    q.table(f"Players matching '{a.name}'", PLAYER_SEARCH, extra={"name": a.name})


def sql(q: "Q", a: argparse.Namespace) -> None:
    q.table("Result", a.query, use_params=False)


PLAYER_SEARCH = """
    SELECT player_id, any_value(player) AS player,
           count(*) FILTER (WHERE kind = 'bat') AS bat_inns, count(*) FILTER (WHERE kind = 'bowl') AS bowl_inns,
           min(year) AS first, max(year) AS last
    FROM (SELECT player_id, player, year, 'bat' AS kind FROM bat_inn
          UNION ALL SELECT player_id, player, year, 'bowl' FROM bowl_inn)
    WHERE player ILIKE '%' || $name || '%' OR CAST(player_id AS VARCHAR) = $name
    GROUP BY player_id ORDER BY bat_inns + bowl_inns DESC LIMIT 25"""


# ----------------------------------------------------------------- plumbing

class NoDatabase(RuntimeError):
    pass


class Q:
    """Runs one command and collects its output as blocks: {"type": "table"|"text", ...}."""

    def __init__(self, a: argparse.Namespace):
        path = Path(getattr(a, "db", None) or store.DEFAULT_DB)
        if not path.exists():
            raise NoDatabase(f"No database at {path}. Run `python -m backend.db.cricsheet_load --sync` first, or pass --db.")
        self.con = store.connect(path, read_only=True)
        tables = {r[0] for r in self.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        self.con.execute(FRANCHISE_MACRO + source_views(tables) + VIEWS)
        y0, y1 = parse_years(getattr(a, "year", None))
        self.span = "(all time)" if getattr(a, "year", None) is None else f"({a.year})"
        self.params: dict[str, Any] = {"y0": y0, "y1": y1, "limit": getattr(a, "limit", 15),
                                       "min_balls": getattr(a, "min_balls", 0) or 0}
        self.blocks: list[dict[str, Any]] = []

    @staticmethod
    def team(code: str | None) -> str:
        code = (code or "").strip().upper()
        return {"KXIP": "PBKS", "KOCHI": "KTK", "DD": "DC"}.get(code, code)

    def _run(self, sql_text: str, extra: dict[str, Any] | None = None, use_params: bool = True):
        if not use_params:
            cur = self.con.execute(sql_text)
        else:
            wanted = {k: v for k, v in {**self.params, **(extra or {})}.items() if f"${k}" in sql_text}
            cur = self.con.execute(sql_text, wanted)
        cols = [c[0] for c in cur.description]
        return cols, cur.fetchall()

    def rows(self, sql_text: str, **extra: Any) -> list[dict[str, Any]]:
        cols, data = self._run(sql_text, extra)
        return [dict(zip(cols, r)) for r in data]

    def table(self, title: str, sql_text: str, use_params: bool = True, extra: dict[str, Any] | None = None) -> None:
        cols, data = self._run(sql_text, extra, use_params=use_params)
        self.blocks.append({"type": "table", "title": title, "columns": cols, "rows": [list(r) for r in data]})

    def show(self, title: str, records: list[dict[str, Any]]) -> None:
        cols = list(records[0]) if records else []
        self.blocks.append({"type": "table", "title": title, "columns": cols, "rows": [list(r.values()) for r in records]})

    def note(self, text: str) -> None:
        self.blocks.append({"type": "text", "text": text})


def render(blocks: list[dict[str, Any]], csv_path: str | None = None) -> None:
    """Print blocks as plain-text tables (and optionally save each table as CSV)."""
    tables = 0
    for b in blocks:
        if b["type"] == "text":
            print(f"\n{b['text']}")
            continue
        tables += 1
        cols, data = b["columns"], b["rows"]
        print(f"\n{b['title']}")
        if not data:
            print("  (no rows - check which years are loaded with: python -m backend.db.queries seasons)")
            continue
        cells = [[("-" if v is None else str(v)) for v in row] for row in data]
        widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
        numeric = [all(_is_num(r[i]) for r in cells) for i in range(len(cols))]
        fmt = lambda vals: "  ".join(v.rjust(w) if n else v.ljust(w) for v, w, n in zip(vals, widths, numeric))  # noqa: E731
        print(fmt(cols))
        print("  ".join("-" * w for w in widths))
        for r in cells:
            print(fmt(r))
        if csv_path:
            out = Path(csv_path)
            if tables > 1:
                out = out.with_name(f"{out.stem}_{tables}{out.suffix}")
            with out.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                w.writerows(data)
            print(f"  -> saved {out}")


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return s == "-"


def parse_years(spec: str | None) -> tuple[int, int]:
    """'2025' -> (2025, 2025); '2016-2018' -> (2016, 2018); None -> all."""
    if not spec:
        return 0, 9999
    parts = [int(p) for p in str(spec).split("-")]
    return (parts[0], parts[-1])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"DuckDB file (default {store.DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, fn, help_text: str, year: bool = True, limit: int = 15, min_balls: int | None = None):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(fn=fn)
        if year:
            p.add_argument("--year", help="IPL year, e.g. 2025, or a range 2016-2018 (default: all time)")
        p.add_argument("--limit", type=int, default=limit, help=f"rows to show (default {limit})")
        if min_balls is not None:
            p.add_argument("--min-balls", type=int, default=min_balls, help=f"minimum balls (default {min_balls})")
        p.add_argument("--csv", help="also save the table(s) to this CSV file")
        return p

    cmd("seasons", seasons, "Coverage and scoring summary per IPL year", limit=100)
    p = cmd("top-batters", top_batters, "Batting leaderboard: most runs (Orange Cap) or any --sort", min_balls=0)
    p.add_argument("--sort", choices=list(BAT_SORTS), default="runs")
    p.add_argument("--team", help="only innings for this team, e.g. CSK")
    p = cmd("top-bowlers", top_bowlers, "Bowling leaderboard: most wickets (Purple Cap) or any --sort", min_balls=0)
    p.add_argument("--sort", choices=list(BOWL_SORTS), default="wkts")
    p.add_argument("--team", help="only spells for this team, e.g. MI")
    p = cmd("players", players, "Find players by name (returns player_id)", year=False)
    p.add_argument("name")
    p = cmd("player", player, "One player's batting and bowling by year and by phase", limit=100)
    p.add_argument("name", help="part of the name (kohli) or the player_id")
    p = cmd("matchup", matchup, "One batter against one bowler, by year", limit=100)
    p.add_argument("batter", help="name or player_id")
    p.add_argument("bowler", help="name or player_id")
    p = cmd("venues", venues, "Grounds: average first innings, bat-first vs chasing wins, highest total")
    p.add_argument("name", nargs="?", help="part of the ground name, e.g. wankhede or kotla")
    p = cmd("ground", ground_report, "One ground against the league: par score by season, phases, how wickets fall")
    p.add_argument("name", help="part of the ground name, e.g. wankhede or chidambaram")
    cmd("champions", champions, "Every IPL final: champion, runner-up, result; titles by team", limit=20)
    p = cmd("team", team, "One team season by season: played, won, lost, how far they got", limit=100)
    p.add_argument("code", help="team code, e.g. CSK")
    p = cmd("phase", phase, "Best batters or bowlers in a phase", min_balls=60)
    p.add_argument("phase", choices=["powerplay", "middle", "death"])
    p.add_argument("--role", choices=["bat", "bowl"], default="bat")
    p = cmd("matches", matches, "List matches (find match ids)", limit=100)
    p.add_argument("--team", help="team code, e.g. RCB")
    p.add_argument("--vs", help="opponent code, e.g. CSK")
    p = cmd("match", match, "Full card for one match, rebuilt from the balls", year=False, limit=100)
    p.add_argument("match_id", type=int)
    cmd("records", records, "Highest totals and scores, best figures, fastest fifties, most sixes", limit=10)
    p = cmd("h2h", h2h, "Head to head results between two teams", limit=100)
    p.add_argument("team1")
    p.add_argument("team2")
    p = cmd("commentary", commentary, "Ball-by-ball commentary for a match", year=False, limit=500)
    p.add_argument("match_id", type=int)
    p.add_argument("--search", help="only lines containing this text, e.g. six, dropped, yorker")
    p.add_argument("--inning", type=int)
    p.add_argument("--full", action="store_true", help="do not shorten long commentary lines")
    p = cmd("sql", sql, "Run your own SQL (views: d, bat_inn, bowl_inn; tables: matches, balls, cricsheet_balls, ref_*)",
            year=False, limit=1000)
    p.add_argument("query")
    return ap


def run(argv: list[str], db: str | Path | None = None) -> list[dict[str, Any]]:
    """Run a command (same arguments as the CLI) and return its output blocks. Used by the API."""
    a = build_parser().parse_args(argv)
    if db is not None:
        a.db = str(db)
    q = Q(a)
    try:
        a.fn(q, a)
    finally:
        q.con.close()
    return q.blocks


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")        # Windows consoles: never crash on a stray character
    except AttributeError:
        pass
    a = build_parser().parse_args(argv)
    try:
        q = Q(a)
    except NoDatabase as exc:
        sys.exit(str(exc))
    try:
        a.fn(q, a)
    finally:
        q.con.close()
    render(q.blocks, a.csv)


if __name__ == "__main__":
    main()
