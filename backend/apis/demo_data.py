"""Stable demo payloads used by the UI when no live provider is configured."""

from datetime import datetime, timezone


def _overs(runs, wickets, probabilities, target=None):
    total = 0
    fallen = 0
    rows = []
    for index, (over_runs, over_wickets, win_probability) in enumerate(
        zip(runs, wickets, probabilities), start=1
    ):
        total += over_runs
        fallen += over_wickets
        row = {
            "overNumber": index,
            "overRuns": over_runs,
            "overWickets": over_wickets,
            "totalRuns": total,
            "totalWickets": fallen,
            "overRunRate": round(total / index, 2),
            "remainingBalls": max(0, 120 - index * 6),
            "predictions": {"winProbability": win_probability, "score": sum(runs)},
        }
        if target:
            required = max(0, target - total)
            balls_left = row["remainingBalls"]
            row["requiredRuns"] = required
            row["requiredRunRate"] = round(required * 6 / balls_left, 2) if balls_left else 0
        rows.append(row)
    return rows


def _innings(team, runs, wickets, overs, batters, bowlers, over_runs, over_wickets,
             probabilities, target=None):
    return {
        "team": {"abbreviation": team, "name": team},
        "runs": runs,
        "wickets": wickets,
        "overs": overs,
        "balls": round(overs * 6),
        "extras": 8,
        "fours": sum(b.get("fours", 0) for b in batters),
        "sixes": sum(b.get("sixes", 0) for b in batters),
        "inningBatsmen": batters,
        "inningBowlers": bowlers,
        "inningOvers": _overs(over_runs, over_wickets, probabilities, target),
        "inningOverGroups": [
            {"type": "POWERPLAY", "startOverNumber": 1, "endOverNumber": 6,
             "oversRuns": sum(over_runs[:6]), "oversWickets": sum(over_wickets[:6])},
            {"type": "MIDDLE_OVERS", "startOverNumber": 7, "endOverNumber": 16,
             "oversRuns": sum(over_runs[6:16]), "oversWickets": sum(over_wickets[6:16])},
            {"type": "FINAL_OVERS", "startOverNumber": 17, "endOverNumber": 20,
             "oversRuns": sum(over_runs[16:]), "oversWickets": sum(over_wickets[16:])},
        ],
        "inningPartnerships": [
            {"runs": 95, "balls": 51, "overs": 8.3,
             "player1Runs": 43, "player1Balls": 27,
             "player2Runs": 52, "player2Balls": 24,
             "player1": batters[0]["player"], "player2": batters[1]["player"]},
            {"runs": 48, "balls": 31, "overs": 5.1,
             "player1Runs": 29, "player1Balls": 17,
             "player2Runs": 19, "player2Balls": 14,
             "player1": batters[1]["player"], "player2": batters[2]["player"]},
        ],
        "inningFallOfWickets": [
            {"fowRuns": 95, "fowWicketNum": 1, "fowOvers": 8.3},
            {"fowRuns": 143, "fowWicketNum": 2, "fowOvers": 13.4},
        ],
        "inningWickets": [
            {"dismissalText": {"long": "caught in the deep"}, "player": batters[0]["player"]},
            {"dismissalText": {"long": "bowled"}, "player": batters[1]["player"]},
        ],
    }


