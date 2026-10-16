"""Ingest IPL (T20) matches from cricdata into DuckDB.

    python -m backend.db.ingest --seasons 2025            # one season
    python -m backend.db.ingest --seasons all             # every IPL edition
    python -m backend.db.ingest --seasons 2024 --force    # re-fetch even if stored

Safe to re-run: matches already stored with status 'ok' are skipped, and each
match is replaced atomically, so an interrupted run loses at most the matches
in flight.

Per match this makes 2 requests: the scorecard page and one ball-by-ball request.
"""

from __future__ import annotations

import argparse
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import parse, store

log = logging.getLogger("cricbot.ingest")

IPL_ANCHOR_SERIES = "ipl-2025-1449924"     # its page links every other IPL edition
PLAYBYPLAY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/8676/playbyplay"
PAGE_LIMIT = 500        # a T20 incl. extras and super overs is ~230-300 items

_local = threading.local()


def client():
    """One cricdata client per thread (HTTP sessions are not shared across threads)."""
    if not hasattr(_local, "client"):
        from cricdata import CricinfoClient
        _local.client = CricinfoClient()
    return _local.client


def _session():
    if not hasattr(_local, "session"):
        from cricdata._session import Session   # cricdata's browser-impersonating session
        _local.session = Session()
    return _local.session


class IncompleteFeed(RuntimeError):
    pass


class NotFound(RuntimeError):
    """Permanent: the provider has no data for this resource. Never retried."""


