"""Grounded cricket search tools shared by global search and the chat agent."""

from typing import Any

from langchain_core.tools import tool

from backend.apis.demo_data import dashboard_shell, demo_scorecard
from backend.src.impact_lab import impact_leaderboard, match_par, pitch_profile, turning_points
from backend.src.scorecard_helpers import innings_team_name


def _contains(query: str, *values: Any) -> bool:
    haystack = query.casefold()
    return any(str(value).casefold() in haystack or haystack in str(value).casefold() for value in values if value)


@tool("search_matches", description="Search matches by team, status, result, or venue.")
def search_matches(query: str) -> list[dict[str, Any]]:
    results = []
    for match in dashboard_shell()["matches"]:
        teams = match["teams"]
        searchable = [*teams, match["status"], match["result"], match["venue"], "match", "score", "live"]
        if not query.strip() or _contains(query, *searchable):
            results.append({
                "id": f"match:{match['id']}",
                "type": "match",
                "title": f"{teams[0]} vs {teams[1]}",
                "subtitle": match["result"],
                "meta": f"{match['score'][0]} · {match['score'][1]} · {match['venue']}",
                "action": {"type": "select_match", "match_id": match["id"]},
            })
    return results


@tool("search_players", description="Search player performances and batting or bowling scorecards.")
def search_players(query: str) -> list[dict[str, Any]]:
    results = []
    for innings in demo_scorecard()["content"]["innings"]:
        team = innings_team_name(innings)
        for batter in innings.get("inningBatsmen", []):
            player = batter.get("player") or {}
            name = player.get("longName") or player.get("name") or "Unknown"
            if not query.strip() or _contains(query, name, team, "batter", "batting", "runs", "score"):
                results.append({
                    "id": f"player:bat:{name}", "type": "player", "title": name,
                    "subtitle": f"{team} · {batter.get('runs', 0)} ({batter.get('balls', 0)})",
                    "meta": f"SR {batter.get('strikerate', '-')} · {batter.get('fours', 0)} fours · {batter.get('sixes', 0)} sixes",
                    "action": {"type": "set_view", "view": "scorecard"},
                })
        for bowler in innings.get("inningBowlers", []):
            player = bowler.get("player") or {}
            name = player.get("longName") or player.get("name") or "Unknown"
            if not query.strip() or _contains(query, name, "bowler", "bowling", "wickets", "economy"):
                results.append({
                    "id": f"player:bowl:{name}", "type": "player", "title": name,
                    "subtitle": f"Bowling · {bowler.get('wickets', 0)}/{bowler.get('conceded', '-')} in {bowler.get('overs', '-')} overs",
                    "meta": f"Economy {bowler.get('economy', '-')} · {bowler.get('dots', 0)} dots",
                    "action": {"type": "navigate", "section": "analytics"},
                })
    return results


@tool("search_standings", description="Search the IPL points table and team standings.")
def search_standings(query: str) -> list[dict[str, Any]]:
    results = []
    for row in dashboard_shell()["standings"]:
        if not query.strip() or _contains(query, row["team"], "standings", "table", "points", "rank", "nrr"):
            results.append({
                "id": f"standing:{row['team']}", "type": "standing",
                "title": f"#{row['rank']} {row['team']}",
                "subtitle": f"{row['points']} points · {row['won']} wins from {row['played']}",
                "meta": f"NRR {row['nrr']}",
                "action": {"type": "navigate", "section": "players"},
            })
    return results


@tool("search_analytics", description="Search player impact, turning points, momentum, and pitch analysis.")
def search_analytics(query: str) -> list[dict[str, Any]]:
    innings = demo_scorecard()["content"]["innings"]
    par = match_par(innings)
    results = []
    if par and (not query.strip() or _contains(query, "impact", "best", "top", "player", "performance")):
        for row in impact_leaderboard(innings, par)[:5]:
            results.append({
                "id": f"impact:{row['role']}:{row['player']}", "type": "analytics",
                "title": f"{row['player']} · {row['impact']:+.1f}",
                "subtitle": f"{row['role'].title()} impact in runs above par",
                "meta": "Impact leaderboard",
                "action": {"type": "navigate", "section": "analytics"},
            })
    if not query.strip() or _contains(query, "pitch", "conditions", "spin", "pace", "surface", "venue"):
        profile = pitch_profile(innings)
        results.append({
            "id": "analytics:pitch", "type": "analytics", "title": "Pitch read",
            "subtitle": f"Pace econ {profile['pace']['econ']} · Spin econ {profile['spin']['econ']}",
            "meta": f"Second-innings change {profile['deterioration']:+.2f} RPO",
            "action": {"type": "navigate", "section": "analytics"},
        })
    if not query.strip() or _contains(query, "turning", "swing", "momentum", "probability", "win"):
        point = turning_points(innings, top_n=1)[0]
        results.append({
            "id": "analytics:momentum", "type": "analytics", "title": "Biggest momentum swing",
            "subtitle": f"{point['team']} over {point['over']} · {point['swing']:+.1f} percentage points",
            "meta": f"{point['runs']} runs · {point['wickets']} wickets",
            "action": {"type": "set_view", "view": "momentum"},
        })
    return results


SEARCH_TOOLS = {
    tool.name: tool for tool in (
        search_matches,
        search_players,
        search_standings,
        search_analytics,
    )
}


def select_tools(query: str) -> list[str]:
    """Route natural-language search to one or more grounded tools."""
    text = query.casefold()
    selected = []
    if any(token in text for token in ("match", "live", "score", "vs", "rcb", "pbks", "mi", "gt", "kkr", "srh")):
        selected.append("search_matches")
    if any(token in text for token in ("player", "batter", "bowler", "runs", "wicket", "kohli", "salt", "pandya", "iyer", "hazlewood")):
        selected.append("search_players")
    if any(token in text for token in ("standing", "table", "rank", "points", "nrr", "qualify")):
        selected.append("search_standings")
    if any(token in text for token in ("impact", "momentum", "pitch", "spin", "pace", "probability", "swing", "best", "analysis")):
        selected.append("search_analytics")
    return selected or list(SEARCH_TOOLS)


def run_search(query: str, limit: int = 12) -> tuple[list[dict[str, Any]], list[str]]:
    selected = select_tools(query)
    results = []
    seen = set()
    for name in selected:
        for item in SEARCH_TOOLS[name].invoke({"query": query}):
            if item["id"] not in seen:
                seen.add(item["id"])
                results.append(item)
    return results[:limit], selected

