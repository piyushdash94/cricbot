"""Offline tests for the DuckDB ingest layer (backend/db).

Fixture: IPL 2025, 2nd match, SRH vs RR (match 1473439), recorded from the
ESPN play-by-play feed and the Cricinfo scorecard. It contains the
"(no ball) 4 byes" delivery whose scoreValue is wrong upstream.
"""

import gzip
import json
from pathlib import Path

import pytest

from backend.db import evaluate, ingest, parse, store

FIXTURE = Path(__file__).parent / "fixtures" / "ipl2025_srh_rr_1473439.json.gz"
MATCH_ID = 1473439


@pytest.fixture(scope="module")
def recorded():
    with gzip.open(FIXTURE, "rt") as fh:
        return json.load(fh)


@pytest.fixture()
def db(tmp_path, recorded):
    path = tmp_path / "test.duckdb"
    con = store.connect(path)
    store.upsert_matches(con, [{
        "match_id": MATCH_ID, "season": "2025", "series_path": "ipl-2025-1449924",
        "match_path": "sunrisers-hyderabad-vs-rajasthan-royals-2nd-match-1473439", "title": "2nd Match",
        "start_date": "2025-03-23", "venue": "Hyderabad", "team1": "SRH", "team2": "RR", "winner": "SRH",
        "status_text": "SRH won by 44 runs", "format": "T20", "stage": "FINISHED", "is_cancelled": False,
    }])
    store.replace_match_data(
        con, MATCH_ID, parse.ball_rows(MATCH_ID, recorded["items"]),
        parse.scorecard_rows(MATCH_ID, recorded["scorecard"]),
        {"status": "ok", "ball_items": len(recorded["items"]), "expected_items": recorded["count"], "error": None},
    )
    yield con, path
    con.close()


def _item(**over):
    """Minimal ESPN play-by-play item for synthetic edge cases."""
    base = {
        "id": "x1", "sequence": 1, "scoreValue": 0, "shortText": "A to B, no run", "text": "",
        "playType": {"description": "no run"}, "team": {"abbreviation": "AAA"}, "innings": {"number": 1},
        "over": {"number": 1, "ball": 1, "actual": 0.1, "wide": 0, "noBall": 0, "byes": 0, "legByes": 0},
        "batsman": {"athlete": {"id": "1", "displayName": "B"}, "runs": 0},
        "otherBatsman": {"athlete": {"id": "2", "displayName": "C"}},
        "bowler": {"athlete": {"id": "9", "displayName": "A"}},
        "dismissal": {"dismissal": False, "bowled": False, "type": ""},
    }
    for key, value in over.items():
        base[key] = {**base[key], **value} if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return base


# ------------------------------------------------------------------ parsing

def test_recorded_match_totals(db):
    con, _ = db
    rows = con.execute("SELECT inning, runs, wickets, balls FROM innings_card ORDER BY inning").fetchall()
    ref = con.execute("SELECT inning, runs, wickets, balls FROM ref_innings ORDER BY inning").fetchall()
    assert rows == ref
    assert rows[0][1:] == (286, 6, 120)          # SRH 286/6 in 20 overs


def test_noball_with_byes_uses_components_not_score_value(db):
    con, _ = db
    row = con.execute(
        "SELECT runs_total, score_value, noball_runs, bye_runs, is_legal FROM balls "
        "WHERE match_id = ? AND short_text LIKE '%(no ball) 4 byes%'", [MATCH_ID]
    ).fetchone()
    assert row == (5, 1, 1, 4, False)            # upstream scoreValue=1 is wrong; 1 nb + 4 byes


def test_retired_hurt_is_not_a_wicket():
    rows = parse.ball_rows(1, [_item(dismissal={"dismissal": True, "bowled": False, "type": "retired not out (hurt)",
                                                "batsman": {"athlete": {"id": "1", "displayName": "B"}}})])
    assert rows[0]["is_wicket"] is False
    assert rows[0]["dismissal_type"] == "retired not out (hurt)"


def test_retired_out_is_a_wicket_but_not_the_bowlers():
    rows = parse.ball_rows(1, [_item(dismissal={"dismissal": True, "bowled": False, "type": "retired out",
                                                "batsman": {"athlete": {"id": "1", "displayName": "B"}}})])
    assert rows[0]["is_wicket"] is True and rows[0]["bowler_wicket"] is False


def test_placeholder_items_are_skipped_and_duplicates_collapse():
    placeholder = _item(id="p", shortText="", over={"ball": 0, "actual": 0})
    real = _item()
    assert len(parse.ball_rows(1, [placeholder, real, dict(real)])) == 1


def test_wide_is_not_a_ball_faced_but_is_charged_to_bowler():
    row = parse.ball_rows(1, [_item(scoreValue=1, playType={"description": "wide"}, over={"wide": 1})])[0]
    assert (row["runs_total"], row["wide_runs"], row["is_legal"], row["is_wide"]) == (1, 1, False, True)


def test_only_finished_t20_matches_are_ingested():
    assert parse.is_ingestable({"format": "T20", "stage": "FINISHED"})
    assert not parse.is_ingestable({"format": "ODI", "stage": "FINISHED"})
    assert not parse.is_ingestable({"format": "T20", "stage": "SCHEDULED", "state": "PRE"})
    assert not parse.is_ingestable({"format": "T20", "stage": "FINISHED", "isCancelled": True})


# --------------------------------------------------------------- evaluation

