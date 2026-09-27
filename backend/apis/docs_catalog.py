"""Machine-readable documentation rendered by the in-app Docs drawer."""

from backend.agent.service import GRAPH_EDGES, GRAPH_NODES


def docs_catalog():
    return {
        "title": "Cricbot platform contract",
        "version": "1.2.0",
        "principles": [
            "The browser only consumes Cricbot's canonical JSON; provider-specific shapes stay in backend/src.",
            "Historical coverage is honest: full ball feed → commentary → over summaries → unavailable.",
            "Pandit retrieves deterministic facts first and uses local Gemma only to phrase grounded answers.",
        ],
        "sources": [
            {"name": "cricdata / ESPNcricinfo", "use": "IPL schedules, results, match info, scorecards, overs, partnerships and commentary", "mode": "live upstream, cached in process"},
            {"name": "Dedicated ball feed", "use": "Ball-level events when the upstream endpoint is available", "mode": "best effort"},
            {"name": "Local demo payload", "use": "Stable scorecard and sample delivery timeline when the provider is unavailable", "mode": "automatic fallback, explicitly labelled"},
            {"name": "Gemma 4 E4B", "use": "Friendly natural-language synthesis after deterministic retrieval", "mode": "local only at 127.0.0.1:8080/v1/completions"},
        ],
        "apis": [
            {"method": "GET", "path": "/api/ipl/seasons", "use": "List supported archive seasons", "response": "{ seasons: [{ year, name, slug }] }"},
            {"method": "GET", "path": "/api/ipl/matches", "use": "Search and page IPL matches", "request": "season, q, limit, offset", "response": "{ matches, pagination, source }"},
            {"method": "GET", "path": "/api/ipl/matches/{series_slug}/{match_slug}", "use": "Canonical match detail", "response": "{ match, innings, balls, ball_coverage, analytics, sources }"},
            {"method": "GET", "path": "/api/search", "use": "Natural-language global search", "request": "q, limit", "response": "{ query, results, sources }"},
            {"method": "POST", "path": "/api/agent/chat/stream", "use": "Pandit chat plus live graph trace", "request": "{ message, history, ui_context }", "response": "NDJSON trace events, then one result event"},
            {"method": "GET", "path": "/api/agent/graph", "use": "Graph metadata for the visual debugger", "response": "{ nodes, edges, state_fields }"},
            {"method": "GET", "path": "/api/docs/catalog", "use": "This documentation", "response": "Machine-readable platform catalog"},
        ],
        "tools": [
            {"name": "search_matches", "owner": "retrieve_facts", "use": "Search the IPL archive by team, season, venue, status or result"},
            {"name": "search_players", "owner": "retrieve_facts", "use": "Find batting and bowling performances in loaded scorecards"},
            {"name": "search_standings", "owner": "retrieve_facts", "use": "Read points-table facts"},
            {"name": "search_analytics", "owner": "retrieve_facts", "use": "Read impact, momentum, turning-point and pitch facts"},
            {"name": "Gemma completion", "owner": "gemma_response", "use": "Phrase retrieved evidence as a concise, friendly answer"},
            {"name": "UI action planner", "owner": "plan_ui", "use": "Open matches, switch detail tabs, navigate, theme or copy summary"},
        ],
        "states": [
            {"name": "message/history/ui_context", "meaning": "Raw request plus the currently visible season, match and tab"},
            {"name": "query/compact_history", "meaning": "Normalized bounded input"},
            {"name": "tool_names/results", "meaning": "Selected deterministic tools and their grounded facts"},
            {"name": "compact_results", "meaning": "Small evidence packet passed to Gemma"},
            {"name": "actions/response_route", "meaning": "Safe UI changes and exact-vs-model branch"},
            {"name": "candidate/reply/validation", "meaning": "Draft answer, final answer, and grounding verdict"},
        ],
        "nodes": GRAPH_NODES,
        "transitions": GRAPH_EDGES,
        "coverage": [
            {"level": "full", "meaning": "Dedicated ball endpoint returned normalized deliveries"},
            {"level": "commentary", "meaning": "Deliveries reconstructed from historical commentary; may be partial"},
            {"level": "overs-only", "meaning": "Only over aggregates are available"},
            {"level": "demo", "meaning": "Clearly labelled local sample while the provider is offline"},
            {"level": "unavailable", "meaning": "No delivery-level data; scorecard remains usable"},
        ],
    }
