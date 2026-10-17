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

from backend.db import queries, store

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
