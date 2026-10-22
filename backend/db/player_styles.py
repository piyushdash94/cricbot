"""Batting hand and bowling type for every IPL player, from ESPN scorecards.

    python -m backend.db.player_styles                 # read the scorecards already cached on disk
    python -m backend.db.player_styles --fetch 200     # also fetch scorecards for players still missing

Cricsheet has no playing styles; every ESPN scorecard lists each player's `longBattingStyles`
("left-hand bat") and `longBowlingStyles` ("right-arm offbreak"). Scorecards are fetched through
the same cached, throttled path as the match pages (backend/src/ipl_data.py), and only for the
matches that cover the most players still missing (a greedy set cover: about 200 matches cover
every player in the archive). Results land in `player_styles`; re-running only adds.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from . import store

COLUMNS = ("player_id", "name", "bat_hand", "bowl_style", "bowl_kind", "bowl_arm", "source")
PACE_CODES = {"rf", "rfm", "rmf", "rm", "lf", "lfm", "lmf", "lm", "rsm", "lsm"}
SPIN_CODES = {"ob", "lb", "lbg", "sla", "slc", "lws", "rob", "lbs"}
SPIN_WORDS = ("offbreak", "legbreak", "googly", "orthodox", "wrist-spin", "wrist spin", "chinaman", "slow left-arm")


def classify(player: dict[str, Any]) -> dict[str, str]:
    """{bat_hand, bowl_style, bowl_kind, bowl_arm} from one ESPN player object."""
    bat = " ".join(player.get("longBattingStyles") or player.get("battingStyles") or []).lower()
    hand = "left" if "left" in bat or "lhb" in bat else "right" if "right" in bat or "rhb" in bat else ""
    styles = [s.lower() for s in player.get("longBowlingStyles") or []]
    codes = [c.lower() for c in player.get("bowlingStyles") or []]
    style = styles[0] if styles else ""
    code = codes[0] if codes else ""
    if style:
        kind = "spin" if any(w in style for w in SPIN_WORDS) else "pace" if any(w in style for w in ("fast", "medium")) else ""
        arm = "left" if "left-arm" in style else "right" if "right-arm" in style or kind == "spin" else ""
    else:
        kind = "spin" if code in SPIN_CODES else "pace" if code in PACE_CODES else ""
        arm = "left" if code.startswith(("l", "sla", "slc")) and code not in ("lb", "lbg", "lbs") else "right" if code else ""
    return {"bat_hand": hand, "bowl_style": style or code, "bowl_kind": kind, "bowl_arm": arm}


def from_scorecard(payload: Any) -> dict[int, dict[str, str]]:
    """Every player object in a scorecard, keyed by Cricinfo id (the archive's player_id)."""
    found: dict[int, dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "battingStyles" in node and str(node.get("objectId") or "").isdigit():
                found[int(node["objectId"])] = {"name": str(node.get("longName") or node.get("name") or ""), **classify(node)}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def cached(directory: Path) -> dict[int, dict[str, str]]:
    styles: dict[int, dict[str, str]] = {}
    for path in sorted((directory / "match_scorecard").glob("*.json")):
        try:
            styles.update(from_scorecard(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return styles


def cover(con: Any, known: set[int], limit: int) -> list[tuple[int, str, str, int]]:
    """Matches to fetch, most players still missing first: (match_id, series_path, match_path, new players)."""
    rows = con.execute("""
        SELECT d.match_id, any_value(m.series_path), any_value(m.match_path), list(DISTINCT d.p)
        FROM (SELECT match_id, batter_id AS p FROM deliveries UNION ALL SELECT match_id, bowler_id FROM deliveries) d
        JOIN matches m USING (match_id)
        WHERE d.p > 0 AND m.match_path <> '' GROUP BY d.match_id""").fetchall()
    sets = {m: (sp, mp, set(ps) - known) for m, sp, mp, ps in rows}
    picks: list[tuple[int, str, str, int]] = []
    covered: set[int] = set()
    while len(picks) < limit:
        best = max(sets, key=lambda m: len(sets[m][2] - covered), default=None)
        if best is None or not sets[best][2] - covered:
            break
        sp, mp, players = sets.pop(best)
        picks.append((best, sp, mp, len(players - covered)))
        covered |= players
    return picks


def save(db: str | Path | None, styles: dict[int, dict[str, str]], source: str) -> None:
    con = store.connect(db)
    try:
        con.execute("BEGIN")
        if styles:
            con.execute(f"DELETE FROM player_styles WHERE player_id IN ({', '.join(str(p) for p in styles)})")
        store.bulk_insert(con, "player_styles", COLUMNS,
                          [{"player_id": pid, **row, "source": source} for pid, row in styles.items()])
        con.execute("COMMIT")
    finally:
        con.close()


def coverage(db: str | Path | None) -> dict[str, Any]:
    con = store.connect(db, read_only=True)
    try:
        return dict(zip(("players_in_archive", "with_styles", "balls_with_bowler_kind"), con.execute("""
            WITH p AS (SELECT DISTINCT batter_id AS p FROM deliveries WHERE batter_id > 0
                       UNION SELECT DISTINCT bowler_id FROM deliveries WHERE bowler_id > 0)
            SELECT (SELECT count(*) FROM p), (SELECT count(*) FROM p JOIN player_styles s ON s.player_id = p.p),
                   (SELECT round(100.0 * count(s.player_id) / count(*), 1) FROM deliveries d
                    LEFT JOIN player_styles s ON s.player_id = d.bowler_id AND s.bowl_kind <> '')""").fetchone()))
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--fetch", type=int, default=0, help="fetch up to N scorecards for players still missing")
    ap.add_argument("--sleep", type=float, default=1.5, help="seconds between ESPN requests (cached ones are free)")
    args = ap.parse_args()
    from backend.src import ipl_data

    store.connect(args.db).close()                       # creates player_styles in an older archive
    styles = cached(ipl_data.CACHE_DIR)
    print(f"cached scorecards: {len(styles)} players")
    if args.fetch:
        con = store.connect(args.db, read_only=True)
        known = set(styles) | {r[0] for r in con.execute("SELECT player_id FROM player_styles").fetchall()}
        picks = cover(con, known, args.fetch)
        con.close()
        client = ipl_data._client()
        print(f"fetching {len(picks)} scorecards (covering {sum(p[3] for p in picks)} players)")
        for i, (match_id, series, match, _) in enumerate(picks, 1):
            cache_hit = (ipl_data.CACHE_DIR / "match_scorecard" / f"{match_id}.json").exists()
            payload, ok = ipl_data._cached_call(client, "match_scorecard", series, match)
            if ok:
                styles.update(from_scorecard(payload))
            if i % 10 == 0 or i == len(picks):
                print(f"  {i}/{len(picks)} scorecards, {len(styles)} players", flush=True)
            if not cache_hit:
                time.sleep(args.sleep)
    for attempt in range(5):                              # the app may hold the file for a moment
        try:
            save(args.db, styles, "espn")
            break
        except Exception as exc:                          # duckdb raises IOException on a lock
            if attempt == 4:
                raise
            print(f"database busy ({str(exc)[:60]}); retrying")
            time.sleep(3)
    print(coverage(args.db))


if __name__ == "__main__":
    main()
