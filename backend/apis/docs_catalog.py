"""Machine-readable documentation rendered by the in-app Docs drawer."""

from backend.agent.service import GRAPH_EDGES, GRAPH_NODES


def docs_catalog():
    return {
        "title": "Cricbot platform contract",
        "version": "1.2.0",
        "principles": [
            "The browser only consumes Cricbot's canonical JSON; provider-specific shapes stay in backend/src.",
            "Historical coverage is honest: Cricsheet deliveries → ESPN play-by-play → Cricinfo commentary → over summaries → unavailable.",
            "Pandit retrieves deterministic facts first; the LLM (Groq, then OpenRouter) only summarises them, and any answer with a number absent from the facts is replaced by a grounded recap.",
            "Cricsheet is the canonical archive: the scorecard is derived from the same deliveries as the ball-by-ball, ESPN adds commentary, and results are cross-checked against both.",
            "Momentum shifts are detected from deliveries and commentary (collapses, bursts, squeezes, chase swings, missed chances) before the LLM explains them.",
            "Team typos and relative dates are resolved before retrieval; the resolved entities remain visible in the graph trace.",
        ],
        "sources": [
            {"name": "cricdata / ESPNcricinfo", "use": "IPL schedules, results, match info, scorecards, overs, partnerships and commentary", "mode": "live upstream, cached in process"},
            {"name": "Cricsheet", "use": "Every delivery of every IPL match (structured, no commentary prose); ODC-BY licensed", "mode": "offline after `python -m backend.src.cricsheet sync`"},
            {"name": "ESPN play-by-play", "use": "Full deliveries with commentary text via cricdata.match_ball_by_ball", "mode": "best effort, paginated"},
            {"name": "Local demo payload", "use": "Stable scorecard and sample delivery timeline when the provider is unavailable", "mode": "automatic fallback, explicitly labelled"},
            {"name": "Groq / OpenRouter LLMs", "use": "Summarise grounded facts into conversational answers; Groq first, OpenRouter as fallback, local Gemma last", "mode": "hosted chat/completions; keys in backend/.env; every number is validated against the facts"},
        ],
        "apis": [
            {"method": "GET", "path": "/api/ipl/seasons", "use": "List supported archive seasons", "response": "{ seasons: [{ year, name, slug }] }"},
            {"method": "GET", "path": "/api/ipl/matches", "use": "Search and page IPL matches", "request": "season, q, limit, offset", "response": "{ matches, pagination, source }"},
            {"method": "GET", "path": "/api/ipl/matches/{series_slug}/{match_slug}", "use": "Canonical match detail", "response": "{ match, innings, balls (with commentary), ball_coverage, overs, partnerships, fall_of_wickets, analytics (incl. shifts), consistency, sources }"},
            {"method": "GET", "path": "/api/data/status", "use": "Which source is canonical, and how many Cricsheet matches and seasons are loaded", "response": "{ cricsheet, canonical_source }"},
            {"method": "GET", "path": "/api/search", "use": "Natural-language global search", "request": "q, limit", "response": "{ query, results, sources }"},
            {"method": "POST", "path": "/api/agent/chat/stream", "use": "Pandit chat plus live graph trace", "request": "{ message, history, ui_context }", "response": "NDJSON trace events, then one result event"},
            {"method": "GET", "path": "/api/agent/graph", "use": "Graph metadata for the visual debugger", "response": "{ nodes, edges, state_fields }"},
            {"method": "GET", "path": "/api/docs/catalog", "use": "This documentation", "response": "Machine-readable platform catalog"},
        ],
        "tools": [
            {"name": "entity resolver", "owner": "extract_entities", "use": "Resolve team typos, players, years, match stages, venues, conversational carry-over, and list intent"},
            {"name": "search_matches", "owner": "retrieve_facts", "use": "Search the IPL archive by team, season, venue, status or result"},
            {"name": "search_players", "owner": "retrieve_facts", "use": "Find batting and bowling performances in loaded scorecards"},
            {"name": "search_standings", "owner": "retrieve_facts", "use": "Read points-table facts"},
            {"name": "search_analytics", "owner": "retrieve_facts", "use": "Read impact, momentum, turning-point and pitch facts"},
            {"name": "match loader", "owner": "load_match", "use": "Fetch scorecard, deliveries, commentary, and momentum shifts for the resolved match; build the facts sheet and chat card"},
            {"name": "LLM router", "owner": "llm_response", "use": "Summarise the facts sheet with Groq, falling back to OpenRouter, then local Gemma"},
            {"name": "UI action planner", "owner": "plan_ui", "use": "Open matches, switch detail tabs, navigate, theme or copy summary"},
        ],
        "states": [
            {"name": "message/history/ui_context", "meaning": "Raw request plus the currently visible season, match and tab"},
            {"name": "query/compact_history", "meaning": "Normalized bounded input"},
            {"name": "entities/retrieval_query", "meaning": "Resolved teams, players, seasons, terms, corrections, intent, and the canonical query used by tools"},
            {"name": "tool_names/results", "meaning": "Selected deterministic tools and their grounded facts"},
            {"name": "compact_results", "meaning": "Bounded facts sheet passed to the LLM: scorecard, overs, wickets, key deliveries, momentum shifts"},
            {"name": "actions/response_route", "meaning": "Safe UI changes and exact-vs-model branch"},
            {"name": "candidate/reply/validation", "meaning": "Draft answer, final answer, and grounding verdict"},
        ],
        "nodes": GRAPH_NODES,
        "transitions": GRAPH_EDGES,
        "coverage": [
            {"level": "cricsheet", "meaning": "Every delivery from the local Cricsheet archive; descriptions generated from structured data"},
            {"level": "full", "meaning": "ESPN play-by-play returned every delivery with commentary text"},
            {"level": "commentary", "meaning": "Deliveries reconstructed from historical commentary; may be partial"},
            {"level": "overs-only", "meaning": "Only over aggregates are available"},
            {"level": "demo", "meaning": "Clearly labelled local sample while the provider is offline"},
            {"level": "unavailable", "meaning": "No delivery-level data; scorecard remains usable"},
        ],
    }
