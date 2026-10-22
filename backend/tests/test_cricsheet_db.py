"""Offline tests for the Cricsheet loader, the combined `deliveries` view and the stats API.

Fixtures: Cricsheet's own file for IPL 2025 match 1473439 (SRH v RR) plus the matching
rows of Cricsheet's player register. The same match is recorded from ESPN in
fixtures/ipl2025_srh_rr_1473439.json.gz, so the two sources can be checked against
each other and against the official card.
"""

import gzip
import json
import os
import shutil
from pathlib import Path

import pytest

from backend.db import cricsheet_load, evaluate, parse, queries, store

FIXTURES = Path(__file__).parent / "fixtures"
MATCH_ID = 1473439


@pytest.fixture(scope="module")
def cricsheet_match():
    with gzip.open(FIXTURES / "cricsheet_ipl2025_1473439.json.gz", "rt") as fh:
        return json.load(fh)


@pytest.fixture()
def cs_dir(tmp_path, cricsheet_match):
    d = tmp_path / "cricsheet"
    d.mkdir()
    (d / f"{MATCH_ID}.json").write_text(json.dumps(cricsheet_match), encoding="utf-8")
    return d


@pytest.fixture()
def register(monkeypatch):
    monkeypatch.setattr(cricsheet_load, "REGISTER_FILE", FIXTURES / "cricsheet_people_1473439.csv")
    return cricsheet_load.load_register(FIXTURES / "cricsheet_people_1473439.csv", download=False)


def _load(tmp_path, cs_dir, register, with_espn=False):
    db = tmp_path / "cs.duckdb"
    if with_espn:                                    # ESPN balls + official card for the same match
        with gzip.open(FIXTURES / "ipl2025_srh_rr_1473439.json.gz", "rt") as fh:
            espn = json.load(fh)
        con = store.connect(db)
        store.replace_match_data(con, MATCH_ID, parse.ball_rows(MATCH_ID, espn["items"]),
                                 parse.scorecard_rows(MATCH_ID, espn["scorecard"]),
                                 {"status": "ok", "ball_items": len(espn["items"]), "expected_items": espn["count"],
                                  "error": None})
        con.close()
    cricsheet_load.load(db, directory=cs_dir, download_register=False)
    return db


# ------------------------------------------------------------------ parsing

def _delivery(**kw):
    base = {"batter": "A Bat", "bowler": "B Bowl", "non_striker": "C Non", "runs": {"batter": 0, "extras": 0, "total": 0}}
    base.update(kw)
    return base


def _payload(deliveries, team="Kings XI Punjab", super_over=False):
    inn = {"team": team, "overs": [{"over": 0, "deliveries": deliveries}]}
    if super_over:
        inn["super_over"] = True
    return {"info": {"registry": {"people": {"A Bat": "aaaa0001", "B Bowl": "bbbb0002", "C Non": "cccc0003"}}},
            "innings": [inn]}


def test_noball_byes_follow_the_official_card():
    # Older Cricsheet files: "no ball + 4 byes" stored as noballs = 5
    rows = cricsheet_load.match_balls(1, _payload([_delivery(runs={"batter": 0, "extras": 5, "total": 5},
                                                             extras={"noballs": 5})]), {})
    r = rows[0]
    assert (r["noball_runs"], r["bye_runs"], r["runs_total"], r["is_legal"]) == (1, 4, 5, False)


def test_retired_hurt_is_not_a_wicket_and_kinds_map_to_bowler_credit():
    hurt = _delivery(wickets=[{"player_out": "A Bat", "kind": "retired hurt"}])
    run_out = _delivery(wickets=[{"player_out": "C Non", "kind": "run out"}])
    caught = _delivery(wickets=[{"player_out": "A Bat", "kind": "caught"}])
    r = cricsheet_load.match_balls(1, _payload([hurt, run_out, caught]), {})
    assert [x["is_wicket"] for x in r] == [False, True, True]
    assert [x["bowler_wicket"] for x in r] == [False, False, True]
    assert r[1]["player_out"] == "C Non"


