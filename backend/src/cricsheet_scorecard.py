"""Derive a complete scorecard from Cricsheet deliveries.

The output uses the same ESPNcricinfo-shaped ``{"match", "content": {"innings"}}``
structure the rest of the backend already consumes, so scorecard, ball-by-ball,
and analytics all come from one set of deliveries and cannot disagree.

Scoring conventions (Laws of Cricket / standard scorecards):
- wides are not balls faced; no-balls are
- bowlers are charged batter runs + wides + no-balls, never byes, leg-byes, or penalties
- run outs, retirements, obstruction, and handled-ball are not bowler wickets
- "retired hurt" / "retired not out" do not count as team wickets
"""

from __future__ import annotations

from typing import Any

from backend.src.ipl_teams import team_code

BOWLER_WICKETS = {"bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket"}
NOT_TEAM_WICKETS = {"retired hurt", "retired not out"}


def overs_text(legal_balls: int) -> float:
    """Cricket overs notation: 19 overs and 4 balls is 19.4."""
    return float(f"{legal_balls // 6}.{legal_balls % 6}")


def dismissal_text(wicket: dict[str, Any], bowler: str) -> str:
    kind = wicket.get("kind", "")
    fielders = [item.get("name", "") for item in wicket.get("fielders", []) if item.get("name")]
    fielder = fielders[0] if fielders else ""
    if kind == "caught":
        return f"c {fielder} b {bowler}" if fielder and fielder != bowler else f"c & b {bowler}"
    if kind == "caught and bowled":
        return f"c & b {bowler}"
    if kind == "bowled":
        return f"b {bowler}"
    if kind == "lbw":
        return f"lbw b {bowler}"
    if kind == "stumped":
        return f"st {fielder} b {bowler}" if fielder else f"st b {bowler}"
    if kind == "hit wicket":
        return f"hit wicket b {bowler}"
    if kind == "run out":
        return f"run out ({' / '.join(fielders)})" if fielders else "run out"
    return kind


def _person(name: str) -> dict[str, str]:
    return {"longName": name, "name": name, "fieldingName": name.split(" ")[-1] if name else name}