def test_evaluation_is_exact_on_recorded_match(db):
    con, path = db
    con.close()
    report = evaluate.evaluate(path)
    assert report["match"] == {"evaluated": 1, "fully_correct": 1, "accuracy": 1.0, "explained": 0, "unexplained": 0}
    assert all(level["accuracy"] == 1.0 for level in report["levels"].values())


def test_evaluation_catches_a_missing_delivery(db):
    con, path = db
    con.execute("DELETE FROM balls WHERE match_id = ? AND inning = 1 AND is_four "
                "AND sequence = (SELECT min(sequence) FROM balls WHERE inning = 1 AND is_four)", [MATCH_ID])
    con.close()
    report = evaluate.evaluate(path)
    assert report["match"]["fully_correct"] == 0
    assert report["levels"]["innings"]["exact"] == 1          # second innings still fine
    assert report["mismatches"]["batting"] and report["mismatches"]["bowling"]


def test_reingest_is_idempotent(db, recorded):
    con, _ = db
    before = con.execute("SELECT count(*) FROM balls").fetchone()[0]
    store.replace_match_data(con, MATCH_ID, parse.ball_rows(MATCH_ID, recorded["items"]),
                             parse.scorecard_rows(MATCH_ID, recorded["scorecard"]),
                             {"status": "ok", "ball_items": 1, "expected_items": 1, "error": None})
    assert con.execute("SELECT count(*) FROM balls").fetchone()[0] == before
    assert store.completed_match_ids(con) == {MATCH_ID}


# ------------------------------------------------------------------ fetching

class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeSession:
    """Serves pages; fails the first request for page 2 with ESPN's error body."""

    def __init__(self, pages, count):
        self.pages, self.count, self.failed = pages, count, False

    def get(self, url, params):
        n = params["page"]
        if n == 2 and not self.failed:
            self.failed = True
            return FakeResponse({"code": 2503})
        return FakeResponse({"commentary": {"count": self.count, "pageCount": len(self.pages), "items": self.pages[n - 1]}})


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda _s: None)


def test_fetch_retries_transient_error_body(monkeypatch, no_sleep):
    pages = [[{"id": "a"}, {"id": "b"}], [{"id": "c"}]]
    session = FakeSession(pages, 3)
    monkeypatch.setattr(ingest, "_session", lambda: session)
    items, expected = ingest.fetch_ball_items(1)
    assert expected == 3 and {i["id"] for i in items} == {"a", "b", "c"}
    assert session.failed                                       # the error page really was retried


def test_short_feed_is_refetched_then_marked_partial(monkeypatch, no_sleep):
    session = FakeSession([[{"id": "a"}], [{"id": "b"}]], count=5)
    session.failed = True
    monkeypatch.setattr(ingest, "_session", lambda: session)
    items, expected = ingest.fetch_ball_items(1)
    assert (len(items), expected) == (2, 5)                     # returned, not raised

    class Card:
        def match_scorecard(self, *_):
            return {"content": {"innings": []}}

    monkeypatch.setattr(ingest, "client", lambda: Card())
    monkeypatch.setattr(ingest.parse, "ball_rows", lambda mid, its: [{"x": 1}] * len(its))
    result = ingest.fetch_match({"match_id": 1, "series_path": "s", "match_path": "m"})
    assert result["log"]["status"] == "partial"


# ------------------------------------------------------------- read helper

def test_stored_match_detail_has_api_shape(db):
    """read.match_detail returns the shape get_match_detail() serves, ready to wire into the API."""
    from backend.db import read

    con, path = db
    con.close()                      # DuckDB: no read-only opener while a writer holds the file
    detail = read.match_detail("sunrisers-hyderabad-vs-rajasthan-royals-2nd-match-1473439", db_path=path)
    assert detail["ball_coverage"]["level"] == "full"
    assert detail["innings"][0]["runs"] == 286 and detail["innings"][0]["overs"] == "20"
    assert detail["balls"][-1]["score"] == "{}/{}".format(detail["innings"][1]["runs"], detail["innings"][1]["wickets"])
    ui_keys = {"id", "inning", "label", "event", "runs", "wicket", "boundary", "title", "text", "score", "win_probability"}
    assert ui_keys <= set(detail["balls"][0])
    assert read.match_detail("not-stored-123", db_path=path) is None


# ------------------------------------------------------------- query tool

def _run_queries(capsys, path, *argv):
    from backend.db import queries
    queries.main(["--db", str(path), *argv])
    return capsys.readouterr().out


@pytest.mark.parametrize("argv, expected", [
    (("top-batters", "--year", "2025", "--limit", "1"), "Ishan Kishan 106"),
    (("top-bowlers", "--limit", "1"), "Tushar Deshpande"),
    (("records", "--limit", "1"), "286/6"),
    (("match", str(MATCH_ID)), "SRH 286/6 (20 ov)"),
    (("player", "ishan kishan"), "Ishan Kishan: batting by phase"),
    (("h2h", "srh", "rr"), "Total 1 1 0 0"),
    (("matches", "--team", "RR"), "1473439"),
    (("commentary", str(MATCH_ID), "--search", "(no ball) 4 byes"), "4 byes"),
    (("sql", "SELECT count(*) AS n FROM d WHERE inning = 1"), "n"),
    (("top-batters", "--year", "2016"), "no rows"),
])
def test_query_commands(db, capsys, argv, expected):
    con, path = db
    con.close()
    out = " ".join(_run_queries(capsys, path, *argv).split())     # column widths vary; compare words
    assert expected in out, out


def test_kxip_is_treated_as_pbks():
    from backend.db.queries import Q
    assert Q.team("kxip") == "PBKS" and Q.team("dd") == "DC" and Q.team("RCB") == "RCB"
