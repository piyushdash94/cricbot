"""FastAPI application exposing the Cricbot formatting and analytics engine."""

import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.apis.demo_data import dashboard_shell, demo_scorecard
from backend.apis.docs_catalog import docs_catalog
from backend.apis.schemas import (
    AgentChatRequest,
    MatchPreviewRequest,
    PlayerCardRequest,
    ScorecardOnlyRequest,
    ScorecardRequest,
    ViewPayload,
)
from backend.agent.service import CricbotAgent
from backend.agent.tools import run_search
from backend.src.cricbot_views import (
    fetch_match_bundle,
    format_live_matches,
    format_match_preview,
    format_player_card,
    format_standings,
)
from backend.src.impact_lab import (
    impact_leaderboard,
    match_par,
    momentum_series,
    pitch_profile,
    turning_points,
)
from backend.src.scorecard_helpers import innings_team_name, over_series
from backend.src.scorecard_renderer import build_compact_scorecard
from backend.src.ipl_data import IPL_SERIES, get_match_detail, list_ipl_matches


app = FastAPI(
    title="Cricbot API",
    description="Chat-ready scorecards and match analytics from ESPNcricinfo-shaped payloads.",
    version="1.2.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = CricbotAgent()


def _innings(scorecard: dict[str, Any]) -> list[dict[str, Any]]:
    return scorecard.get("content", {}).get("innings", []) or []


def _analytics(scorecard: dict[str, Any]) -> dict[str, Any]:
    innings = _innings(scorecard)
    par = match_par(innings)
    return {
        "par": par,
        "impact": impact_leaderboard(innings, par)[:10] if par else [],
        "turning_points": turning_points(innings, top_n=5),
        "momentum": [
            {"team": innings_team_name(inn), "values": [round(value, 2) for value in momentum_series(inn)]}
            for inn in innings
        ],
        "pitch": pitch_profile(innings),
    }


@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok", "service": "cricbot-api", "version": app.version}


@app.get("/api/dashboard", tags=["dashboard"])
def dashboard():
    scorecard = demo_scorecard()
    innings = _innings(scorecard)
    payload = dashboard_shell()
    payload.update(
        {
            "featured_match": {
                "title": "RCB vs PBKS",
                "result": scorecard["match"]["statusText"],
                "venue": scorecard["match"]["ground"]["name"],
                "innings": [
                    {
                        "team": innings_team_name(inn),
                        "runs": inn["runs"],
                        "wickets": inn["wickets"],
                        "overs": inn["overs"],
                        "over_runs": [row["runs"] for row in over_series(inn)],
                    }
                    for inn in innings
                ],
            },
            "analytics": _analytics(scorecard),
            "summary": build_compact_scorecard(
                {}, scorecard, phases=True, charts=True, partnerships=True, fow=False
            ),
        }
    )
    return payload


@app.get("/api/search", tags=["search"])
def search(q: str, limit: int = 12):
    query = q.strip()
    if len(query) < 2:
        return {"query": query, "results": [], "sources": []}
    results, sources = run_search(query, limit=max(1, min(limit, 30)))
    return {"query": query, "results": results, "sources": sources}


@app.get("/api/ipl/seasons", tags=["ipl archive"])
def ipl_seasons():
    return {
        "seasons": [
            {"year": year, **metadata}
            for year, metadata in IPL_SERIES.items()
        ]
    }


@app.get("/api/ipl/matches", tags=["ipl archive"])
def ipl_matches(season: int = 2025, q: str = "", limit: int = 24, offset: int = 0):
    try:
        return list_ipl_matches(season=season, query=q, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ipl/matches/{series_slug}/{match_slug}", tags=["ipl archive"])
def ipl_match_detail(series_slug: str, match_slug: str):
    return get_match_detail(series_slug, match_slug)


@app.get("/api/docs/catalog", tags=["documentation"])
def documentation_catalog():
    return docs_catalog()


@app.get("/api/agent/status", tags=["agent"])
def agent_status():
    return agent.status()


@app.get("/api/agent/graph", tags=["agent"])
def agent_graph():
    return agent.graph_definition()


@app.post("/api/agent/chat", tags=["agent"])
def agent_chat(request: AgentChatRequest):
    return agent.answer(
        request.message,
        history=[item.model_dump() for item in request.history],
        ui_context=request.ui_context,
    )


@app.post("/api/agent/chat/stream", tags=["agent"])
def agent_chat_stream(request: AgentChatRequest):
    def events():
        for event in agent.iter_answer(
            request.message,
            history=[item.model_dump() for item in request.history],
            ui_context=request.ui_context,
        ):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/scorecard/render", tags=["scorecard"])
def render_scorecard(request: ScorecardRequest):
    options = request.options.model_dump()
    text = build_compact_scorecard(request.info, request.scorecard, **options)
    return {
        "text": text,
        "characters": len(text),
    }


@app.post("/api/analytics", tags=["analytics"])
def analytics(request: ScorecardOnlyRequest):
    return _analytics(request.scorecard)


@app.post("/api/views/live", tags=["views"])
def live_view(request: ViewPayload):
    limit = request.limit or 10
    return {"text": format_live_matches(request.payload, limit=limit)}


@app.post("/api/views/standings", tags=["views"])
def standings_view(request: ViewPayload):
    limit = request.limit or 12
    return {"text": format_standings(request.payload, limit=limit)}


@app.post("/api/views/player", tags=["views"])
def player_view(request: PlayerCardRequest):
    return {"text": format_player_card(request.bio, request.career, request.format)}


@app.post("/api/views/preview", tags=["views"])
def preview_view(request: MatchPreviewRequest):
    return {"text": format_match_preview(request.info, request.ground)}


def _client():
    try:
        from cricdata import CricinfoClient
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="cricdata is not installed") from exc
    return CricinfoClient()


@app.get("/api/live", tags=["provider"])
def live_matches():
    try:
        matches = _client().live_matches()
        return {"matches": matches, "formatted": format_live_matches(matches)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream provider failed: {exc}") from exc


@app.get("/api/matches/{series_slug}/{match_slug}", tags=["provider"])
def match_bundle(series_slug: str, match_slug: str):
    try:
        bundle = fetch_match_bundle(_client(), series_slug, match_slug)
        bundle["summary"] = build_compact_scorecard(bundle["info"], bundle["scorecard"])
        return bundle
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream provider failed: {exc}") from exc