def derive_innings(inning: dict[str, Any], *, target: int | None = None, ball_limit: int = 120) -> dict[str, Any]:
    """Build one ESPN-shaped innings record from a Cricsheet innings."""
    batters: dict[str, dict[str, Any]] = {}
    bowlers: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    total = wickets = legal = fours = sixes = 0
    extras = {"wides": 0, "noballs": 0, "byes": 0, "legbyes": 0, "penalty": 0}
    overs_out: list[dict[str, Any]] = []
    fall: list[dict[str, Any]] = []
    partnerships: list[dict[str, Any]] = []
    partnership = {"runs": 0, "balls": 0, "players": []}

    def batter(name: str) -> dict[str, Any]:
        if name not in batters:
            batters[name] = {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "isOut": False, "dismissal": "", "fowRuns": None}
            order.append(name)
        return batters[name]

    def bowler(name: str) -> dict[str, Any]:
        return bowlers.setdefault(name, {"balls": 0, "conceded": 0, "wickets": 0, "dots": 0, "maidens": 0, "order": len(bowlers)})

    for over in inning.get("overs", []):
        over_runs = over_wickets = 0
        over_bowlers: dict[str, int] = {}
        over_legal = 0
        for delivery in over.get("deliveries", []):
            runs = delivery.get("runs", {})
            extra = delivery.get("extras", {})
            striker, partner, bowler_name = delivery.get("batter", ""), delivery.get("non_striker", ""), delivery.get("bowler", "")
            batter(striker)
            if partner:
                batter(partner)
            stats = bowler(bowler_name)
            is_wide, is_noball = "wides" in extra, "noballs" in extra
            is_legal = not is_wide and not is_noball
            batter_runs = runs.get("batter", 0)
            total += runs.get("total", 0)
            over_runs += runs.get("total", 0)
            for key in extras:
                extras[key] += extra.get(key, 0)
            if not is_wide:
                batters[striker]["balls"] += 1
            batters[striker]["runs"] += batter_runs
            if batter_runs == 4 and not runs.get("non_boundary"):
                batters[striker]["fours"] += 1
                fours += 1
            elif batter_runs == 6 and not runs.get("non_boundary"):
                batters[striker]["sixes"] += 1
                sixes += 1
            charged = batter_runs + extra.get("wides", 0) + extra.get("noballs", 0)
            stats["conceded"] += charged
            over_bowlers[bowler_name] = over_bowlers.get(bowler_name, 0) + charged
            if is_legal:
                legal += 1
                over_legal += 1
                stats["balls"] += 1
                if batter_runs == 0:
                    stats["dots"] += 1
            partnership["runs"] += runs.get("total", 0)
            partnership["balls"] += 1 if is_legal else 0
            partnership["players"] = sorted({striker, partner} - {""})
            for wicket in delivery.get("wickets", []):
                kind = wicket.get("kind", "")
                out = wicket.get("player_out", striker)
                record = batter(out)
                record["dismissal"] = dismissal_text(wicket, bowler_name)
                if kind in NOT_TEAM_WICKETS:
                    continue
                record["isOut"] = True
                wickets += 1
                over_wickets += 1
                record["fowRuns"] = total
                if kind in BOWLER_WICKETS:
                    stats["wickets"] += 1
                fall.append({"wicket": wickets, "runs": total, "over": overs_text(legal), "player": out})
                partnerships.append({**partnership, "wicket": wickets})
                partnership = {"runs": 0, "balls": 0, "players": []}
        # A maiden is a complete over by one bowler with nothing charged to them.
        if over_legal == 6 and len(over_bowlers) == 1:
            (only, charged), = over_bowlers.items()
            if charged == 0:
                bowlers[only]["maidens"] += 1
        remaining = max(ball_limit - legal, 0)
        record = {
            "overNumber": int(over.get("over", 0)) + 1,
            "overRuns": over_runs, "overWickets": over_wickets,
            "totalRuns": total, "totalWickets": wickets,
            "overRunRate": round(total / (legal / 6), 2) if legal else 0.0,
            "remainingBalls": remaining, "predictions": {},
        }
        if target:
            record["requiredRuns"] = max(target - total, 0)
            record["requiredRunRate"] = round(record["requiredRuns"] / (remaining / 6), 2) if remaining else None
        overs_out.append(record)
    if partnership["balls"] or partnership["runs"]:
        partnerships.append({**partnership, "wicket": None, "unbroken": True})

    team = inning.get("team", "")
    return {
        "team": {"name": team, "longName": team, "abbreviation": team_code(team)},
        "runs": total, "wickets": wickets, "overs": overs_text(legal), "balls": legal,
        "extras": sum(extras.values()), "extrasBreakdown": extras,
        "fours": fours, "sixes": sixes, "target": target,
        "inningBatsmen": [
            {
                "battedType": "yes", "runs": row["runs"], "balls": row["balls"], "fours": row["fours"], "sixes": row["sixes"],
                "strikerate": round(100 * row["runs"] / row["balls"], 2) if row["balls"] else 0.0,
                "isOut": row["isOut"], "fowRuns": row["fowRuns"],
                "dismissalText": {"long": row["dismissal"] or ("not out" if not row["isOut"] else "")},
                "player": _person(name),
            }
            for name, row in ((name, batters[name]) for name in order)
        ],
        "inningBowlers": [
            {
                "overs": overs_text(row["balls"]), "balls": row["balls"], "maidens": row["maidens"],
                "conceded": row["conceded"], "wickets": row["wickets"], "dots": row["dots"],
                "economy": round(row["conceded"] / (row["balls"] / 6), 2) if row["balls"] else 0.0,
                "player": _person(name),
            }
            for name, row in sorted(bowlers.items(), key=lambda item: item[1]["order"])
        ],
        "inningOvers": overs_out,
        "inningFallOfWickets": fall,
        "inningWickets": [{"dismissalText": {"long": batters[item["player"]]["dismissal"]}, "player": _person(item["player"])} for item in fall],
        "inningPartnerships": partnerships,
    }


def result_text(info: dict[str, Any]) -> str:
    outcome = info.get("outcome", {})
    winner = outcome.get("winner")
    by = outcome.get("by", {})
    method = f" ({outcome['method']})" if outcome.get("method") else ""
    if winner and "runs" in by:
        return f"{team_code(winner)} won by {by['runs']} run{'s' if by['runs'] != 1 else ''}{method}"
    if winner and "wickets" in by:
        return f"{team_code(winner)} won by {by['wickets']} wicket{'s' if by['wickets'] != 1 else ''}{method}"
    if outcome.get("result") == "tie":
        eliminator = outcome.get("eliminator")
        return f"Match tied ({team_code(eliminator)} won the Super Over)" if eliminator else "Match tied"
    if outcome.get("result") == "no result":
        return "No result"
    if winner:
        return f"{team_code(winner)} won{method}"
    return "Result unavailable"


def toss_text(info: dict[str, Any]) -> str:
    toss = info.get("toss", {})
    if not toss.get("winner"):
        return "Unavailable"
    decision = {"field": "field", "bat": "bat"}.get(toss.get("decision", ""), toss.get("decision", ""))
    return f"{team_code(toss['winner'])} won the toss and chose to {decision}"


def derive_scorecard(payload: dict[str, Any]) -> dict[str, Any]:
    """ESPN-shaped scorecard for a whole Cricsheet match (super overs excluded)."""
    info = payload.get("info", {})
    ball_limit = 6 * int(info.get("overs") or 20)
    innings = []
    for inning in payload.get("innings", []):
        if inning.get("super_over"):
            continue
        target_runs = (inning.get("target") or {}).get("runs")
        if target_runs is None and innings:
            target_runs = innings[0]["runs"] + 1
        innings.append(derive_innings(inning, target=target_runs if innings else None, ball_limit=ball_limit))
    return {
        "match": {"statusText": result_text(info), "ground": {"name": info.get("venue", "")}},
        "content": {
            "innings": innings,
            "supportInfo": {"playersOfTheMatch": [{"player": _person(name)} for name in info.get("player_of_match", [])]},
        },
    }
