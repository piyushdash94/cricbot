"""Cricsheet ball-by-ball source for historical IPL matches.

Cricsheet (https://cricsheet.org) publishes every delivery of every IPL match as
JSON under the Open Data Commons Attribution License. It has no commentary prose,
but its structured deliveries are complete, so it is the preferred historical
ball source. Data is synced once into a local directory and read offline:

    python -m backend.src.cricsheet sync

The directory defaults to ``backend/data/cricsheet/ipl`` and can be moved with
the ``CRICSHEET_DIR`` environment variable.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import zipfile

IPL_ZIP_URL = "https://cricsheet.org/downloads/ipl_json.zip"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "cricsheet" / "ipl"
INDEX_FILE = "index.json"
INDEX_VERSION = 2

# Cricsheet keeps the name a franchise used at the time; the archive uses the
# current one. Both sides collapse to one key before matching.
TEAM_ALIASES = {
    "royal challengers bangalore": "royal challengers bengaluru",
    "kings xi punjab": "punjab kings",
    "delhi daredevils": "delhi capitals",
    "rising pune supergiants": "rising pune supergiant",
}


def data_dir() -> Path:
    return Path(os.environ.get("CRICSHEET_DIR") or DEFAULT_DIR)


def team_key(name: str) -> str:
    key = re.sub(r"\s+", " ", name.casefold()).strip()
    return TEAM_ALIASES.get(key, key)


def sync(dest: Path | None = None, *, client: Any = None) -> int:
    """Download the IPL archive, extract match files, and rebuild the index."""
    import httpx

    target = dest or data_dir()
    target.mkdir(parents=True, exist_ok=True)
    http = client or httpx.Client(timeout=120, follow_redirects=True)
    response = http.get(IPL_ZIP_URL)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if name.endswith(".json"):
                (target / Path(name).name).write_bytes(archive.read(name))
    entries = build_index(target)
    _index.cache_clear()
    return len(entries)


def build_index(directory: Path) -> list[dict[str, Any]]:
    """Index match files by date and teams so lookups do not parse every file."""
    entries = []
    for path in sorted(directory.glob("*.json")):
        if path.name == INDEX_FILE:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries.append(index_entry(path.stem, payload))
    (directory / INDEX_FILE).write_text(json.dumps({"version": INDEX_VERSION, "matches": entries}), encoding="utf-8")
    return entries


def index_entry(match_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Everything the archive list needs, so listing never opens match files."""
    from backend.src.cricsheet_scorecard import derive_scorecard, result_text, toss_text

    info = payload.get("info", {})
    dates = info.get("dates", [])
    innings = derive_scorecard(payload)["content"]["innings"]
    event = info.get("event", {})
    return {
        "id": str(match_id),
        "file": f"{match_id}.json",
        # IPL seasons sit inside one calendar year; Cricsheet's own season field
        # is "2007/08" for 2008 and "2020/21" for 2020, so the date is safer.
        "season": int(dates[0][:4]) if dates else None,
        "dates": dates,
        "teams": info.get("teams", []),
        "venue": info.get("venue", ""),
        "city": info.get("city", ""),
        "stage": event.get("stage") or (f"Match {event['match_number']}" if event.get("match_number") else ""),
        "result": result_text(info),
        "winner": info.get("outcome", {}).get("winner"),
        "toss": toss_text(info),
        "player_of_match": info.get("player_of_match", []),
        "innings": [{"team": row["team"]["name"], "runs": row["runs"], "wickets": row["wickets"], "overs": row["overs"]} for row in innings],
        "version": payload.get("meta", {}).get("data_version", ""),
    }


@lru_cache(maxsize=2)
def _index(directory: str) -> tuple[dict[str, Any], ...]:
    path = Path(directory)
    if not path.is_dir():
        return ()
    index_path = path / INDEX_FILE
    try:
        stored = json.loads(index_path.read_text(encoding="utf-8"))
        entries = stored["matches"] if stored.get("version") == INDEX_VERSION else None
    except (OSError, ValueError, AttributeError, KeyError):
        entries = None
    if entries is None:
        entries = build_index(path) if any(p for p in path.glob("*.json") if p.name != INDEX_FILE) else []
    return tuple(entries)


def entries() -> tuple[dict[str, Any], ...]:
    return _index(str(data_dir()))


def seasons() -> list[int]:
    return sorted({entry["season"] for entry in entries() if entry.get("season")}, reverse=True)


def by_id(match_id: str) -> dict[str, Any] | None:
    return next((entry for entry in entries() if entry["id"] == str(match_id)), None)


