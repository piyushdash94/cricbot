"""Offline tests for the ball-by-ball source ladder (Cricsheet, ESPN, fallbacks).

Payloads are synthetic but follow the published Cricsheet JSON format and the
ESPN play-by-play item shape used by cricdata.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.src import cricsheet, ipl_data  # noqa: E402


def delivery(batter, bowler, runs=0, extras=None, wicket=None, non_boundary=False):
    extra_total = sum((extras or {}).values())
    row = {
        "batter": batter, "bowler": bowler, "non_striker": "Partner",
        "runs": {"batter": runs, "extras": extra_total, "total": runs + extra_total},
    }
    if non_boundary:
        row["runs"]["non_boundary"] = True
    if extras:
        row["extras"] = extras
    if wicket:
        row["wickets"] = [wicket]
    return row


def cricsheet_match(teams=("Royal Challengers Bangalore", "Kings XI Punjab"), day="2025-06-03"):
    return {
        "meta": {"data_version": "1.1.0"},
        "info": {"dates": [day], "teams": list(teams), "event": {"name": "Indian Premier League", "stage": "Final"}, "venue": "Narendra Modi Stadium"},
        "innings": [
            {"team": teams[0], "overs": [
                {"over": 0, "deliveries": [
                    delivery("Salt", "Arshdeep", 4),
                    delivery("Salt", "Arshdeep", extras={"wides": 1}),
                    delivery("Salt", "Arshdeep", 0, wicket={"player_out": "Salt", "kind": "caught", "fielders": [{"name": "Iyer"}]}),
                    delivery("Kohli", "Arshdeep", 4, non_boundary=True),
                    delivery("Kohli", "Arshdeep", extras={"legbyes": 1}),
                    delivery("Patidar", "Arshdeep", 6),
                    delivery("Patidar", "Arshdeep", 1),
                ]},
            ]},
            {"team": teams[1], "overs": [{"over": 0, "deliveries": [delivery("Arya", "Hazlewood", 1)]}]},
            {"team": teams[1], "super_over": True, "overs": [{"over": 0, "deliveries": [delivery("Arya", "Hazlewood", 6)]}]},
        ],
    }


class CricsheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "1473511.json").write_text(json.dumps(cricsheet_match()))
        self.env = patch.dict(os.environ, {"CRICSHEET_DIR": str(self.dir)})
        self.env.start()
        cricsheet._index.cache_clear()

    def tearDown(self):
        self.env.stop()
        cricsheet._index.cache_clear()
        self.tmp.cleanup()

    def test_matches_renamed_franchises_across_utc_date_shift(self):
        found = cricsheet.find_match("2025-06-02T14:00:00Z", ["Royal Challengers Bengaluru", "Punjab Kings"])
        self.assertEqual(found.name, "1473511.json")
        self.assertIsNone(cricsheet.find_match("2025-06-10", ["Royal Challengers Bengaluru", "Punjab Kings"]))
        self.assertIsNone(cricsheet.find_match("2025-06-03", ["Mumbai Indians", "Punjab Kings"]))

    def test_normalizes_every_delivery_with_scorecard_conventions(self):
        balls = cricsheet.load_deliveries(self.dir / "1473511.json")["balls"]
        self.assertEqual(len(balls), 8)  # super over excluded
        labels = [ball["label"] for ball in balls[:7]]
        self.assertEqual(labels, ["0.1", "0.2", "0.2", "0.3", "0.4", "0.5", "0.6"])
        wide, wicket, run_four, legbye, six = balls[1], balls[2], balls[3], balls[4], balls[5]
        self.assertEqual(wide["event"], "1wd")
        self.assertEqual(wicket["event"], "W")
        self.assertIn("caught (Iyer)", wicket["text"])
        self.assertEqual(run_four["event"], "4")
        self.assertFalse(run_four["boundary"])  # four runs, not a boundary
        self.assertEqual(legbye["event"], "1lb")
        self.assertTrue(six["boundary"])
        self.assertEqual(balls[6]["score"], "17/1")
        self.assertEqual(balls[7]["inning"], 2)

    def test_index_is_built_on_demand(self):
        self.assertFalse((self.dir / cricsheet.INDEX_FILE).exists())
        cricsheet.find_match("2025-06-03", ["Royal Challengers Bengaluru", "Punjab Kings"])
        self.assertTrue((self.dir / cricsheet.INDEX_FILE).exists())

    def test_sync_extracts_archive_and_indexes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("1000.json", json.dumps(cricsheet_match(("Chennai Super Kings", "Mumbai Indians"), "2023-04-08")))
            archive.writestr("README.txt", "ignored")
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=buffer.getvalue())))
        target = self.dir / "synced"
        self.assertEqual(cricsheet.sync(target, client=client), 1)
        self.assertTrue((target / "1000.json").exists())
        self.assertFalse((target / "README.txt").exists())

    def detail(self, client):
        with tempfile.TemporaryDirectory() as cache, patch.object(ipl_data, "CACHE_DIR", Path(cache)), patch.object(ipl_data, "_client", return_value=client):
            first = ipl_data.get_match_detail(ipl_data.IPL_SERIES[2025]["slug"], ipl_data.DEMO_MATCH_SLUG)
            second = ipl_data.get_match_detail(ipl_data.IPL_SERIES[2025]["slug"], ipl_data.DEMO_MATCH_SLUG)
        return first, second

    def test_cricsheet_is_canonical_when_providers_are_offline(self):
        class Offline:
            def __getattr__(self, name):
                def call(*_args):
                    raise RuntimeError("offline")
                return call

        detail, _ = self.detail(Offline())
        self.assertEqual(detail["ball_coverage"]["level"], "cricsheet")
        self.assertEqual(len(detail["balls"]), 8)
        self.assertEqual(detail["match"]["title"], "RCB vs PBKS")
        self.assertEqual(detail["innings"][0]["runs"], 17)
        self.assertEqual(detail["consistency"]["status"], "single-source")
        self.assertFalse(next(row for row in detail["sources"] if row["name"] == "Commentary")["available"])

    def test_espn_enrichment_is_merged_verified_and_cached(self):
        reference = {"content": {"innings": [
            {"team": {"abbreviation": "RCB"}, "runs": 17, "wickets": 1, "inningBatsmen": [], "inningBowlers": []},
            {"team": {"abbreviation": "PBKS"}, "runs": 1, "wickets": 0, "inningBatsmen": [], "inningBowlers": []},
        ]}}
        commentary = [[{"id": str(index), "over": {"actual": float(f"0.{index}")}, "innings": {"number": 1}, "playType": {"description": "run"}, "scoreValue": 0, "text": f"line {index}"} for index in range(1, 8)]]

        class Online:
            calls: list = []

            def match_scorecard(self, *_args):
                Online.calls.append("match_scorecard")
                return reference

            def match_ball_by_ball(self, *_args):
                Online.calls.append("match_ball_by_ball")
                return commentary

            def __getattr__(self, _name):
                def missing(*_args):
                    raise RuntimeError("unused")
                return missing

        detail, again = self.detail(Online())
        self.assertEqual(detail["consistency"]["status"], "verified")
        self.assertEqual(detail["commentary_count"], 7)
        self.assertEqual(detail["balls"][0]["commentary"], "line 1")
        self.assertEqual(again["commentary_count"], 7)
        self.assertEqual(sorted(Online.calls), ["match_ball_by_ball", "match_scorecard"])  # second load came from cache

    def test_result_is_checked_against_derived_scorecard(self):
        innings = [{"team": "RCB", "runs": 190, "wickets": 9}, {"team": "PBKS", "runs": 184, "wickets": 7}]
        info = {"outcome": {"winner": "Royal Challengers Bengaluru", "by": {"runs": 6}}}
        self.assertEqual(ipl_data.internal_check(info, innings)["status"], "ok")
        self.assertEqual(ipl_data.internal_check({"outcome": {"winner": "Royal Challengers Bengaluru", "by": {"runs": 7}}}, innings)["status"], "mismatch")
        chase = [{"team": "DCH", "runs": 150, "wickets": 8}, {"team": "KKR", "runs": 151, "wickets": 4}]
        self.assertEqual(ipl_data.internal_check({"outcome": {"winner": "Kolkata Knight Riders", "by": {"wickets": 6}}}, chase)["status"], "ok")
        self.assertEqual(ipl_data.internal_check({"outcome": {"winner": "Kolkata Knight Riders", "by": {"wickets": 5}}}, chase)["status"], "mismatch")
        self.assertEqual(ipl_data.internal_check({"outcome": {"winner": "Kolkata Knight Riders", "by": {"wickets": 5}, "method": "D/L"}}, chase)["status"], "skipped")

    def test_mismatch_is_reported(self):
        canonical = [{"number": 1, "team": "RCB", "runs": 190, "wickets": 9}]
        reference = {"content": {"innings": [{"team": {"abbreviation": "RCB"}, "runs": 191, "wickets": 9}]}}
        self.assertEqual(ipl_data.consistency_check(canonical, reference)["status"], "mismatch")


class EspnPlayByPlayTests(unittest.TestCase):
    def item(self, actual, play, score, *, wicket=False, number=1):
        return {
            "id": f"{number}-{actual}", "playType": {"description": play}, "scoreValue": score,
            "over": {"actual": actual}, "innings": {"number": number},
            "bowler": {"athlete": {"displayName": "Hazlewood"}}, "batsman": {"athlete": {"displayName": "Iyer"}, "runs": score},
            "dismissal": {"dismissal": wicket}, "text": "Commentary line", "shortText": "Hazlewood to Iyer",
        }

    def test_normalizes_grouped_ball_items(self):
        payload = [[self.item(0.2, "four", 4), self.item(0.1, "no run", 0)], [self.item(0.1, "out", 0, wicket=True, number=2), self.item(0.1, "wide", 1, number=2)]]
        balls = ipl_data._normalize_espn_balls(payload)
        self.assertEqual([ball["label"] for ball in balls], ["0.1", "0.2", "0.1", "0.1"])
        self.assertTrue(balls[1]["boundary"])
        self.assertEqual({ball["event"] for ball in balls[2:]}, {"W", "1wd"})
        self.assertEqual(balls[0]["title"], "Hazlewood to Iyer")

    def test_rejects_non_list_payloads(self):
        self.assertEqual(ipl_data._normalize_espn_balls({"comments": []}), [])

    def test_espn_feed_is_used_when_cricsheet_has_no_match(self):
        class Client:
            def match_ball_by_ball(self, *_args):
                return [[EspnPlayByPlayTests.item(None, 0.1, "four", 4)]]

            def __getattr__(self, _name):
                def missing(*_args):
                    raise RuntimeError("offline")
                return missing

        with tempfile.TemporaryDirectory() as empty, patch.dict(os.environ, {"CRICSHEET_DIR": empty}), patch.object(ipl_data, "_client", return_value=Client()):
            cricsheet._index.cache_clear()
            detail = ipl_data.get_match_detail(ipl_data.IPL_SERIES[2025]["slug"], ipl_data.DEMO_MATCH_SLUG)
        cricsheet._index.cache_clear()
        self.assertEqual(detail["ball_coverage"]["level"], "full")
        self.assertEqual(detail["sources"][2]["provider"], "ESPN play-by-play")


if __name__ == "__main__":
    unittest.main()
