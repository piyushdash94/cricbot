"""Stats endpoints over the local DuckDB archive (backend/db).

Every endpoint returns `{"blocks": [...]}`, the same blocks `python -m backend.db.queries`
prints: `{"type": "table", "title", "columns", "rows"}` or `{"type": "text", "text"}`.
The UI renders them generically, so a new stat needs no new frontend code.

Build the database first:  python -m backend.db.cricsheet_load --sync
"""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from backend.db import analytics, queries, store

router = APIRouter(prefix="/api/stats", tags=["stats"])

# Factories, not shared objects: FastAPI must get a separate Query per parameter.
def YEAR():  # noqa: N802
    return Query(None, pattern=r"^\d{4}(-\d{4})?$", description="IPL year (2016) or range (2016-2018); omit for all time")


def TEAM():  # noqa: N802
    return Query(..., pattern=r"^[A-Za-z]{2,6}$", description="Team code, e.g. MI, CSK, PBKS")


def TEAM_FILTER():  # noqa: N802
    return Query(None, pattern=r"^[A-Za-z]{2,6}$", description="Only this team, e.g. CSK")


def db_path() -> str:
    return os.environ.get("CRICBOT_DB") or str(store.DEFAULT_DB)


def _run(*argv: Any) -> dict[str, Any]:
    args = [str(a) for a in argv if a is not None]
    try:
        return {"blocks": queries.run(args, db=db_path())}
    except queries.NoDatabase as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SystemExit as exc:                                   # argparse rejected the arguments
        raise HTTPException(status_code=400, detail=f"invalid arguments: {args}") from exc


def _opt(flag: str, value: Any) -> list[Any]:
    return [flag, value] if value not in (None, "") else []