def load(match_id: str) -> dict[str, Any] | None:
    path = data_dir() / f"{match_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_match(match_date: str, team_names: list[str]) -> Path | None:
    """Return the Cricsheet file for a match played by both teams on that date.

    Cricsheet stores the local match date; the archive stores UTC start time, so
    one day either side is accepted to absorb the timezone shift.
    """
    try:
        day = date.fromisoformat(match_date[:10])
    except (TypeError, ValueError):
        return None
    wanted = {team_key(name) for name in team_names if name}
    if len(wanted) != 2:
        return None
    window = {(day + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)}
    directory = data_dir()
    candidates = [
        entry for entry in _index(str(directory))
        if {team_key(team) for team in entry.get("teams", [])} == wanted
        and window.intersection(entry.get("dates", []))
    ]
    # Prefer the exact date when a pair played on consecutive days.
    candidates.sort(key=lambda entry: day.isoformat() not in entry.get("dates", []))
    return directory / candidates[0]["file"] if candidates else None


def _event(delivery: dict[str, Any]) -> tuple[str, bool]:
    runs = delivery.get("runs", {})
    extras = delivery.get("extras", {})
    batter = runs.get("batter", 0)
    if delivery.get("wickets"):
        return "W", False
    if "wides" in extras:
        return f"{extras['wides']}wd", False
    if "noballs" in extras:
        return f"{runs.get('total', 0)}nb", False
    boundary = batter in (4, 6) and not runs.get("non_boundary")
    if boundary:
        return str(batter), True
    if "byes" in extras:
        return f"{extras['byes']}b", False
    if "legbyes" in extras:
        return f"{extras['legbyes']}lb", False
    return str(batter), False


def _describe(delivery: dict[str, Any], event: str, boundary: bool) -> str:
    """Factual one-line description built only from the structured delivery."""
    runs = delivery.get("runs", {})
    parts = []
    for wicket in delivery.get("wickets", []):
        fielders = [fielder.get("name") for fielder in wicket.get("fielders", []) if fielder.get("name")]
        how = wicket.get("kind", "out")
        detail = f"OUT, {wicket.get('player_out', 'batter')} {how}"
        if fielders and how not in {"bowled", "lbw"}:
            detail += f" ({', '.join(fielders)})"
        parts.append(detail)
    if boundary:
        parts.append("SIX" if event == "6" else "FOUR")
    elif not delivery.get("wickets"):
        extras = delivery.get("extras", {})
        kinds = {"wides": "wide", "noballs": "no ball", "byes": "byes", "legbyes": "leg byes", "penalty": "penalty"}
        named = [label for key, label in kinds.items() if key in extras]
        total = runs.get("total", 0)
        tail = "no run" if total == 0 else f"{total} run{'s' if total != 1 else ''}"
        parts.append(f"{', '.join(named)}, {tail}" if named else tail)
    if delivery.get("wickets") and runs.get("total"):
        parts.append(f"{runs['total']} run{'s' if runs['total'] != 1 else ''} completed")
    text = "; ".join(parts) + "."
    return text[:1].upper() + text[1:]


def load_deliveries(path: Path) -> dict[str, Any]:
    """Normalize one Cricsheet match file to the canonical ball contract."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    balls: list[dict[str, Any]] = []
    for inning_number, inning in enumerate(payload.get("innings", []), start=1):
        if inning.get("super_over"):
            continue
        total = wickets = 0
        for over in inning.get("overs", []):
            over_number = int(over.get("over", 0))
            legal = 0
            for sequence, delivery in enumerate(over.get("deliveries", []), start=1):
                extras = delivery.get("extras", {})
                is_legal = "wides" not in extras and "noballs" not in extras
                if is_legal:
                    legal += 1
                # Illegal deliveries carry the number of the ball still to come,
                # matching scorecard convention.
                ball_number = legal if is_legal else legal + 1
                total += delivery.get("runs", {}).get("total", 0)
                wickets += len(delivery.get("wickets", []))
                event, boundary = _event(delivery)
                balls.append({
                    "id": f"cs-{inning_number}-{over_number}-{sequence}",
                    "inning": inning_number,
                    "over": over_number,
                    "ball": ball_number,
                    "label": f"{over_number}.{ball_number}",
                    "event": event,
                    "runs": delivery.get("runs", {}).get("total", 0),
                    "batter_runs": delivery.get("runs", {}).get("batter", 0),
                    "wicket": bool(delivery.get("wickets")),
                    "boundary": boundary,
                    "title": f"{delivery.get('bowler', 'Bowler')} to {delivery.get('batter', 'batter')}",
                    "text": _describe(delivery, event, boundary),
                    "score": f"{total}/{wickets}",
                    "win_probability": None,
                    "batter": delivery.get("batter", ""),
                    "bowler": delivery.get("bowler", ""),
                    "team": inning.get("team", ""),
                })
    info = payload.get("info", {})
    return {
        "balls": balls,
        "match_number": info.get("event", {}).get("match_number") or info.get("event", {}).get("stage"),
        "version": payload.get("meta", {}).get("data_version", ""),
        "file": Path(path).name,
    }


def main(argv: list[str]) -> int:
    if argv[:1] != ["sync"]:
        print("usage: python -m backend.src.cricsheet sync", file=sys.stderr)
        return 2
    try:
        count = sync()
    except Exception as exc:  # network policy, DNS, HTTP errors
        print(f"Could not download {IPL_ZIP_URL}: {exc}", file=sys.stderr)
        return 1
    print(f"Indexed {count} IPL matches in {data_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