def retry(fn: Callable[[], Any], attempts: int = 6, base: float = 2.0, what: str = "request") -> Any:
    """Exponential backoff with jitter: ~1, 2, 4, 8, 16s. Upstream returns 502/2503 under bursts."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except NotFound:
            raise
        except Exception as exc:                  # network + transient upstream errors
            if attempt == attempts:
                raise RuntimeError(f"{what}: {exc}") from exc
            delay = base ** (attempt - 1) + random.random()
            log.debug("retry %s/%s after %s: %s", attempt, attempts, round(delay, 1), exc)
            time.sleep(delay)


def fetch_ball_items(match_id: int) -> tuple[list[dict[str, Any]], int]:
    """All play-by-play items for a match, verified against the API's own `count`.

    cricdata.match_ball_by_ball() stops at the first page without items and has
    no retries, so one transient upstream error (e.g. a {"code": 2503} body)
    silently truncates the match. Here every page is retried until it parses.
    """
    def page(n: int) -> dict[str, Any]:
        # limit=PAGE_LIMIT returns a whole T20 in one request (default is 25/page = ~11 requests).
        # Pagination below still runs if a match ever exceeds the limit.
        r = _session().get(PLAYBYPLAY_URL, params={"event": match_id, "page": n, "limit": PAGE_LIMIT})
        try:
            body = r.json()
        except ValueError:
            body = {}
        if body.get("code") == 2404:              # ESPN's "not found" (HTTP 500 + code 2404)
            raise NotFound(f"ball-by-ball p{n}: no feed for match {match_id}")
        r.raise_for_status()
        if "commentary" not in body:
            raise IncompleteFeed(f"page {n}: {str(body)[:80]}")
        return body["commentary"]

    def all_pages() -> tuple[dict[str, dict[str, Any]], int]:
        first = retry(lambda: page(1), what="ball-by-ball p1")
        items = list(first.get("items") or [])
        for n in range(2, int(first.get("pageCount") or 1) + 1):
            items.extend(retry(lambda n=n: page(n), what=f"ball-by-ball p{n}").get("items") or [])
        return {str(i.get("id")): i for i in items}, int(first.get("count") or 0)

    unique, expected = all_pages()
    if expected and len(unique) < expected:          # refetch once in case a page changed under us
        again, expected = all_pages()
        unique.update(again)
    # Still short is returned as-is: some old matches are a few items under `count` upstream.
    # The caller stores it as 'partial' and backend.db.evaluate decides whether it reconciles.
    return list(unique.values()), expected


# ---------------------------------------------------------------- discovery

def ipl_series() -> list[dict[str, str]]:
    """Every IPL edition as {season, path}, discovered from the 2025 series page."""
    content = retry(lambda: client().series_matches(IPL_ANCHOR_SERIES), what="series list")["content"]
    found = {"2025": IPL_ANCHOR_SERIES}
    for s in content.get("recentSimilarSerieses") or []:
        found[str(s["season"])] = f"{s['slug']}-{s['objectId']}"
    return [{"season": k, "path": v} for k, v in sorted(found.items())]


def season_matches(series: dict[str, str]) -> list[dict[str, Any]]:
    content = retry(lambda: client().series_matches(series["path"]), what=f"series {series['season']}")["content"]
    return content.get("matches") or []


# ------------------------------------------------------------------ per match

def fetch_match(m: dict[str, Any]) -> dict[str, Any]:
    """Network only. Returns parsed rows; never raises (errors become a log row)."""
    match_id = m["match_id"]
    reference: dict[str, list[dict[str, Any]]] = {}
    try:
        scorecard = retry(lambda: client().match_scorecard(m["series_path"], m["match_path"]), what="scorecard")
        reference = parse.scorecard_rows(match_id, scorecard)
        try:
            items, expected = fetch_ball_items(match_id)
        except NotFound as exc:                     # keep the scorecard; there is simply no ball feed
            return {"match_id": match_id, "balls": [], "reference": reference,
                    "log": {"status": "no_balls", "ball_items": 0, "expected_items": 0, "error": str(exc)[:300]}}
        balls = parse.ball_rows(match_id, items)
        status = "no_balls" if not balls else "partial" if expected and len(items) < expected else "ok"
        return {"match_id": match_id, "balls": balls, "reference": reference,
                "log": {"status": status, "ball_items": len(items), "expected_items": expected, "error": None}}
    except Exception as exc:
        return {"match_id": match_id, "balls": [], "reference": {},
                "log": {"status": "error", "ball_items": 0, "expected_items": None, "error": str(exc)[:300]}}


def ingest(seasons: list[str] | None = None, db_path: str | None = None, workers: int = 2,
           force: bool = False, limit: int | None = None) -> dict[str, int]:
    con = store.connect(db_path)
    editions = ipl_series()
    if seasons:
        wanted = set(seasons)
        editions = [e for e in editions if e["season"] in wanted or e["season"][:4] in wanted]

    todo: list[dict[str, Any]] = []
    for edition in editions:
        raw = season_matches(edition)
        rows = [parse.match_row(r, edition["path"], edition["season"]) for r in raw if parse.is_ingestable(r)]
        store.upsert_matches(con, rows)
        log.info("%s: %s T20 matches listed (%s raw)", edition["season"], len(rows), len(raw))
        todo.extend(rows)

    done = set() if force else store.completed_match_ids(con)
    todo = [m for m in todo if m["match_id"] not in done][:limit]
    log.info("fetching %s matches with %s workers", len(todo), workers)

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:          # threads fetch, main thread writes
        futures = [pool.submit(fetch_match, m) for m in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            store.replace_match_data(con, res["match_id"], res["balls"], res["reference"], res["log"])
            status = res["log"]["status"]
            counts[status] = counts.get(status, 0) + 1
            if status == "partial":
                # ESPN's own `count` is higher than the items it serves; usually a non-delivery item.
                # Not an error: backend.db.evaluate shows whether the match still reconciles.
                log.info("match %s: partial (feed count %s, served %s) - check with evaluate",
                         res["match_id"], res["log"]["expected_items"], res["log"]["ball_items"])
            elif status != "ok":
                log.warning("match %s: %s %s", res["match_id"], status, res["log"]["error"] or "")
            if i % 25 == 0 or i == len(todo):
                log.info("%s/%s done %s", i, len(todo), counts)
    con.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", default="2025", help="comma list (2023,2024) or 'all'")
    ap.add_argument("--db", default=None, help=f"DuckDB file (default {store.DEFAULT_DB})")
    ap.add_argument("--workers", type=int, default=2, help="parallel matches; >2 triggers upstream 502s")
    ap.add_argument("--force", action="store_true", help="re-fetch matches already stored")
    ap.add_argument("--limit", type=int, default=None, help="max matches to fetch (for quick tests)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    seasons = None if args.seasons == "all" else [s.strip() for s in args.seasons.split(",")]
    print(ingest(seasons, args.db, args.workers, args.force, args.limit))


if __name__ == "__main__":
    main()