@router.get("/status")
def status() -> dict[str, Any]:
    """Whether the archive exists, which sources it holds, and which years have ball data."""
    path = db_path()
    if not os.path.exists(path):
        return {"available": False, "hint": "Run: python -m backend.db.cricsheet_load --sync"}
    con = store.connect(path, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        count = lambda t: con.execute(f"SELECT count(DISTINCT match_id) FROM {t}").fetchone()[0] if t in tables else 0  # noqa: E731
        years = [r[0] for r in con.execute(
            "SELECT DISTINCT year(m.start_date) FROM matches m WHERE m.match_id IN "
            + ("(SELECT match_id FROM cricsheet_balls UNION SELECT match_id FROM balls)" if "cricsheet_balls" in tables
               else "(SELECT match_id FROM balls)") + " ORDER BY 1").fetchall()]
        sources = {"cricsheet": count("cricsheet_balls"), "espn": count("balls")}
    finally:
        con.close()
    return {"available": True, "sources": sources, "years": years}


@router.get("/seasons")
def seasons():
    return _run("seasons")


@router.get("/batting")
def batting(year: str | None = YEAR(), limit: int = Query(15, ge=1, le=100), min_balls: int = Query(0, ge=0),
            sort: Literal[tuple(queries.BAT_SORTS)] = "runs", team: str | None = TEAM_FILTER()):  # type: ignore[valid-type]
    return _run("top-batters", *_opt("--year", year), "--limit", limit, "--min-balls", min_balls,
                "--sort", sort, *_opt("--team", team))


@router.get("/bowling")
def bowling(year: str | None = YEAR(), limit: int = Query(15, ge=1, le=100), min_balls: int = Query(0, ge=0),
            sort: Literal[tuple(queries.BOWL_SORTS)] = "wkts", team: str | None = TEAM_FILTER()):  # type: ignore[valid-type]
    return _run("top-bowlers", *_opt("--year", year), "--limit", limit, "--min-balls", min_balls,
                "--sort", sort, *_opt("--team", team))


@router.get("/records")
def records(year: str | None = YEAR(), limit: int = Query(10, ge=1, le=50)):
    return _run("records", *_opt("--year", year), "--limit", limit)


@router.get("/phase")
def phase(phase: Literal["powerplay", "middle", "death"] = "death", role: Literal["bat", "bowl"] = "bat",
          year: str | None = YEAR(), min_balls: int = Query(60, ge=0), limit: int = Query(15, ge=1, le=100)):
    return _run("phase", phase, "--role", role, *_opt("--year", year), "--min-balls", min_balls, "--limit", limit)


@router.get("/players")
def players(q: str = Query(..., min_length=2, max_length=60)):
    return _run("players", q)


@router.get("/players/{player_id}")
def player(player_id: int, year: str | None = YEAR()):
    return _run("player", player_id, *_opt("--year", year))


@router.get("/h2h")
def h2h(team1: str = TEAM(), team2: str = TEAM(), year: str | None = YEAR()):
    return _run("h2h", team1, team2, *_opt("--year", year))


@router.get("/matchup")
def matchup(batter: str = Query(..., min_length=2, max_length=60), bowler: str = Query(..., min_length=2, max_length=60),
            year: str | None = YEAR()):
    return _run("matchup", batter, bowler, *_opt("--year", year))


@router.get("/venues")
def venues(name: str | None = Query(None, max_length=60), year: str | None = YEAR(), limit: int = Query(15, ge=1, le=60)):
    return _run("venues", *([name] if name else []), *_opt("--year", year), "--limit", limit)


@router.get("/grounds/{name}")
def ground(name: str, year: str | None = YEAR()):
    """One ground against the league in the same seasons (scoring by season, phases, how wickets fall)."""
    from backend.agent.stats import GROUND_ALIASES

    if not 2 <= len(name.strip()) <= 60:
        raise HTTPException(422, "ground name must be 2-60 characters")
    return _run("ground", GROUND_ALIASES.get(name.casefold().strip(), name), *_opt("--year", year))


@router.get("/champions")
def champions(year: str | None = YEAR()):
    return _run("champions", *_opt("--year", year))


@router.get("/teams/{code}")
def team(code: str, year: str | None = YEAR()):
    if not code.isalpha() or not 2 <= len(code) <= 6:
        raise HTTPException(status_code=422, detail="team code must be 2-6 letters, e.g. CSK")
    return _run("team", code, *_opt("--year", year))


# ----------------------------------------------------------------- slices and match facts (backend/db/analytics.py)

def _ground(name: str | None) -> str | None:
    from backend.agent.stats import GROUND_ALIASES

    return GROUND_ALIASES.get(name.casefold().strip(), name) if name else None


PLAYER_NAME = {"min_length": 2, "max_length": 60}


@router.get("/slice")
def slice_stats(
    subject: Literal["batter", "bowler", "batting_team", "bowling_team", "league"] = "batter",
    metric: Literal[tuple(analytics.METRICS)] = "runs",  # type: ignore[valid-type]
    player: str | None = Query(None, **PLAYER_NAME), vs: str | None = Query(None, **PLAYER_NAME),
    team: str | None = TEAM_FILTER(), opp: str | None = TEAM_FILTER(),
    overs: str | None = Query(None, pattern=r"^\d{1,2}(-\d{1,2})?$"),
    phase: Literal["powerplay", "middle", "death"] | None = None, innings: int | None = Query(None, ge=1, le=2),
    stage: Literal["final", "playoff"] | None = None, result: Literal["won", "lost"] | None = None,
    toss: Literal["won", "lost"] | None = None, venue: str | None = Query(None, **PLAYER_NAME),
    target_min: int | None = Query(None, ge=1, le=400), target_max: int | None = Query(None, ge=1, le=400),
    rrr_min: float | None = Query(None, ge=0, le=36), rrr_max: float | None = Query(None, ge=0, le=36),
    wickets_in_hand: str | None = Query(None, pattern=r"^\d{1,2}-\d{1,2}$"),
    batter_balls: str | None = Query(None, pattern=r"^\d{1,3}-\d{1,3}$"),
    split: Literal[tuple(analytics.SPLITS)] | None = None,  # type: ignore[valid-type]
    adjust: bool = False, shrink: bool = False, order: Literal["best", "worst"] = "best",
    min_balls: int | None = Query(None, ge=0, le=5000), year: str | None = YEAR(), limit: int = Query(15, ge=1, le=30),
):
    """Any metric on any slice of balls: the same compiler Pandit uses (see backend/db/analytics.py)."""
    return _run("slice", "--subject", subject, "--metric", metric, *_opt("--player", player), *_opt("--vs", vs),
                *_opt("--team", team), *_opt("--opp", opp), *_opt("--overs", overs), *_opt("--phase", phase),
                *_opt("--innings", innings), *_opt("--stage", stage), *_opt("--result", result), *_opt("--toss", toss),
                *_opt("--venue", _ground(venue)), *_opt("--target-min", target_min), *_opt("--target-max", target_max),
                *_opt("--rrr-min", rrr_min), *_opt("--rrr-max", rrr_max), *_opt("--wickets-in-hand", wickets_in_hand),
                *_opt("--batter-balls", batter_balls), *_opt("--split", split), *(["--adjust"] if adjust else []),
                *(["--shrink"] if shrink else []), "--order", order, *_opt("--min-balls", min_balls),
                *_opt("--year", year), "--limit", limit)


@router.get("/partnerships")
def partnerships(by: Literal["single", "pair"] = "single", wicket: int | None = Query(None, ge=1, le=10),
                 team: str | None = TEAM_FILTER(), innings: int | None = Query(None, ge=1, le=2), year: str | None = YEAR(),
                 limit: int = Query(10, ge=1, le=50)):
    return _run("partnerships", "--by", by, *_opt("--wicket", wicket), *_opt("--team", team),
                *_opt("--innings", innings), *_opt("--year", year), "--limit", limit)


@router.get("/awards")
def awards(team: str | None = TEAM_FILTER(), player: str | None = Query(None, **PLAYER_NAME), year: str | None = YEAR(),
           limit: int = Query(10, ge=1, le=50)):
    return _run("awards", *_opt("--team", team), *_opt("--player", player), *_opt("--year", year), "--limit", limit)


@router.get("/hattricks")
def hattricks(team: str | None = TEAM_FILTER(), year: str | None = YEAR()):
    return _run("hattricks", *_opt("--team", team), *_opt("--year", year))


@router.get("/impact")
def impact(team: str | None = TEAM_FILTER(), sort: Literal["times", "runs", "wkts"] = "times", year: str | None = YEAR(),
           limit: int = Query(10, ge=1, le=50)):
    return _run("impact", *_opt("--team", team), "--sort", sort, *_opt("--year", year), "--limit", limit)


@router.get("/toss")
def toss(by: Literal["season", "venue", "team"] | None = None, venue: str | None = Query(None, **PLAYER_NAME),
         year: str | None = YEAR(), limit: int = Query(25, ge=1, le=60)):
    return _run("toss", *_opt("--by", by), *_opt("--venue", _ground(venue)), *_opt("--year", year), "--limit", limit)
