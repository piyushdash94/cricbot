"""Momentum-shift detection on hand-built innings with known answers."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.src.momentum_shifts import describe, innings_shifts, match_shifts  # noqa: E402


def innings(over_runs, wickets=(), inning=1, commentary=None):
    """Build deliveries: over_runs[i] runs spread over six balls; wickets as (over, ball)."""
    balls = []
    for over, total in enumerate(over_runs):
        per_ball = [total // 6 + (1 if i < total % 6 else 0) for i in range(6)]
        for ball, runs in enumerate(per_ball, 1):
            out = (over, ball) in wickets
            label = f"{over}.{ball}"
            balls.append({
                "id": f"{inning}-{label}", "inning": inning, "over": over, "label": label,
                "event": "W" if out else str(0 if out else runs), "runs": 0 if out else runs,
                "wicket": out, "boundary": not out and runs >= 4, "title": f"Bowler{over % 5} to Batter{over}",
                "text": (commentary or {}).get(label, ""),
            })
    return balls


class MomentumShiftTests(unittest.TestCase):
    def test_collapse_is_a_wicket_cluster(self):
        balls = innings([8] * 20, wickets={(9, 1), (9, 4), (10, 2)})
        events = innings_shifts(balls, batting="MI", bowling="CSK")
        cluster = next(event for event in events if event["type"] == "wicket_cluster")
        self.assertEqual((cluster["from"], cluster["to"], cluster["wickets"]), ("9.1", "10.2", 3))
        self.assertEqual(cluster["favours"], "CSK")

    def test_spread_out_wickets_are_not_a_collapse(self):
        balls = innings([8] * 20, wickets={(2, 1), (8, 1), (15, 1)})
        self.assertFalse(any(event["type"] == "wicket_cluster" for event in innings_shifts(balls, batting="MI", bowling="CSK")))

    def test_burst_and_squeeze_are_relative_to_the_innings_rate(self):
        runs = [8] * 20
        runs[15:18] = [22, 24, 20]   # death-overs assault
        runs[6:9] = [2, 3, 2]        # middle-overs squeeze, no boundaries (all singles)
        events = innings_shifts(innings(runs), batting="RCB", bowling="KKR")
        burst = next(event for event in events if event["type"] == "scoring_burst")
        self.assertEqual((burst["from"], burst["to"], burst["runs"], burst["favours"]), ("15.1", "17.6", 66, "RCB"))
        squeeze = next(event for event in events if event["type"] == "squeeze")
        self.assertEqual((squeeze["from"], squeeze["to"], squeeze["favours"]), ("6.1", "8.6", "KKR"))

    def test_chase_swing_only_while_the_chase_is_alive(self):
        alive = innings([9] * 10 + [2, 2] + [9] * 8, inning=2)
        events = innings_shifts(alive, batting="GT", bowling="RR", target=181)
        swing = next(event for event in events if event["type"] == "chase_swing")
        self.assertEqual(swing["favours"], "RR")
        self.assertIn("were needed", swing["headline"])
        dead = innings([2] * 20, inning=2)  # needs 30+ an over almost at once
        dead_events = innings_shifts(dead, batting="GT", bowling="RR", target=400)
        self.assertFalse(any(event["type"] == "chase_swing" for event in dead_events))

    def test_commentary_reveals_missed_chances(self):
        balls = innings([8] * 20, commentary={"12.3": "Dropped at long-on! A simple chance put down."})
        chance = next(event for event in innings_shifts(balls, batting="SRH", bowling="DC") if event["type"] == "missed_chance")
        self.assertEqual((chance["from"], chance["favours"]), ("12.3", "SRH"))
        self.assertIn("Dropped", describe({**chance, "dismissed": []}))

    def test_match_shifts_marks_one_decisive_event_and_orders_by_time(self):
        first = innings([8] * 20)
        second = innings([9] * 12 + [1, 1, 1] + [9] * 5, wickets={(12, 1), (12, 3), (13, 2)}, inning=2)
        shifts = match_shifts(first + second, [{"team": "LSG", "runs": 160}, {"team": "PBKS", "runs": 152}])
        self.assertEqual(sum(1 for event in shifts if event.get("decisive")), 1)
        decisive = next(event for event in shifts if event.get("decisive"))
        self.assertEqual((decisive["inning"], decisive["favours"]), (2, "LSG"))
        positions = [(event["inning"], float(event["from"])) for event in shifts]
        self.assertEqual(positions, sorted(positions))
        # One passage, one shift: overlapping readings that favour LSG collapse into one.
        self.assertEqual(sum(1 for event in shifts if event["inning"] == 2 and event["favours"] == "LSG" and 11.0 <= float(event["from"]) <= 14.6), 1)


if __name__ == "__main__":
    unittest.main()