def demo_scorecard():
    rcb_batters = [
        {"battedType": "yes", "runs": 61, "balls": 30, "fours": 5, "sixes": 4,
         "strikerate": 203.3, "isOut": True, "fowRuns": 95,
         "player": {"longName": "Phil Salt", "name": "PD Salt", "fieldingName": "Salt"}},
        {"battedType": "yes", "runs": 43, "balls": 35, "fours": 4, "sixes": 1,
         "strikerate": 122.8, "isOut": True, "fowRuns": 143,
         "player": {"longName": "Virat Kohli", "name": "V Kohli", "fieldingName": "Kohli"}},
        {"battedType": "yes", "runs": 36, "balls": 22, "fours": 2, "sixes": 2,
         "strikerate": 163.6, "isOut": False, "fowRuns": 0,
         "player": {"longName": "Rajat Patidar", "name": "RM Patidar", "fieldingName": "Patidar"}},
    ]
    pbks_batters = [
        {"battedType": "yes", "runs": 54, "balls": 32, "fours": 6, "sixes": 2,
         "strikerate": 168.8, "isOut": True, "fowRuns": 95,
         "player": {"longName": "Priyansh Arya", "name": "P Arya", "fieldingName": "Arya"}},
        {"battedType": "yes", "runs": 44, "balls": 30, "fours": 3, "sixes": 2,
         "strikerate": 146.7, "isOut": True, "fowRuns": 143,
         "player": {"longName": "Shreyas Iyer", "name": "SS Iyer", "fieldingName": "Iyer"}},
        {"battedType": "yes", "runs": 31, "balls": 21, "fours": 2, "sixes": 1,
         "strikerate": 147.6, "isOut": False, "fowRuns": 0,
         "player": {"longName": "Nehal Wadhera", "name": "N Wadhera", "fieldingName": "Wadhera"}},
    ]
    pbks_bowlers = [
        {"overs": 4.0, "balls": 24, "wickets": 2, "conceded": 34, "economy": 8.5,
         "dots": 9, "player": {"longName": "Arshdeep Singh", "fieldingName": "Arshdeep", "bowlingStyles": ["lfm"]}},
        {"overs": 4.0, "balls": 24, "wickets": 1, "conceded": 29, "economy": 7.25,
         "dots": 10, "player": {"longName": "Yuzvendra Chahal", "fieldingName": "Chahal", "bowlingStyles": ["lbg"]}},
    ]
    rcb_bowlers = [
        {"overs": 4.0, "balls": 24, "wickets": 3, "conceded": 23, "economy": 5.75,
         "dots": 12, "player": {"longName": "Krunal Pandya", "fieldingName": "Krunal", "bowlingStyles": ["sla"]}},
        {"overs": 4.0, "balls": 24, "wickets": 2, "conceded": 36, "economy": 9.0,
         "dots": 8, "player": {"longName": "Josh Hazlewood", "fieldingName": "Hazlewood", "bowlingStyles": ["rfm"]}},
    ]

    first_runs = [9, 12, 7, 14, 8, 10, 6, 11, 13, 8, 7, 10, 12, 5, 9, 6, 8, 12, 11, 12]
    second_runs = [8, 11, 10, 12, 9, 8, 7, 12, 6, 11, 10, 8, 9, 7, 6, 10, 8, 13, 9, 10]
    first = _innings("RCB", 190, 9, 20.0, rcb_batters, pbks_bowlers, first_runs,
                     [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 1, 1],
                     [48, 50, 49, 54, 52, 55, 53, 58, 62, 61, 59, 63, 67, 64, 66, 69, 72, 74, 77, 79])
    second = _innings("PBKS", 184, 7, 20.0, pbks_batters, rcb_bowlers, second_runs,
                      [0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0],
                      [52, 55, 48, 51, 50, 46, 43, 47, 40, 44, 46, 43, 39, 36, 31, 35, 27, 22, 14, 0],
                      target=191)
    return {
        "match": {
            "statusText": "RCB won by 6 runs",
            "ground": {"name": "Narendra Modi Stadium, Ahmedabad"},
            "teams": [{"team": {"abbreviation": "RCB"}}, {"team": {"abbreviation": "PBKS"}}],
        },
        "content": {
            "innings": [first, second],
            "supportInfo": {"playersOfTheMatch": [{"player": {"longName": "Krunal Pandya"}}]},
        },
    }


def dashboard_shell():
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "competition": "Indian Premier League 2025",
        "matches": [
            {"id": "rcb-pbks", "teams": ["RCB", "PBKS"], "status": "Final", "result": "RCB won by 6 runs", "venue": "Ahmedabad", "score": ["190/9", "184/7"]},
            {"id": "mi-gt", "teams": ["MI", "GT"], "status": "Live · 14.2 ov", "result": "GT need 48 from 34 balls", "venue": "Wankhede", "score": ["178/6", "131/3"]},
            {"id": "kkr-srh", "teams": ["KKR", "SRH"], "status": "Today · 7:30 PM", "result": "Match starts in 2h 18m", "venue": "Eden Gardens", "score": ["—", "—"]},
        ],
        "standings": [
            {"rank": 1, "team": "PBKS", "played": 14, "won": 9, "points": 19, "nrr": "+0.372"},
            {"rank": 2, "team": "RCB", "played": 14, "won": 9, "points": 19, "nrr": "+0.301"},
            {"rank": 3, "team": "GT", "played": 14, "won": 9, "points": 18, "nrr": "+0.254"},
            {"rank": 4, "team": "MI", "played": 14, "won": 8, "points": 16, "nrr": "+1.142"},
            {"rank": 5, "team": "DC", "played": 14, "won": 7, "points": 15, "nrr": "+0.011"},
        ],
    }