def test_boundaries_need_the_ball_to_reach_the_rope():
    ran_four = _delivery(runs={"batter": 4, "extras": 0, "total": 4, "non_boundary": True})
    hit_four = _delivery(runs={"batter": 4, "extras": 0, "total": 4})
    r = cricsheet_load.match_balls(1, _payload([ran_four, hit_four]), {})
    assert [x["is_four"] for x in r] == [False, True]


def test_ids_come_from_the_register_and_teams_from_franchise_codes():
    r = cricsheet_load.match_balls(1, _payload([_delivery()]), {"aaaa0001": 253802})[0]
    assert r["batter_id"] == 253802                      # Cricinfo id, shared with ESPN
    assert r["bowler_id"] == -int("bbbb0002", 16)        # not in register: stable negative id
    assert r["batting_team"] == "PBKS"                   # Kings XI Punjab -> current code


def test_super_over_is_numbered_after_the_main_innings():
    payload = _payload([_delivery()])
    payload["innings"] = payload["innings"] * 2 + _payload([_delivery()], super_over=True)["innings"]
    assert max(r["inning"] for r in cricsheet_load.match_balls(1, payload, {})) == 3


def test_match_row_result_text():
    info = {"teams": ["Mumbai Indians", "Chennai Super Kings"], "dates": ["2019-05-12"], "venue": "Rajiv Gandhi Stadium",
            "city": "Hyderabad", "season": "2019", "event": {"stage": "Final"},
            "outcome": {"winner": "Mumbai Indians", "by": {"runs": 1}}}
    row = cricsheet_load.match_row(1181768, {"info": info})
    assert (row["title"], row["status_text"], row["winner"], row["venue"]) == \
           ("Final", "MI won by 1 run", "MI", "Rajiv Gandhi Stadium, Hyderabad")


# ---------------------------------------------------------- loading + views

def test_real_match_totals_and_ids(tmp_path, cs_dir, register):
    db = _load(tmp_path, cs_dir, register)
    con = store.connect(db, read_only=True)
    totals = con.execute("SELECT inning, runs, wickets, balls FROM cs_innings_card ORDER BY inning").fetchall()
    assert totals[0][1:] == (286, 6, 120)                # SRH 286/6
    assert con.execute("SELECT count(*) FROM players WHERE player_id < 0").fetchone()[0] == 0
    assert con.execute("SELECT DISTINCT source FROM deliveries").fetchall() == [("cricsheet",)]
    con.close()


def test_three_sources_agree_on_the_recorded_match(tmp_path, cs_dir, register):
    db = _load(tmp_path, cs_dir, register, with_espn=True)
    for source in ("espn", "cricsheet"):
        report = evaluate.evaluate(db, source=source)
        assert report["match"]["fully_correct"] == 1, (source, report["mismatches"])
    con = store.connect(db, read_only=True)
    # Cricsheet wins in `deliveries`, ESPN rows are not double counted
    assert con.execute("SELECT DISTINCT source FROM deliveries").fetchall() == [("cricsheet",)]
    con.close()


def test_queries_use_full_espn_names_when_available(tmp_path, cs_dir, register):
    db = _load(tmp_path, cs_dir, register, with_espn=True)
    top = queries.run(["top-batters", "--limit", "1"], db=db)[0]["rows"][0]
    assert top[:2] == ["Ishan Kishan", 106]


def test_queries_work_on_a_cricsheet_only_database(tmp_path, cs_dir, register):
    db = _load(tmp_path, cs_dir, register)
    blocks = queries.run(["match", str(MATCH_ID)], db=db)
    text = " ".join(b.get("text", "") for b in blocks)
    assert "[balls: cricsheet]" in text and "SRH 286/6" in text
    batting = next(b for b in blocks if b.get("title") == "Batting")
    assert batting["rows"][0][0] == "Abhishek Sharma"     # batting order from first ball faced
    note = queries.run(["commentary", str(MATCH_ID), "--limit", "1"], db=db)[0]
    assert note["type"] == "text" and "No ESPN commentary" in note["text"]


