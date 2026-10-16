"""Load Cricsheet's IPL deliveries into the DuckDB archive.

    python -m backend.src.cricsheet sync          # download Cricsheet (once; ~4 s)
    python -m backend.db.cricsheet_load           # load it into data/cricbot.duckdb (~20 s)
    python -m backend.db.cricsheet_load --sync    # both in one step

Cricsheet (https://cricsheet.org, Open Data Commons Attribution License) has every
delivery of every IPL match but no commentary text. It fills `cricsheet_balls`; the
ESPN ingest (`backend.db.ingest`) stays optional and adds commentary text, the official
Cricinfo scorecard and very recent matches.

Player IDs: Cricsheet's register maps its own player ids to Cricinfo ids, the ids ESPN
uses, so the same player has the same `player_id` in both sources.

Safe to re-run: the Cricsheet tables are rebuilt in one transaction.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import store

log = logging.getLogger("cricbot.cricsheet_load")

REGISTER_URL = "https://cricsheet.org/register/people.csv"
REGISTER_FILE = store.DEFAULT_DB.parent / "cricsheet_people.csv"

BOWLER_WICKETS = {"bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket"}
NOT_OUT_KINDS = {"retired hurt", "retired not out"}           # recorded, but not a wicket

CRICSHEET_COLUMNS = (
    "match_id", "inning", "sequence", "over_number", "ball_in_over", "over_label",
    "batting_team", "batter_id", "batter", "non_striker", "bowler_id", "bowler",
    "runs_total", "score_value", "runs_bat", "extras", "wide_runs", "noball_runs", "bye_runs", "legbye_runs",
    "penalty_runs", "is_legal", "is_wide", "is_noball", "is_four", "is_six",
    "is_wicket", "bowler_wicket", "dismissal_type", "player_out_id", "player_out",
    "short_text", "commentary",
)


# ------------------------------------------------------------------- inputs

def cricsheet_dir() -> Path:
    from backend.src import cricsheet
    return cricsheet.data_dir()


def match_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.json") if p.stem.isdigit())


def load_register(path: Path = REGISTER_FILE, download: bool = True) -> dict[str, int]:
    """Cricsheet player id -> Cricinfo player id (empty if the register is unavailable)."""
    if not path.exists() and download:
        try:
            import httpx
            path.parent.mkdir(parents=True, exist_ok=True)
            response = httpx.get(REGISTER_URL, timeout=60, follow_redirects=True)
            response.raise_for_status()
            path.write_bytes(response.content)
        except Exception as exc:                              # register is optional
            log.warning("player register unavailable (%s); using Cricsheet ids", exc)
            return {}
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {row["identifier"]: int(row["key_cricinfo"]) for row in csv.DictReader(fh)
                if (row.get("key_cricinfo") or "").isdigit()}


def player_id(cricsheet_id: str | None, register: dict[str, int]) -> int | None:
    """Cricinfo id when known (shared with ESPN); otherwise a stable negative id."""
    if not cricsheet_id:
        return None
    return register.get(cricsheet_id) or -int(cricsheet_id, 16)


# ------------------------------------------------------------------ parsing

def team_code(name: str) -> str:
    from backend.src.ipl_teams import team_code as code
    return code(name)


def _describe(batter: str, bowler: str, runs: dict[str, Any], extras: dict[str, Any], wicket: bool,
              four: bool, six: bool) -> str:
    if wicket:
        what = "OUT"
    elif six:
        what = "SIX"
    elif four:
        what = "FOUR"
    elif extras:
        labels = {"wides": "wide", "noballs": "no ball", "byes": "bye", "legbyes": "leg bye", "penalty": "penalty"}
        what = ", ".join(f"{n} {labels[k]}{'s' if n > 1 and k != 'noballs' else ''}" for k, n in extras.items())
        if runs.get("batter"):
            what = f"{what}, {runs['batter']} run{'s' if runs['batter'] > 1 else ''}"
    elif runs.get("batter"):
        what = f"{runs['batter']} run{'s' if runs['batter'] > 1 else ''}"
    else:
        what = "no run"
    return f"{bowler} to {batter}, {what}"


def match_balls(match_id: int, payload: dict[str, Any], register: dict[str, int]) -> list[dict[str, Any]]:
    people = (payload.get("info", {}).get("registry") or {}).get("people") or {}
    pid = lambda name: player_id(people.get(name), register)    # noqa: E731
    rows = []
    for inning, inn in enumerate(payload.get("innings") or [], 1):
        team = team_code(inn.get("team", ""))
        for over in inn.get("overs") or []:
            over_number = int(over["over"]) + 1                 # Cricsheet counts overs from 0
            legal = 0
            for position, d in enumerate(over.get("deliveries") or [], 1):
                runs = d.get("runs") or {}
                extras = d.get("extras") or {}
                wide, noball = int(extras.get("wides", 0)), int(extras.get("noballs", 0))
                byes, legbyes = int(extras.get("byes", 0)), int(extras.get("legbyes", 0))
                is_legal = not (wide or noball)
                legal += is_legal
                bat = int(runs.get("batter", 0))
                if noball > 1 and not bat and not (byes or legbyes):
                    # Older Cricsheet files score "no ball + 4 byes" as 5 no-ball runs (pre-2017 Laws);
                    # the official Cricinfo card charges the bowler 1 and records the rest as byes.
                    byes, noball = noball - 1, 1
                four = bat == 4 and not runs.get("non_boundary")
                six = bat == 6 and not runs.get("non_boundary")
                wickets = d.get("wickets") or []
                first = wickets[0] if wickets else {}
                kind = (first.get("kind") or "").lower()
                is_wicket = bool(first) and kind not in NOT_OUT_KINDS
                batter, bowler = d.get("batter", ""), d.get("bowler", "")
                rows.append({
                    "match_id": match_id, "inning": inning,
                    "sequence": inning * 100000 + over_number * 100 + position,
                    "over_number": over_number, "ball_in_over": position,
                    "over_label": str(d.get("actual_delivery") or f"{over_number - 1}.{legal if is_legal else legal + 1}"),
                    "batting_team": team,
                    "batter_id": pid(batter), "batter": batter, "non_striker": d.get("non_striker", ""),
                    "bowler_id": pid(bowler), "bowler": bowler,
                    "runs_total": int(runs.get("total", 0)), "score_value": int(runs.get("total", 0)),
                    "runs_bat": bat, "extras": int(runs.get("extras", 0)),
                    "wide_runs": wide, "noball_runs": noball,
                    "bye_runs": byes, "legbye_runs": legbyes,
                    "penalty_runs": int(extras.get("penalty", 0)),
                    "is_legal": is_legal, "is_wide": bool(wide), "is_noball": bool(noball),
                    "is_four": four, "is_six": six,
                    "is_wicket": is_wicket, "bowler_wicket": is_wicket and kind in BOWLER_WICKETS,
                    "dismissal_type": kind,
                    "player_out_id": pid(first.get("player_out")) if first else None,
                    "player_out": first.get("player_out", "") if first else "",
                    "short_text": _describe(batter, bowler, runs, extras, is_wicket, four, six),
                    "commentary": "",
                })
    return rows


def match_row(match_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    info = payload.get("info") or {}
    teams = [team_code(t) for t in info.get("teams") or []] + ["", ""]
    outcome = info.get("outcome") or {}
    winner = team_code(outcome["winner"]) if outcome.get("winner") else ""
    by = outcome.get("by") or {}
    if winner and by:
        unit, n = next(iter(by.items()))
        status = f"{winner} won by {n} {unit if n != 1 else unit.rstrip('s')}"
        if outcome.get("method"):
            status += f" ({outcome['method']})"
    elif winner:
        status = f"{winner} won" + (" (Super Over)" if outcome.get("eliminator") else "")
    elif outcome.get("result") == "tie":
        status = "Match tied" + (f" ({team_code(outcome['eliminator'])} won the Super Over)" if outcome.get("eliminator") else "")
        winner = team_code(outcome["eliminator"]) if outcome.get("eliminator") else ""
    else:
        status = (outcome.get("result") or "no result").capitalize()
    event = info.get("event") or {}
    title = event.get("stage") or (f"Match {event['match_number']}" if event.get("match_number") else "")
    venue, city = info.get("venue") or "", info.get("city") or ""
    return {
        "match_id": match_id, "season": str(info.get("season", "")), "series_path": "", "match_path": "",
        "title": title, "start_date": (info.get("dates") or [None])[0],
        "venue": f"{venue}, {city}" if city and city not in venue else venue,
        "team1": teams[0], "team2": teams[1], "winner": winner, "status_text": status,
        "format": "T20", "stage": "FINISHED", "is_cancelled": False,
    }


# ------------------------------------------------------------------ loading

def _bulk_insert(con, table: str, columns: Iterable[str], rows: list[dict[str, Any]]) -> None:
    """Insert many rows fast: write NDJSON, then one INSERT ... SELECT (executemany is slow)."""
    if not rows:
        return
    columns = tuple(columns)
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({c: r.get(c) for c in columns}) + "\n")
        tmp = fh.name
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM read_json_auto(?, format='newline_delimited')", [tmp])
    finally:
        os.unlink(tmp)


def load(db_path: str | Path | None = None, directory: Path | None = None, download_register: bool = True) -> dict[str, int]:
    directory = directory or cricsheet_dir()
    files = match_files(directory)
    if not files:
        raise SystemExit(f"No Cricsheet files in {directory}. Run: python -m backend.src.cricsheet sync")
    register = load_register(download=download_register)
    balls, matches, names = [], [], {}
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        match_id = int(path.stem)
        matches.append(match_row(match_id, payload))
        rows = match_balls(match_id, payload, register)
        balls.extend(rows)
        for r in rows:
            for pid_key, name_key in (("batter_id", "batter"), ("bowler_id", "bowler"), ("player_out_id", "player_out")):
                if r[pid_key] is not None:
                    names.setdefault(r[pid_key], r[name_key])

    con = store.connect(db_path)
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM cricsheet_balls")
        _bulk_insert(con, "cricsheet_balls", CRICSHEET_COLUMNS, balls)
        # Match rows only fill gaps: a row from the ESPN fixture list (with its cricdata paths) wins.
        new = [m for m in matches if m["match_id"] not in
               {r[0] for r in con.execute("SELECT match_id FROM matches").fetchall()}]
        _bulk_insert(con, "matches", store.MATCH_COLUMNS, new)
        con.execute("DELETE FROM players WHERE source = 'cricsheet'")
        _bulk_insert(con, "players", ("player_id", "name", "source"),
                     [{"player_id": k, "name": v, "source": "cricsheet"} for k, v in names.items()])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    counts = {"matches": len(files), "deliveries": len(balls), "new_match_rows": len(new),
              "players": len(names), "players_with_cricinfo_id": sum(1 for k in names if k > 0)}
    con.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help=f"DuckDB file (default {store.DEFAULT_DB})")
    ap.add_argument("--sync", action="store_true", help="download the latest Cricsheet archive first")
    ap.add_argument("--no-register", action="store_true", help="do not download the player register")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.sync:
        from backend.src import cricsheet
        log.info("synced %s Cricsheet matches", cricsheet.sync())
    print(load(args.db, download_register=not args.no_register))


if __name__ == "__main__":
    main()
