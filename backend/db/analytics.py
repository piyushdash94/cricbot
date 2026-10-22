"""Composable analytics over the ball archive: one slice spec instead of one command per question.

    python -m backend.db.queries slice --subject batter --metric strike_rate --overs 17-20 --innings 2 \\
        --result won --target-min 180 --year 2023-2026 --min-balls 60
    python -m backend.db.queries slice --subject batter --metric average --player "Virat Kohli" --split innings
    python -m backend.db.queries slice --subject bowler --metric economy --overs 16-20 --shrink
    python -m backend.db.queries hattricks | toss --by season | awards | impact | partnerships --wicket 1

A question has five parts: who (subject), what (metric), which balls (filters), how to group (split)
and how to rank (order, minimum sample). The planner fills them in; this module turns them into SQL
deterministically, so the LLM never writes SQL and every filter it asks for is visible in the title.

Filters come in two kinds:
  * context (overs, innings, stage, venue, years, match state, result, toss): which balls count at all;
  * selection (player, team, opposition, opponent player): whose balls we report.
League baselines (season adjustment, shrinkage) use the context only, so "Kohli in won chases" is
compared with everyone in won chases, not with Kohli.

Match state per ball (view `st`): score, wickets and legal balls before the ball, the first-innings
total, runs needed, balls left, required rate (all before the ball) and the batter's nth ball.
Rain-revised (D/L) matches are left out of target, required-rate and won/lost-chase slices: their targets
are not like-for-like.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

STATE_VIEWS = """
CREATE OR REPLACE TEMP VIEW st AS
WITH x AS (
    SELECT d.*,
           sum(runs_total) OVER w - runs_total AS score_before,
           sum(CASE WHEN is_wicket THEN 1 ELSE 0 END) OVER w - CASE WHEN is_wicket THEN 1 ELSE 0 END AS wkts_before,
           sum(CASE WHEN is_legal THEN 1 ELSE 0 END) OVER w - CASE WHEN is_legal THEN 1 ELSE 0 END AS legal_before,
           sum(CASE WHEN NOT is_wide THEN 1 ELSE 0 END) OVER (
               PARTITION BY match_id, inning, batter_id ORDER BY sequence ROWS UNBOUNDED PRECEDING) AS bat_ball_no
    FROM d WINDOW w AS (PARTITION BY match_id, inning ORDER BY sequence ROWS UNBOUNDED PRECEDING)
), t AS (SELECT match_id, sum(runs_total) AS first_total FROM d WHERE inning = 1 GROUP BY match_id)
SELECT x.*, t.first_total,
       CASE WHEN x.inning = 2 THEN t.first_total + 1 - x.score_before END AS needed,
       120 - x.legal_before AS balls_left,
       CASE WHEN x.inning = 2 AND x.legal_before < 120
            THEN 6.0 * (t.first_total + 1 - x.score_before) / (120 - x.legal_before) END AS rrr,
       10 - x.wkts_before AS wkts_in_hand
FROM x LEFT JOIN t USING (match_id);

-- One row per partnership (the stand between two wickets). `pair` names the batters who faced a ball.
CREATE OR REPLACE TEMP VIEW pship AS
SELECT match_id, inning, any_value(year) AS year, any_value(batting_team) AS team,
       any_value(bowling_team) AS opposition, wkts_before + 1 AS wicket, sum(runs_total) AS runs,
       sum(CASE WHEN is_legal THEN 1 ELSE 0 END) AS balls, list_sort(list(DISTINCT batter_id)) AS ids,
       string_agg(DISTINCT batter, ' & ' ORDER BY batter) AS pair, bool_or(is_wicket) AS ended
FROM st GROUP BY match_id, inning, wkts_before;
"""

NO_DL = r"NOT regexp_matches(mm.status_text, 'D/?L')"
FINAL = r"regexp_matches(x.match_title, '^final$', 'i')"
PLAYOFF = ("(x.match_title ILIKE '%final%' OR x.match_title ILIKE '%qualif%' "
           "OR x.match_title ILIKE '%eliminator%')")

# ----------------------------------------------------------------- metrics
# Every metric is num / den * scale (a count has no den). Ball-level columns per side:
#   R runs, B balls, W wickets / dismissals, BND boundaries, DOT dots, F4 fours, S6 sixes.
# A batter's balls exclude wides; a team's and a bowler's balls are legal deliveries. A batter's
# runs are off the bat; a bowler's exclude byes and leg byes; a team's are everything.
SIDES = {
    # A batter's W is replaced by his own dismissals after grouping; here it gives the league baseline.
    "batter": {"R": "runs_bat", "B": "CASE WHEN NOT is_wide THEN 1 ELSE 0 END", "W": "CASE WHEN is_wicket THEN 1 ELSE 0 END",
               "DOT": "CASE WHEN NOT is_wide AND runs_bat = 0 THEN 1 ELSE 0 END"},
    "batting_team": {"R": "runs_total", "B": "CASE WHEN is_legal THEN 1 ELSE 0 END",
                     "W": "CASE WHEN is_wicket THEN 1 ELSE 0 END",
                     "DOT": "CASE WHEN is_legal AND runs_total = 0 THEN 1 ELSE 0 END"},
    "bowler": {"R": "bowler_runs", "B": "CASE WHEN is_legal THEN 1 ELSE 0 END",
               "W": "CASE WHEN bowler_wicket THEN 1 ELSE 0 END",
               "DOT": "CASE WHEN is_legal AND bowler_runs = 0 THEN 1 ELSE 0 END"},
    "bowling_team": {"R": "runs_total", "B": "CASE WHEN is_legal THEN 1 ELSE 0 END",
                     "W": "CASE WHEN is_wicket THEN 1 ELSE 0 END",
                     "DOT": "CASE WHEN is_legal AND runs_total = 0 THEN 1 ELSE 0 END"},
}
SIDES["league"] = SIDES["batting_team"]
BAT_SIDE = {"batter", "batting_team", "league"}

# name -> (num, den, scale, higher_is_better, label); den "B" means per ball, so shrinkage applies.
METRICS = {
    "runs": ("R", None, 1, True, "runs"),
    "wickets": ("W", None, 1, True, "wickets"),
    "sixes": ("S6", None, 1, True, "sixes"),
    "fours": ("F4", None, 1, True, "fours"),
    "balls": ("B", None, 1, True, "balls"),
    "strike_rate": ("R", "B", 100, True, "strike rate"),
    "run_rate": ("R", "B", 6, True, "run rate"),
    "economy": ("R", "B", 6, False, "economy"),
    "average": ("R", "W", 1, True, "average"),
    "bowling_average": ("R", "W", 1, False, "bowling average"),
    "bowling_strike_rate": ("B", "W", 1, False, "balls per wicket"),
    "boundary_pct": ("BND", "B", 100, True, "boundary %"),
    "dot_pct": ("DOT", "B", 100, True, "dot-ball %"),
    "six_pct": ("S6", "B", 100, True, "sixes per 100 balls"),
    "balls_per_boundary": ("B", "BND", 1, False, "balls per boundary"),
    "dots": ("DOT", None, 1, True, "dot balls"),
    "boundaries": ("BND", None, 1, True, "boundaries"),
    # Innings-level: the best single innings in the slice ("highest score in finals", "highest total chasing").
    "high_score": ("R", None, 1, True, "highest score"),
    # Innings-level counts: innings that were a duck (0 and out), 50-99, or 100+.
    "ducks": ("DUCK", None, 1, True, "ducks"),
    "fifties": ("FIFTY", None, 1, True, "fifties"),
    "hundreds": ("HUNDRED", None, 1, True, "hundreds"),
}
INNINGS_COUNTS = {"ducks": "runs = 0 AND is_out", "fifties": "runs BETWEEN 50 AND 99", "hundreds": "runs >= 100"}
# Which metrics make sense for which side (the planner may ask for "economy" of a batter: refuse).
SIDE_METRICS = {
    "batter": {"runs", "sixes", "fours", "balls", "strike_rate", "average", "boundary_pct", "dot_pct", "six_pct",
               "balls_per_boundary", "wickets", "high_score", "dots", "boundaries", "ducks", "fifties", "hundreds"},
    "batting_team": {"runs", "sixes", "fours", "balls", "run_rate", "strike_rate", "average", "boundary_pct",
                     "dot_pct", "six_pct", "wickets", "high_score", "dots", "boundaries"},
    "bowler": {"wickets", "runs", "sixes", "fours", "balls", "economy", "bowling_average", "bowling_strike_rate",
               "dot_pct", "boundary_pct", "average", "strike_rate", "dots", "boundaries"},
    "bowling_team": {"wickets", "runs", "sixes", "fours", "balls", "economy", "bowling_average",
                     "bowling_strike_rate", "dot_pct", "boundary_pct", "run_rate", "dots", "boundaries"},
}
SIDE_METRICS["league"] = SIDE_METRICS["batting_team"] | {"economy"}
# Plain words a planner may use for the bowler's version of a batting metric.
BOWLER_ALIASES = {"average": "bowling_average", "strike_rate": "bowling_strike_rate"}

SPLITS = {
    "innings": "CASE WHEN x.inning = 1 THEN 'batting first' ELSE 'chasing' END",
    "phase": "x.phase",
    "season": "CAST(x.year AS VARCHAR)",
    "venue": "ground(x.venue)",
    "opposition": None,                                   # filled per side
    "result": None,
    "toss": None,
    "bowl_kind": "coalesce(nullif(bs.bowl_kind, ''), 'unknown')",
    "bat_hand": "coalesce(nullif(hs.bat_hand, ''), 'unknown')",
}
SPLIT_LABELS = {"bowl_kind": "bowling type", "bat_hand": "batting hand"}
# The ranking unit: a career (default), a season ("most ducks in a season") or a match ("which match had the most runs").
UNITS = {"career": "''", "season": "CAST(x.year AS VARCHAR)", "match": "CAST(x.match_id AS VARCHAR)"}

# Home ground of a team in a season: the ground it played most at (4+ matches, 25%+ of all its games, about half its home games). Seasons played
# at neutral venues have none (2009 South Africa, 2020 UAE, 2021 bubble, 2022 Mumbai/Pune). A match is home for a
# team at its home ground, away at the opponent's, and neutral anywhere else (left out of both).
HOME_VIEW = """
CREATE OR REPLACE TEMP VIEW home_ground AS
WITH t AS (SELECT year(start_date) AS y, franchise(team1) AS team, ground(venue) AS g FROM matches
           UNION ALL SELECT year(start_date), franchise(team2), ground(venue) FROM matches),
     c AS (SELECT y, team, g, count(*) AS n, sum(count(*)) OVER (PARTITION BY y, team) AS tot,
                  row_number() OVER (PARTITION BY y, team ORDER BY count(*) DESC, g) AS rk
           FROM t GROUP BY y, team, g)