def test_old_database_without_cricsheet_tables_still_queries(tmp_path):
    db = tmp_path / "old.duckdb"
    con = store.connect(db)                      # then remove what this change added, as in an older file
    con.execute("DROP VIEW deliveries; DROP VIEW cs_innings_card; DROP VIEW cs_batting_card; "
                "DROP VIEW cs_bowling_card; DROP TABLE players; DROP TABLE cricsheet_balls")
    con.close()
    assert queries.run(["seasons"], db=db)[0]["type"] == "table"


# ----------------------------------------------------------------- stats API

@pytest.fixture()
def api(tmp_path, cs_dir, register, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.apis.main import app

    db = _load(tmp_path, cs_dir, register, with_espn=True)
    monkeypatch.setenv("CRICBOT_DB", str(db))
    return TestClient(app)


def test_stats_api_endpoints(api):
    status = api.get("/api/stats/status").json()
    assert status["available"] and status["sources"] == {"cricsheet": 1, "espn": 1} and status["years"] == [2025]
    batting = api.get("/api/stats/batting", params={"year": "2025", "limit": 1}).json()["blocks"][0]
    assert batting["rows"][0][:2] == ["Ishan Kishan", 106]
    found = api.get("/api/stats/players", params={"q": "kishan"}).json()["blocks"][0]["rows"]
    profile = api.get(f"/api/stats/players/{found[0][0]}").json()["blocks"]
    assert profile[0]["title"].startswith("Ishan Kishan: batting by year")
    h2h = api.get("/api/stats/h2h", params={"team1": "SRH", "team2": "RR"}).json()["blocks"][0]
    assert h2h["columns"][2:4] == ["SRH won", "RR won"] and h2h["rows"][-1][1:3] == [1, 1]
    assert api.get("/api/stats/records", params={"limit": 1}).json()["blocks"][0]["rows"][0][1] == "286/6"


def test_stats_api_slices_and_match_facts(api):
    get = lambda path, **params: api.get(f"/api/stats/{path}", params=params)    # noqa: E731
    best = get("slice", subject="batter", metric="high_score").json()["blocks"][0]
    assert best["rows"][0][:2] == ["Ishan Kishan", "106*"]
    split = get("slice", subject="batter", metric="runs", player="Ishan Kishan", split="innings").json()["blocks"][0]
    assert split["columns"][:3] == ["batter", "innings", "runs"] and split["rows"][0][2] == 106
    assert get("slice", metric="economy").json()["blocks"][0]["text"].startswith("'economy' is not")
    assert get("slice", overs="death").status_code == 422 and get("slice", innings=3).status_code == 422
    chase = get("slice", metric="runs", innings=2, result="lost", shrink=1, min_balls=0)  # query values arrive as strings
    assert chase.status_code == 200 and "chasing" in chase.json()["blocks"][0]["title"]
    assert get("partnerships", innings=1, by="pair").status_code == 200
    assert get("partnerships", limit=1).json()["blocks"][0]["rows"][0][1] == 111
    assert get("hattricks").json()["blocks"][-1]["text"].startswith("0 hat-tricks")
    assert get("awards").json()["blocks"][0]["rows"][0][:2] == ["Ishan Kishan", 1]
    assert {r[0] for r in get("impact").json()["blocks"][0]["rows"]} == {"Adam Zampa", "Sanju Samson"}   # ESPN full names
    assert get("toss").json()["blocks"][0]["rows"][0][1] == 1                  # one match with a result


def test_stats_api_validation_and_missing_database(api, monkeypatch, tmp_path):
    assert api.get("/api/stats/batting", params={"year": "20x5"}).status_code == 422
    assert api.get("/api/stats/h2h", params={"team1": "MI"}).status_code == 422
    monkeypatch.setenv("CRICBOT_DB", str(tmp_path / "missing.duckdb"))
    assert api.get("/api/stats/status").json()["available"] is False
    assert api.get("/api/stats/batting").status_code == 503