SELECT y, team, g FROM c WHERE rk = 1 AND n >= 4 AND n >= 0.25 * tot AND y NOT IN (2009, 2020, 2021, 2022);
"""
HOME_NOTE = ("Home = the ground a team played most at that season (4+ matches); 2009, 2020, 2021 and 2022 were at "
             "neutral venues and are left out, as are matches at a team's second ground.")


def _side_cols(subject: str) -> tuple[str, str, str]:
    """(the subject's team column, the other team column, the key column) for one side."""
    if subject in ("batter", "batting_team", "league"):
        team, other = "x.batting_team", "x.bowling_team"
    else:
        team, other = "x.bowling_team", "x.batting_team"
    key = {"batter": "x.batter_id", "bowler": "x.bowler_id", "batting_team": "x.batting_team",
           "bowling_team": "x.bowling_team", "league": "'IPL'"}[subject]
    return team, other, key


def _range(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    parts = [int(p) for p in str(text).replace(" ", "").split("-") if p]
    return (parts[0], parts[-1]) if parts else None


def describe(a: argparse.Namespace, names: dict[str, str]) -> list[str]:
    """Every filter in words: the title shows exactly what was computed."""
    bits = []
    if (o := _range(a.overs)):
        bits.append(f"over {o[0]}" if o[0] == o[1] else f"overs {o[0]}-{o[1]}")
    if a.phase:
        bits.append({"powerplay": "powerplay (overs 1-6)", "middle": "middle overs (7-15)",
                     "death": "death overs (16-20)"}[a.phase])
    if a.innings:
        side = a.subject in BAT_SIDE
        bits.append({1: "batting first" if side else "bowling first", 2: "chasing" if side else "bowling second"}[a.innings])
    if a.stage:
        bits.append({"final": "in finals", "playoff": "in playoffs"}[a.stage])
    if a.result:
        bits.append({"won": "in matches won", "lost": "in matches lost"}[a.result])
    if a.toss:
        bits.append({"won": "after winning the toss", "lost": "after losing the toss"}[a.toss])
    if a.venue:
        bits.append(f"at {names.get('venue', a.venue)}")
    if a.team:
        bits.append(f"for {a.team}")
    if a.opp:
        bits.append(f"against {a.opp}")
    if names.get("vs"):
        bits.append(f"against {names['vs']}")
    if a.target_min is not None or a.target_max is not None:
        lo, hi = a.target_min, a.target_max
        bits.append(f"target {lo}-{hi}" if lo is not None and hi is not None else
                    f"target {lo} or more" if lo is not None else f"target {hi} or less")
    if a.rrr_min is not None or a.rrr_max is not None:
        bits.append(f"required rate {'above ' + str(a.rrr_min) if a.rrr_min is not None else ''}"
                    f"{' and ' if a.rrr_min is not None and a.rrr_max is not None else ''}"
                    f"{'below ' + str(a.rrr_max) if a.rrr_max is not None else ''}")
    if a.wickets_in_hand:
        lo, hi = _range(a.wickets_in_hand)
        bits.append(f"{lo}-{hi} wickets in hand" if lo != hi else f"{lo} wickets in hand")
    if (bb := _range(a.batter_balls)):
        bits.append(f"batter's balls {bb[0]}-{bb[1]} of his innings")
    if a.bowl_kind:
        bits.append({"spin": "spinners", "pace": "pace bowlers"}[a.bowl_kind] if a.subject in ("bowler", "bowling_team")
                    else f"against {a.bowl_kind}")
    if a.bat_hand:
        bits.append(f"{a.bat_hand}-handed batters" if a.subject == "batter" else f"to {a.bat_hand}-handed batters")
    if a.venue_type:
        bits.append({"home": "at home", "away": "away from home"}[a.venue_type])
    if a.per and a.per != "career":
        bits.append({"season": "best single seasons", "match": "single matches"}[a.per])
    return bits


def slice_stats(q: Any, a: argparse.Namespace) -> None:
    from .queries import _resolve_player                  # lazy: queries imports this module
    subject = a.subject
    metric = BOWLER_ALIASES.get(a.metric, a.metric) if subject in ("bowler", "bowling_team") else a.metric
    if metric not in SIDE_METRICS[subject]:
        q.note(f"'{a.metric}' is not a {subject.replace('_', ' ')} metric. Try one of: {', '.join(sorted(SIDE_METRICS[subject]))}.")
        return
    num, den, scale, higher, label = METRICS[metric]
    side = SIDES[subject]
    team_col, other_col, key = _side_cols(subject)
    names: dict[str, str] = {}

    # ---- which balls count (context) and whose (selection)
    state = any(v is not None for v in (a.target_min, a.target_max, a.rrr_min, a.rrr_max)) \
        or bool(a.wickets_in_hand or a.batter_balls)
    base = "st" if state else "d"
    ctx = ["x.year BETWEEN $y0 AND $y1"]
    params: dict[str, Any] = {}
    if (o := _range(a.overs)):
        ctx.append(f"x.over_number BETWEEN {o[0]} AND {o[1]}")
    if a.phase:
        ctx.append(f"x.phase = '{a.phase}'")
    if a.innings:
        ctx.append(f"x.inning = {int(a.innings)}")
    if a.stage:
        ctx.append(FINAL if a.stage == "final" else PLAYOFF)
    if a.result:
        ctx.append(f"franchise(mm.winner) {'=' if a.result == 'won' else '<>'} {team_col}"
                   + (" AND mm.winner <> ''" if a.result == "lost" else ""))
    joins_info = bool(a.toss) or a.split == "toss"
    if a.toss and not q.filled("cs_match_info"):
        q.note("Toss data is not loaded in this archive yet: re-run `python -m backend.db.cricsheet_load`.")
        return
    if a.toss:
        ctx.append(f"ci.toss_winner IS NOT NULL AND franchise(ci.toss_winner) {'=' if a.toss == 'won' else '<>'} {team_col}")
    if a.venue:
        found = q.rows("""SELECT ground(venue) AS g, count(*) AS n FROM matches
                          WHERE ground(venue) ILIKE '%' || $v || '%' OR venue ILIKE '%' || $v || '%'
                          GROUP BY 1 ORDER BY n DESC LIMIT 1""", v=a.venue)
        if not found:
            q.note(f"No IPL ground matching '{a.venue}'.")
            return
        names["venue"] = found[0]["g"]
        params["venue_g"] = found[0]["g"]
        ctx.append("ground(x.venue) = $venue_g")
    # A revised (D/L) target is not like-for-like: leave those matches out of target, required-rate and won/lost-chase slices.
    chase = (a.innings == 2 and bool(a.result)) or any(v is not None for v in (a.target_min, a.target_max, a.rrr_min, a.rrr_max))
    if chase:
        ctx.append(NO_DL)
    if a.target_min is not None:                          # target = first-innings total + 1
        ctx.append(f"x.inning = 2 AND x.first_total + 1 >= {int(a.target_min)}")
    if a.target_max is not None:
        ctx.append(f"x.inning = 2 AND x.first_total + 1 <= {int(a.target_max)}")
    if a.rrr_min is not None:
        ctx.append(f"x.rrr > {float(a.rrr_min)}")
    if a.rrr_max is not None:
        ctx.append(f"x.rrr < {float(a.rrr_max)}")
    if a.wickets_in_hand:
        lo, hi = _range(a.wickets_in_hand)
        ctx.append(f"x.wkts_in_hand BETWEEN {lo} AND {hi}")
    if a.batter_balls:
        lo, hi = _range(a.batter_balls)
        ctx.append(f"x.bat_ball_no BETWEEN {lo} AND {hi}")

    styles = bool(a.bowl_kind or a.bat_hand) or a.split in ("bowl_kind", "bat_hand")
    if styles and not q.filled("player_styles"):
        q.note("Batting hand and bowling type are not loaded in this archive yet: run `python -m backend.db.player_styles --fetch 200`.")
        return
    if a.bowl_kind:
        ctx.append(f"bs.bowl_kind = '{a.bowl_kind}'")
    if a.bat_hand:
        ctx.append(f"hs.bat_hand = '{a.bat_hand}'")
    if a.venue_type:
        h = "hg" if a.venue_type == "home" else "ag"
        ctx.append(f"ground(x.venue) = {h}.g")

    sel, sel_out = [], []                                 # sel_out: the same, for a batter's dismissals
    if a.player:
        ids = []
        for name in a.player:
            found = _resolve_player(q, name)
            if not found:
                return
            ids.append(found[0])
            names.setdefault("players", "")
            names["players"] += ("" if not names["players"] else ", ") + found[1]
        idlist = ", ".join(str(i) for i in ids)
        col = {"batter": "x.batter_id", "bowler": "x.bowler_id"}.get(subject)
        if not col:
            q.note("--player needs subject batter or bowler.")
            return
        sel.append(f"{col} IN ({idlist})")
        sel_out.append(f"x.player_out_id IN ({idlist})")
    if a.vs:
        found = _resolve_player(q, a.vs)
        if not found:
            return
        names["vs"] = found[1]
        col = "x.bowler_id" if subject in BAT_SIDE else "x.batter_id"
        sel.append(f"{col} = {found[0]}")
        sel_out.append(f"{col} = {found[0]}")
    if a.team:
        sel.append(f"{team_col} = '{q.team(a.team)}'")
        sel_out.append(f"{team_col} = '{q.team(a.team)}'")
    if a.opp:
        sel.append(f"{other_col} = '{q.team(a.opp)}'")
        sel_out.append(f"{other_col} = '{q.team(a.opp)}'")

    split = a.split
    split_sql = {"opposition": other_col,
                 "result": f"CASE WHEN franchise(mm.winner) = {team_col} THEN 'won' "
                           f"WHEN mm.winner = '' THEN 'no result' ELSE 'lost' END",
                 "toss": f"CASE WHEN franchise(ci.toss_winner) = {team_col} THEN 'won toss' ELSE 'lost toss' END"
                 }.get(split) or SPLITS.get(split or "", None) or "''"
    name_sql = {"batter": "x.batter", "bowler": "x.bowler"}.get(subject, key)

    cols = {"R": side["R"], "B": side["B"], "W": side["W"] or "0", "DOT": side["DOT"],
            "BND": "CASE WHEN is_four OR is_six THEN 1 ELSE 0 END",
            "F4": "CASE WHEN is_four THEN 1 ELSE 0 END", "S6": "CASE WHEN is_six THEN 1 ELSE 0 END"}
    select_cols = ", ".join(f"({v}) AS {k}" for k, v in cols.items())
    from_sql = (f"FROM {base} x JOIN matches mm USING (match_id)"
                + (" LEFT JOIN cs_match_info ci USING (match_id)" if joins_info else "")
                + (" LEFT JOIN player_styles bs ON bs.player_id = x.bowler_id"
                   " LEFT JOIN player_styles hs ON hs.player_id = x.batter_id" if styles else "")
                + (f" JOIN home_ground {'hg' if a.venue_type == 'home' else 'ag'} ON "
                   f"{'hg' if a.venue_type == 'home' else 'ag'}.y = x.year AND "
                   f"{'hg' if a.venue_type == 'home' else 'ag'}.team = {team_col if a.venue_type == 'home' else other_col}"
                   if a.venue_type else ""))
    sel_sql = " AND ".join(sel) or "TRUE"
    sel_out_sql = " AND ".join(sel_out) or "TRUE"
    unit = a.per or "career"
    filtered = f"""f AS (SELECT x.match_id, x.inning, x.year, x.is_wicket, x.player_out_id, x.player_out,
                              {key} AS k, {name_sql} AS name, {split_sql} AS s, {UNITS[unit]} AS u, {select_cols},
                              ({sel_sql}) AS sel, ({sel_out_sql}) AS sel_out
                       {from_sql} WHERE {' AND '.join(ctx)})"""
    if metric == "high_score":
        _innings_list(q, a, filtered, params, subject, describe(a, names), names)
        return
    if metric in INNINGS_COUNTS:
        _innings_counts(q, a, filtered, params, metric, describe(a, names), names)
        return
    rows = q.rows(f"""WITH {filtered}
        SELECT k, s, u, any_value(name) AS name, sum(R) AS R, sum(B) AS B, sum(W) AS W, sum(DOT) AS DOT,
               sum(BND) AS BND, sum(F4) AS F4, sum(S6) AS S6, count(DISTINCT (match_id, inning)) AS inns
        FROM f WHERE sel GROUP BY k, s, u""", **params)
    if subject == "batter":                               # dismissals belong to the player out (non-striker run-outs too)
        outs = {(r["k"], r["s"], r["u"]): r["n"] for r in q.rows(f"""WITH {filtered}
            SELECT player_out_id AS k, s, u, count(*) AS n FROM f WHERE is_wicket AND sel_out GROUP BY 1, 2, 3""", **params)}
        for r in rows:
            r["W"] = outs.get((r["k"], r["s"], r["u"]), 0)

    # ---- baselines from the context only: per season (adjust) and per ball variance (shrink)
    base_rows = q.rows(f"""WITH {filtered} SELECT year, sum({num}) AS num, sum({den or num}) AS den,
                                                  var_pop({num}) FILTER (WHERE {den or num} = 1) AS v
                           FROM f GROUP BY year""", **params) if den else []
    league = {r["year"]: (r["num"] or 0) / r["den"] for r in base_rows if r["den"]}

    def value(r: dict[str, Any]) -> float | None:
        n = r[num] or 0
        if den is None:
            return n
        return n / r[den] * scale if r[den] else None

    out = [dict(r, value=value(r)) for r in rows]
    # One named player: show his numbers whatever the sample. Era indexes need a career-sized sample.
    min_sample = a.min_balls if a.min_balls is not None else (
        0 if den is None or subject not in ("batter", "bowler") or a.player else 500 if a.adjust else 60)
    sample_col = "B"
    notes = []
    if den and a.adjust and len(league) > 0:
        # Expected numerator if this player had the league rate of each season he played in.
        per = q.rows(f"""WITH {filtered} SELECT k, s, u, year, sum({den}) AS den FROM f WHERE sel GROUP BY 1, 2, 3, 4""", **params)
        exp: dict[tuple, float] = {}
        for r in per:
            exp[(r["k"], r["s"], r["u"])] = exp.get((r["k"], r["s"], r["u"]), 0) + (r["den"] or 0) * league.get(r["year"], 0)
        for r in out:
            e = exp.get((r["k"], r["s"], r["u"]))
            r["index"] = round(100 * (r[num] or 0) / e) if e else None
        notes.append(f"Season-adjusted index: 100 = the league's {label} on the same kind of balls in the same "
                     f"season{'s' if len(league) > 1 else ''}; above 100 is better than the league"
                     + ("" if higher else " (for this metric, below 100 is better)") + ".")
    if den == "B" and a.shrink and base_rows:
        # Empirical Bayes: pull each rate toward the league rate by k pseudo-balls, k = sampling / true variance.
        tot_num = sum(r["num"] or 0 for r in base_rows)
        tot_den = sum(r["den"] or 0 for r in base_rows)
        p0 = tot_num / tot_den if tot_den else 0
        s2 = sum((r["v"] or 0) * (r["den"] or 0) for r in base_rows) / tot_den if tot_den else 0
        cand = [r for r in out if r["B"]]
        sw = sum(r["B"] for r in cand)
        if cand and sw:
            spread = sum(r["B"] * ((r[num] or 0) / r["B"] - p0) ** 2 for r in cand)
            tau2 = (spread - len(cand) * s2) / sw
            k = s2 / tau2 if tau2 > 0 else float("inf")
            for r in out:
                r["shrunk"] = round(((r[num] or 0) + (k * p0 if k != float("inf") else 0)) /
                                    ((r["B"] or 0) + k) * scale, 2) if k != float("inf") else round(p0 * scale, 2)
            notes.append(f"Shrunk: each {label} is pulled toward the league's {round(p0 * scale, 2)} by "
                         + (f"{round(k)} balls' worth" if k != float("inf") else "all of it (no real spread)")
                         + " (empirical Bayes), so a short hot streak cannot top the list; ranked by the shrunk value.")
    rank_key = "index" if a.adjust and den else ("shrunk" if any("shrunk" in r for r in out) else "value")
    # An average over one or two dismissals is noise (183 from a single out): a ranking needs 3.
    min_outs = 1 if (a.player or subject not in ("batter", "bowler")) else 3
    out = [r for r in out if r["value"] is not None and (r[sample_col] or 0) >= (min_sample or 0)
           and (den != "W" or (r["W"] or 0) >= min_outs)]
    better_high = higher if a.order == "best" else not higher
    out.sort(key=lambda r: (r.get(rank_key) is None, -(r.get(rank_key) or 0) if better_high else (r.get(rank_key) or 0)))
    if split and (subject == "league" or names.get("players")):
        out.sort(key=lambda r: (str(r["name"]), str(r["s"])))     # a trend or one player's split: in split order

    # ---- table
    who = {"batter": "batter", "bowler": "bowler", "batting_team": "team", "bowling_team": "team", "league": "IPL"}[subject]
    bits = describe(a, names)
    sample_txt = (f" (min {min_sample} balls" + (f", {min_outs} dismissals" if den == "W" and min_outs > 1 else "") + ")") \
        if min_sample and den else (f" (min {min_outs} dismissals)" if den == "W" and min_outs > 1 else "")
    focus = f"{names['players']}: " if names.get("players") else ""
    noun = {"batter": "Batters", "bowler": "Bowlers", "batting_team": "Teams batting", "bowling_team": "Teams bowling",
            "league": "All IPL"}[subject]
    title = (f"{focus}{label}" if focus else f"IPL {label}" if subject == "league" else f"{noun} by {label}") \
        + (f" by {SPLIT_LABELS.get(split, split)}" if split else "") + (" - " + ", ".join(bits) if bits else "") + f" {q.span}{sample_txt}"
    fmt = lambda v: round(v, 2) if isinstance(v, float) else v      # noqa: E731
    shown = out[: a.limit]
    matches = {}
    if unit == "match" and shown:
        matches = {str(m["match_id"]): m for m in q.rows(f"""SELECT match_id, start_date AS date, title,
            franchise(team1) || ' v ' || franchise(team2) AS teams, status_text AS result FROM matches
            WHERE match_id IN ({', '.join(str(int(r['u'])) for r in shown)})""")}
    records = []
    for r in shown:
        rec: dict[str, Any] = {who: r["name"]} if not (unit == "match" and subject == "league") else {}
        if unit == "season":
            rec["season"] = r["u"]
        if unit == "match":
            m = matches.get(str(r["u"]), {})
            rec.update(match=f"{m.get('teams', '')}, {m.get('title', '')}".strip(", "), date=q._plain(m.get("date")))
        if split:
            rec[split] = r["s"]
        rec[label] = fmt(r["value"])
        if "index" in r:
            rec[f"{label} index"] = r["index"]
        if "shrunk" in r:
            rec[f"{label} (shrunk)"] = r["shrunk"]
        if subject in BAT_SIDE:
            rec.update(runs=r["R"], balls=r["B"], **({"outs": r["W"]} if subject == "batter" else {"wickets": r["W"]}))
        else:
            rec.update(balls=r["B"], runs=r["R"], wickets=r["W"])
        if metric == "six_pct":
            rec["sixes"] = r["S6"]
        if metric in ("boundary_pct", "balls_per_boundary"):
            rec["4s+6s"] = r["BND"]
        records.append(rec)
    if not records:
        q.note(f"{title}: no rows" + (" with enough balls - try a smaller --min-balls." if min_sample else "."))
        return
    q.show(title, records)
    for n in notes:
        q.note(n)
    if chase:
        q.note("Rain-revised (D/L) matches are left out: their targets and overs are not like-for-like.")
    if a.venue_type:
        q.note(HOME_NOTE)
    if styles:
        by_bowler = bool(a.bowl_kind) or a.split == "bowl_kind"
        pct = q.rows(f"""SELECT round(100.0 * count(s.player_id) / nullif(count(*), 0), 1) AS pct FROM d x
                         LEFT JOIN player_styles s ON s.player_id = x.{'bowler_id' if by_bowler else 'batter_id'}
                              AND s.{'bowl_kind' if by_bowler else 'bat_hand'} <> ''
                         WHERE x.year BETWEEN $y0 AND $y1""")[0]["pct"]
        q.note(f"{'Bowling type' if by_bowler else 'Batting hand'} (from ESPN player profiles) is known for {pct}% of these "
               "seasons' balls; the rest are left out.")


def _innings_list(q: Any, a: argparse.Namespace, filtered: str, params: dict[str, Any], subject: str,
                  bits: list[str], names: dict[str, str]) -> None:
    """The highest single innings in the slice: a batter's score (with not out) or a team's total."""
    batter = subject == "batter"
    rows = q.rows(f"""WITH {filtered},
        inn AS (SELECT k, match_id, inning, any_value(name) AS name, sum(R) AS runs, sum(B) AS balls,
                       sum(F4) AS fours, sum(S6) AS sixes, sum(W) AS wkts
                FROM f WHERE sel GROUP BY k, match_id, inning),
        outs AS (SELECT DISTINCT match_id, inning, player_out_id FROM f WHERE is_wicket)
        SELECT inn.name, inn.runs, inn.balls, inn.fours, inn.sixes, inn.wkts,
               {"o.player_out_id IS NULL" if batter else "FALSE"} AS not_out,
               {"any_value(x2.batting_team)" if batter else "inn.k"} AS team,
               m.start_date AS date, m.title AS match
        FROM inn JOIN matches m USING (match_id)
        {"LEFT JOIN outs o ON o.match_id = inn.match_id AND o.inning = inn.inning AND o.player_out_id = inn.k" if batter else ""}
        {"LEFT JOIN d x2 ON x2.match_id = inn.match_id AND x2.inning = inn.inning AND x2.batter_id = inn.k" if batter else ""}
        GROUP BY ALL ORDER BY inn.runs DESC, inn.balls LIMIT $limit""", **params)
    title = f"{'Highest individual scores' if batter else 'Highest team totals'}" \
        + (f" by {names['players']}" if names.get("players") else "") + (" - " + ", ".join(bits) if bits else "") + f" {q.span}"
    if not rows:
        q.note(f"{title}: no innings.")
        return
    q.show(title, [{"player" if batter else "team": r["name"] if batter else r["team"],
                    "score": f"{r['runs']}{'*' if r['not_out'] else ''}" if batter else f"{r['runs']}/{r['wkts']}",
                    "balls": r["balls"], **({"team": r["team"]} if batter else {}), "date": r["date"], "match": r["match"]}
                   for r in rows])
    if batter:
        q.note("* = not out.")


def _innings_counts(q: Any, a: argparse.Namespace, filtered: str, params: dict[str, Any], metric: str,
                    bits: list[str], names: dict[str, str]) -> None:
    """Ducks, fifties, hundreds: count a batter's innings in the slice (per career or per season)."""
    unit = a.per or "career"
    rows = q.rows(f"""WITH {filtered},
        inn AS (SELECT k, u, match_id, inning, any_value(name) AS name, sum(R) AS runs FROM f WHERE sel GROUP BY k, u, match_id, inning),
        outs AS (SELECT DISTINCT match_id, inning, player_out_id FROM f WHERE is_wicket),
        x AS (SELECT inn.*, o.player_out_id IS NOT NULL AS is_out FROM inn
              LEFT JOIN outs o ON o.match_id = inn.match_id AND o.inning = inn.inning AND o.player_out_id = inn.k)
        SELECT any_value(name) AS name, u, count(*) FILTER (WHERE {INNINGS_COUNTS[metric]}) AS n, count(*) AS innings,
               sum(runs) AS runs
        FROM x GROUP BY k, u HAVING n > 0 ORDER BY n DESC, innings, name LIMIT $limit""", **params)
    label = METRICS[metric][4]
    title = (f"{names['players']}: {label}" if names.get("players") else f"Batters by {label}") \
        + (" - " + ", ".join(bits) if bits else "") + f" {q.span}"
    if not rows:
        q.note(f"{title}: none.")
        return
    q.show(title, [{"batter": r["name"], **({"season": r["u"]} if unit == "season" else {}), label: r["n"],
                    "innings": r["innings"], "runs": r["runs"]} for r in rows])
    if metric == "ducks":
        q.note("A duck is an innings of 0 that ended in a dismissal.")


def home_record(q: Any, a: argparse.Namespace) -> None:
    """Each team's record at home and away (see HOME_NOTE for what counts as home)."""
    order = "home_win_pct ASC" if a.order == "worst" else "home_win_pct DESC"
    q.table(f"Home and away records {q.span} (matches with a result; rain-revised kept)", f"""
        WITH m AS (SELECT match_id, year(start_date) AS y, franchise(team1) AS t1, franchise(team2) AS t2,
                          franchise(winner) AS w, ground(venue) AS g FROM matches
                   WHERE year(start_date) BETWEEN $y0 AND $y1 AND winner <> ''),
             side AS (SELECT m.*, t1 AS team, t2 AS opp FROM m UNION ALL SELECT m.*, t2, t1 FROM m),
             tagged AS (SELECT s.team, s.w,
                               CASE WHEN h.g = s.g THEN 'home' WHEN o.g = s.g THEN 'away' ELSE 'neutral' END AS place
                        FROM side s LEFT JOIN home_ground h ON h.y = s.y AND h.team = s.team
                        LEFT JOIN home_ground o ON o.y = s.y AND o.team = s.opp)
        SELECT team, count(*) FILTER (WHERE place = 'home') AS home_played,
               count(*) FILTER (WHERE place = 'home' AND w = team) AS home_won,
               round(100.0 * count(*) FILTER (WHERE place = 'home' AND w = team) / nullif(count(*) FILTER (WHERE place = 'home'), 0), 1) AS home_win_pct,
               count(*) FILTER (WHERE place = 'away') AS away_played,
               round(100.0 * count(*) FILTER (WHERE place = 'away' AND w = team) / nullif(count(*) FILTER (WHERE place = 'away'), 0), 1) AS away_win_pct
        FROM tagged GROUP BY team HAVING home_played >= {a.min_matches} ORDER BY {order} LIMIT $limit""")
    q.note(HOME_NOTE)


# ----------------------------------------------------------------- families

def hattricks(q: Any, a: argparse.Namespace) -> None:
    """Three wickets credited to the bowler on his consecutive deliveries in one match (a wide or no-ball
    between them counts as a delivery and breaks the run; run-outs are not the bowler's)."""
    team = f"AND x.bowling_team = '{q.team(a.team)}'" if a.team else ""
    q.table(f"Hat-tricks {q.span}" + (f" for {q.team(a.team)}" if a.team else ""), f"""
        WITH b AS (SELECT x.match_id, x.bowler_id, x.bowler, x.bowling_team AS team, x.batting_team AS opposition,
                          x.start_date, x.bowler_wicket AS w,
                          lag(x.bowler_wicket, 1) OVER p AS w1, lag(x.bowler_wicket, 2) OVER p AS w2
                   FROM d x WHERE x.year BETWEEN $y0 AND $y1 {team}
                   WINDOW p AS (PARTITION BY x.match_id, x.bowler_id ORDER BY x.inning, x.sequence))
        SELECT any_value(bowler) AS bowler, any_value(team) AS team, any_value(opposition) AS against,
               min(start_date) AS date
        FROM b WHERE w AND w1 AND w2 GROUP BY match_id, bowler_id ORDER BY date DESC LIMIT $limit""")
    n = q.rows(f"""WITH b AS (SELECT x.match_id, x.bowler_id, x.bowler_wicket AS w,
                          lag(x.bowler_wicket, 1) OVER p AS w1, lag(x.bowler_wicket, 2) OVER p AS w2
                   FROM d x WHERE x.year BETWEEN $y0 AND $y1 {team}
                   WINDOW p AS (PARTITION BY x.match_id, x.bowler_id ORDER BY x.inning, x.sequence))
        SELECT any_value(x2.bowler) AS bowler, count(DISTINCT b.match_id) AS hattricks
        FROM b JOIN (SELECT DISTINCT bowler_id, bowler FROM d) x2 USING (bowler_id)
        WHERE w AND w1 AND w2 GROUP BY b.bowler_id ORDER BY hattricks DESC, bowler""")
    total = sum(r["hattricks"] for r in n)
    multi = [f"{r['bowler']} {r['hattricks']}" for r in n if r["hattricks"] > 1]
    q.note(f"{total} hat-tricks {q.span}" + (f" for {q.team(a.team)}" if a.team else "")
           + (f"; more than one: {', '.join(multi)}." if multi else ".")
           + " (Four in four counts once.)")


def toss(q: Any, a: argparse.Namespace) -> None:
    if not q.filled("cs_match_info"):
        q.note("Toss data is not loaded in this archive yet: re-run `python -m backend.db.cricsheet_load`.")
        return
    group = {"season": "CAST(year(m.start_date) AS VARCHAR)", "venue": "ground(m.venue)",
             "team": "franchise(ci.toss_winner)", None: "'all'"}[a.by]
    q.params["venue"] = a.venue or ""
    where = "AND (ground(m.venue) ILIKE '%' || $venue || '%' OR m.venue ILIKE '%' || $venue || '%')" if a.venue else ""
    q.table(f"Toss {('by ' + a.by) if a.by else ''} {q.span}{' at ' + a.venue if a.venue else ''} "
            "(matches with a result; rain-revised left out)", f"""
        SELECT {group} AS {a.by or 'scope'}, count(*) AS matches,
               round(100.0 * count(*) FILTER (WHERE ci.toss_decision = 'field') / count(*), 1) AS chose_field_pct,
               round(100.0 * count(*) FILTER (WHERE franchise(ci.toss_winner) = franchise(m.winner)) / count(*), 1)
                   AS toss_winner_won_pct,
               round(100.0 * count(*) FILTER (WHERE ci.toss_decision = 'field' AND franchise(ci.toss_winner) = franchise(m.winner))
                     / nullif(count(*) FILTER (WHERE ci.toss_decision = 'field'), 0), 1) AS won_pct_when_fielding,
               round(100.0 * count(*) FILTER (WHERE ci.toss_decision = 'bat' AND franchise(ci.toss_winner) = franchise(m.winner))
                     / nullif(count(*) FILTER (WHERE ci.toss_decision = 'bat'), 0), 1) AS won_pct_when_batting
        FROM matches m JOIN cs_match_info ci USING (match_id)
        WHERE year(m.start_date) BETWEEN $y0 AND $y1 AND m.winner <> '' AND NOT regexp_matches(m.status_text, 'D/?L') {where}
        GROUP BY 1 HAVING count(*) >= {a.min_matches if a.by else 1} ORDER BY {'1' if a.by == 'season' else 'matches DESC'} LIMIT $limit""")


def awards(q: Any, a: argparse.Namespace) -> None:
    if not q.filled("cs_awards"):
        q.note("Player-of-the-match data is not loaded yet: re-run `python -m backend.db.cricsheet_load`.")
        return
    team = ""
    if a.team:
        q.params["tm"] = q.team(a.team)
        team = """AND (SELECT any_value(franchise(team)) FROM (
                      SELECT batting_team AS team FROM d WHERE d.match_id = aw.match_id AND d.batter_id = aw.player_id
                      UNION ALL SELECT bowling_team FROM d WHERE d.match_id = aw.match_id AND d.bowler_id = aw.player_id)) = $tm"""
    if a.player:
        from .queries import _resolve_player
        found = _resolve_player(q, a.player)
        if not found:
            return
        q.params["aw_pid"] = found[0]
        team += " AND aw.player_id = $aw_pid"
    q.table(f"Most player-of-the-match awards {q.span}" + (f" for {q.team(a.team)}" if a.team else ""), f"""
        SELECT coalesce(any_value(p.name), any_value(aw.player)) AS player, count(*) AS awards,
               max(m.start_date) AS latest
        FROM cs_awards aw JOIN matches m USING (match_id) LEFT JOIN pnames p ON p.player_id = aw.player_id
        WHERE year(m.start_date) BETWEEN $y0 AND $y1 {team}
        GROUP BY aw.player_id ORDER BY awards DESC, latest DESC LIMIT $limit""")


def impact(q: Any, a: argparse.Namespace) -> None:
    if not q.filled("cs_subs"):
        q.note("Substitution data is not loaded yet: re-run `python -m backend.db.cricsheet_load`.")
        return
    team = f"AND franchise(s.team) = '{q.team(a.team)}'" if a.team else ""
    q.table(f"Impact-player substitutes {q.span}" + (f" for {q.team(a.team)}" if a.team else "")
            + " - what they did in the matches they came on in", f"""
        WITH s AS (SELECT s.* FROM cs_subs s JOIN matches m USING (match_id)
                   WHERE s.reason = 'impact_player' AND year(m.start_date) BETWEEN $y0 AND $y1 {team}),
        bat AS (SELECT s.player_in_id, sum(x.runs_bat) AS runs, count(*) FILTER (WHERE NOT x.is_wide) AS balls
                FROM s JOIN d x ON x.match_id = s.match_id AND x.batter_id = s.player_in_id GROUP BY 1),
        bowl AS (SELECT s.player_in_id, count(*) FILTER (WHERE x.bowler_wicket) AS wkts,
                        count(*) FILTER (WHERE x.is_legal) AS balls, sum(x.bowler_runs) AS conceded
                 FROM s JOIN d x ON x.match_id = s.match_id AND x.bowler_id = s.player_in_id GROUP BY 1)
        SELECT coalesce(any_value(p.name), any_value(s.player_in)) AS player, count(*) AS times_subbed_in,
               coalesce(any_value(bat.runs), 0) AS runs, coalesce(any_value(bat.balls), 0) AS balls_faced,
               coalesce(any_value(bowl.wkts), 0) AS wkts,
               round(6.0 * any_value(bowl.conceded) / nullif(any_value(bowl.balls), 0), 2) AS economy
        FROM s LEFT JOIN bat USING (player_in_id) LEFT JOIN bowl USING (player_in_id)
        LEFT JOIN pnames p ON p.player_id = s.player_in_id
        GROUP BY s.player_in_id ORDER BY {'runs' if a.sort == 'runs' else 'wkts' if a.sort == 'wkts' else 'times_subbed_in'} DESC
        LIMIT $limit""")
    n = q.rows(f"SELECT count(*) AS n, count(DISTINCT match_id) AS m FROM cs_subs s JOIN matches mm USING (match_id) "
               f"WHERE s.reason = 'impact_player' AND year(mm.start_date) BETWEEN $y0 AND $y1 {team}")[0]
    q.note(f"{n['n']} impact-player substitutions in {n['m']} matches {q.span} (the rule began in 2023).")


def partnerships(q: Any, a: argparse.Namespace) -> None:
    where = ["year BETWEEN $y0 AND $y1"]
    if a.wicket:
        where.append(f"wicket = {int(a.wicket)}")
    if a.team:
        where.append(f"team = '{q.team(a.team)}'")
    if a.innings:
        where.append(f"inning = {int(a.innings)}")
    label = (f" for the {a.wicket}{'st' if a.wicket == 1 else 'nd' if a.wicket == 2 else 'rd' if a.wicket == 3 else 'th'} wicket"
             if a.wicket else "") + (f" for {q.team(a.team)}" if a.team else "") \
        + ({1: ", batting first", 2: ", chasing"}.get(a.innings, "") if a.innings else "")
    if a.by == "pair":
        q.table(f"Most partnership runs by a pair{label} {q.span} (min {a.min_stands} stands)", f"""
            SELECT any_value(pair) AS pair, count(*) AS stands, sum(runs) AS runs,
                   round(sum(runs) / count(*), 1) AS avg_stand, max(runs) AS best,
                   count(*) FILTER (WHERE runs >= 100) AS hundred_stands,
                   round(100.0 * sum(runs) / nullif(sum(balls), 0), 1) AS run_rate_per_100
            FROM pship WHERE {' AND '.join(where)} AND len(ids) = 2
            GROUP BY ids HAVING count(*) >= {a.min_stands} ORDER BY runs DESC LIMIT $limit""")
    else:
        q.table(f"Highest partnerships{label} {q.span}", f"""
            SELECT p.pair, p.runs, p.balls, p.wicket, p.team, p.opposition, m.start_date AS date
            FROM pship p JOIN matches m USING (match_id) WHERE {' AND '.join(where)}
            ORDER BY p.runs DESC, date LIMIT $limit""")


# ----------------------------------------------------------------- pitch profile

# Phrases (not single words: "turned it to fine leg" is a stroke, not turn off the pitch) that describe the surface.
PITCH_CUES = [
    ("turn", r"(?<!bat )\b(?:turn(?:ed|ing|s)? (?:away|back|sharply|square|a long way)(?! for\b| to (?:deep|fine|square|short|long|mid|the))|turn(?:ed|s)? in sharply|big turn|sharp turn"
             r"|plenty of turn|some turn|spun (?:away|in|back|sharply)|(?:grip(?:ped|ping|s)?) (?:and|off|on|the surface)"
             r"|ripp(?:ed|ing) (?:it|past|through)|dusty)\b"),
    ("seam or swing", r"\b(?:seam(?:ed|ing)? (?:away|in|back)|off the seam|nip(?:ped|s|ping)? (?:back|away|in)"
                      r"|swing(?:s|ing)? (?:away|in|back)|(?<!been )swung (?:in|away|back)(?! over\b| to\b| for\b)|late movement|lateral movement|some movement)\b"),
    ("bounce", r"\b(?:steep bounce|extra bounce|kick(?:ed|s)? up|short and climbing|climbs? (?:on|at|towards) (?:him|the)"
               r"|bounced (?:more|steeply)|spongy bounce)\b"),
    ("low bounce", r"\b(?:kept low|stayed low|keeping low|staying low|skidded|skid(?:s|ding)? on|uneven bounce|variable bounce)\b"),
    ("slow, two-paced", r"\b(?:slow (?:pitch|surface|wicket|track|deck)|two[- ]paced|stopp(?:ed|ing) on|held up|holding up"
                        r"|sticky|didn't come on|not coming on)\b"),
    ("flat, good to bat", r"\b(?:flat (?:pitch|track|deck|surface)|belter of a (?:pitch|track|surface|deck)|batting paradise"
                          r"|good (?:pitch|surface|track) to bat|true (?:pitch|surface|bounce))\b"),
    ("dew", r"\b(?:dew|wet ball)\b"),
]


def pitch(q: Any, a: argparse.Namespace) -> None:
    """How one match's pitch played, against the league in the same season: pace v spin, phase scoring, how
    wickets fell, and what the commentary said. Outcomes, not a pitch reading: they mix the surface with the
    bowlers, the batters and the match situation."""
    q.params["mid"] = a.match_id
    info = q.rows("""SELECT m.title, m.start_date, ground(m.venue) AS ground, franchise(m.team1) AS t1, franchise(m.team2) AS t2,
                            m.status_text, year(m.start_date) AS y FROM matches m WHERE m.match_id = $mid""")
    if not info:
        q.note(f"No match {a.match_id} in the archive.")
        return
    m = info[0]
    q.params["py"] = m["y"]
    head = f"{m['t1']} v {m['t2']}, {m['title']}, {m['ground']}, {q._plain(m['start_date'])}"
    phase = q.rows("""
        WITH x AS (SELECT phase, match_id = $mid AS here, sum(runs_total) AS r, count(*) FILTER (WHERE is_legal) AS b
                   FROM d WHERE year = $py GROUP BY 1, 2)
        SELECT phase, round(6.0 * sum(r) FILTER (WHERE here) / nullif(sum(b) FILTER (WHERE here), 0), 2) AS rr_here,
               round(6.0 * sum(r) / nullif(sum(b), 0), 2) AS rr_league
        FROM x GROUP BY phase ORDER BY CASE phase WHEN 'powerplay' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END""")
    q.show(f"Pitch profile: {head} - run rate by phase against IPL {m['y']}", phase)
    kinds: list[dict[str, Any]] = []
    if q.filled("player_styles"):
        kinds = q.rows("""
            WITH x AS (SELECT s.bowl_kind AS kind, match_id = $mid AS here, sum(bowler_runs) AS r,
                              count(*) FILTER (WHERE is_legal) AS b, count(*) FILTER (WHERE bowler_wicket) AS w,
                              count(*) FILTER (WHERE is_legal AND bowler_runs = 0) AS dots
                       FROM d JOIN player_styles s ON s.player_id = d.bowler_id AND s.bowl_kind <> ''
                       WHERE year = $py GROUP BY 1, 2)
            SELECT kind, sum(b) FILTER (WHERE here) AS balls, sum(w) FILTER (WHERE here) AS wickets,
                   round(6.0 * sum(r) FILTER (WHERE here) / nullif(sum(b) FILTER (WHERE here), 0), 2) AS econ_here,
                   round(6.0 * sum(r) / nullif(sum(b), 0), 2) AS econ_league,
                   round(100.0 * sum(dots) FILTER (WHERE here) / nullif(sum(b) FILTER (WHERE here), 0), 1) AS dot_pct_here,
                   round(100.0 * sum(dots) / nullif(sum(b), 0), 1) AS dot_pct_league
            FROM x GROUP BY kind ORDER BY kind""")
        q.show(f"Pace and spin against IPL {m['y']}", kinds)
    outs = q.rows("""
        SELECT round(100.0 * count(*) FILTER (WHERE match_id = $mid AND dismissal_type IN ('bowled', 'lbw'))
                     / nullif(count(*) FILTER (WHERE match_id = $mid), 0), 1) AS bowled_lbw_pct_here,
               round(100.0 * count(*) FILTER (WHERE dismissal_type IN ('bowled', 'lbw')) / nullif(count(*), 0), 1)
                   AS bowled_lbw_pct_league,
               count(*) FILTER (WHERE match_id = $mid) AS wickets_here
        FROM d WHERE year = $py AND bowler_wicket""")
    q.show("How wickets fell: bowled or lbw (a sign of low or skiddy bounce)", outs)
    texts = [r["c"] for r in q.rows("SELECT commentary AS c FROM balls WHERE match_id = $mid AND commentary <> ''")] \
        if q.has("balls") else []
    cues = []
    for label, pattern in PITCH_CUES:
        hits = [t for t in texts if re.search(pattern, t, re.I)]
        if hits:
            example = re.sub(r"<[^>]{0,40}>", "", hits[0])
            cues.append({"cue": label, "mentions": len(hits), "example": (example[:140] + "…") if len(example) > 140 else example})
    if cues:
        q.show(f"What the commentary said ({len(texts)} balls with commentary)", sorted(cues, key=lambda c: -c["mentions"]))
    q.note(_pitch_verdict(phase, kinds, outs[0] if outs else {}, cues, len(texts)))


def _pitch_verdict(phase: list[dict[str, Any]], kinds: list[dict[str, Any]], outs: dict[str, Any],
                   cues: list[dict[str, Any]], commentary: int) -> str:
    """One sentence from the numbers: each part says what it compares, so the verdict can be checked."""
    parts = []
    here = [p for p in phase if p["rr_here"] is not None]
    if here:
        diff = sum(p["rr_here"] - p["rr_league"] for p in here) / len(here)
        parts.append("high-scoring" if diff > 1 else "low-scoring" if diff < -1 else "about par for scoring")
    by = {k["kind"]: k for k in kinds if k["econ_here"] is not None and k["balls"] and k["balls"] >= 24}
    if "spin" in by and "pace" in by:
        edge = (by["spin"]["econ_here"] - by["spin"]["econ_league"]) - (by["pace"]["econ_here"] - by["pace"]["econ_league"])
        parts.append("spin did better than usual relative to pace" if edge < -1 else
                     "pace did better than usual relative to spin" if edge > 1 else "pace and spin fared about as usual")
    if outs.get("wickets_here") and outs.get("bowled_lbw_pct_here") is not None and outs["wickets_here"] >= 6 \
            and outs["bowled_lbw_pct_here"] - (outs.get("bowled_lbw_pct_league") or 0) > 15:
        parts.append("an unusual share of bowled and lbw dismissals")
    top = [c for c in cues if c["mentions"] >= 2][:2]
    if top:
        parts.append("commentary often mentioned " + " and ".join(c["cue"] for c in top))
    elif commentary == 0:
        parts.append("no ball-by-ball commentary to add")
    return ("Verdict: " + "; ".join(parts) + ". Each part compares this match with the league in the same season; "
            "it describes outcomes, which mix the surface with the bowlers, batters and match situation.") if parts else \
        "Not enough ball data to profile this pitch."


def par(q: Any, a: argparse.Namespace) -> None:
    """The league's par for each over number in a season: average runs and wickets per innings (the baseline for the
    over-by-over chart; death overs always score more, so comparing an over with the innings' own average misleads)."""
    q.table(f"Par by over {q.span}", """
        SELECT over_number AS over, round(sum(runs_total) * 1.0 / count(DISTINCT (match_id, inning)), 2) AS runs,
               round(count(*) FILTER (WHERE is_wicket) * 1.0 / count(DISTINCT (match_id, inning)), 3) AS wickets
        FROM d WHERE year BETWEEN $y0 AND $y1 GROUP BY 1 ORDER BY 1""")


def register(cmd: Any) -> None:
    """Add the commands to queries.build_parser."""
    p = cmd("slice", slice_stats, "Any metric over any slice of balls (see backend/db/analytics.py)", limit=10)
    p.add_argument("--subject", choices=["batter", "bowler", "batting_team", "bowling_team", "league"], default="batter")
    p.add_argument("--metric", choices=sorted(METRICS), default="runs")
    p.add_argument("--player", action="append", help="limit to this player (repeat for several)")
    p.add_argument("--vs", help="only balls against this player (the bowler for batters, the batter for bowlers)")
    p.add_argument("--team", help="the subject's team")
    p.add_argument("--opp", help="the opposition")
    p.add_argument("--overs", help="over range, e.g. 17-20 or 1")
    p.add_argument("--phase", choices=["powerplay", "middle", "death"])
    p.add_argument("--innings", type=int, choices=[1, 2])
    p.add_argument("--stage", choices=["final", "playoff"])
    p.add_argument("--result", choices=["won", "lost"], help="matches the subject's side won or lost")
    p.add_argument("--toss", choices=["won", "lost"], help="matches where the subject's side won or lost the toss")
    p.add_argument("--venue", help="ground or part of its name")
    p.add_argument("--target-min", type=int, help="chases of a target of at least this (first-innings total + 1)")
    p.add_argument("--target-max", type=int)
    p.add_argument("--rrr-min", type=float, help="balls bowled when the required rate was above this")
    p.add_argument("--rrr-max", type=float)
    p.add_argument("--wickets-in-hand", help="e.g. 0-3 (before the ball)")
    p.add_argument("--batter-balls", help="the batter's nth balls of his innings, e.g. 1-10")
    p.add_argument("--split", choices=sorted(SPLITS), help="one row per value: compare chasing vs batting first, ...")
    p.add_argument("--per", choices=sorted(UNITS), help="rank careers (default), single seasons or single matches")
    p.add_argument("--bowl-kind", choices=["pace", "spin"], help="the bowler's type (spinners; or a batter against spin)")
    p.add_argument("--bat-hand", choices=["left", "right"], help="the batter's hand (left-handers; or a bowler to them)")
    p.add_argument("--venue-type", choices=["home", "away"], help="the subject's side at home or away (see HOME_NOTE)")
    p.add_argument("--adjust", action="store_true", help="season-adjusted index (100 = league that season)")
    p.add_argument("--shrink", action="store_true", help="empirical-Bayes shrinkage toward the league rate")
    p.add_argument("--order", choices=["best", "worst"], default="best")
    p.add_argument("--min-balls", type=int, help="minimum sample (balls; dismissals for averages)")
    cmd("par", par, "League par for each over number (runs and wickets per innings)", limit=20)
    p = cmd("pitch", pitch, "How one match's pitch played against the league that season", year=False)
    p.add_argument("match_id", type=int)
    p = cmd("home", home_record, "Each team's home and away record", limit=20)
    p.add_argument("--order", choices=["best", "worst"], default="best")
    p.add_argument("--min-matches", type=int, default=10)
    p = cmd("hattricks", hattricks, "Every hat-trick (three wickets on consecutive deliveries)", limit=40)
    p.add_argument("--team")
    p = cmd("toss", toss, "Toss decisions and whether the toss winner won", limit=25)
    p.add_argument("--by", choices=["season", "venue", "team"])
    p.add_argument("--venue")
    p.add_argument("--min-matches", type=int, default=5)
    p = cmd("awards", awards, "Most player-of-the-match awards", limit=10)
    p.add_argument("--team")
    p.add_argument("--player")
    p = cmd("impact", impact, "Impact-player substitutes and what they did", limit=10)
    p.add_argument("--team")
    p.add_argument("--sort", choices=["times", "runs", "wkts"], default="times")
    p = cmd("partnerships", partnerships, "Highest partnerships, or most runs by a pair", limit=10)
    p.add_argument("--wicket", type=int)
    p.add_argument("--team")
    p.add_argument("--innings", type=int, choices=[1, 2])
    p.add_argument("--by", choices=["single", "pair"], default="single")
    p.add_argument("--min-stands", type=int, default=10)
