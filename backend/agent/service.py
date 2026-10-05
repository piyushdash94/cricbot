"""LangGraph orchestration for grounded cricket chat and observable UI control."""

import json
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .gemma import GemmaCompletionClient
from .entities import resolve_entities
from .llm import GemmaProvider, LLMRouter
from .match_facts import build_match_context, deterministic_summary, match_list_card, resolve_match
from .tools import run_search, select_tools


SYSTEM_PROMPT = """You are Pandit, the cricket analyst inside Cricbot. You talk like a sharp, friendly commentator.
Rules:
- Answer the user's question using ONLY the FACTS provided. Never invent scores, names, or statistics.
- Lead with the direct answer, then the one or two details that explain it. Summarise; do not list the whole scorecard.
- Copy numbers exactly as they appear in the facts. Do not calculate new figures.
- For momentum, turning-point, or "where was it won/lost" questions, use the MOMENTUM SHIFTS lines: say who seized control, in which overs, how (collapse, burst, squeeze, chase pressure, missed chance), and the deliveries or commentary that show it. Lead with the largest shift.
- If the facts do not contain the answer, say what is missing in one sentence.
- At most five sentences. Plain text, no markdown tables or headings."""




class PanditState(TypedDict, total=False):
    """State passed between the observable Pandit graph nodes."""

    message: str
    history: list[dict[str, str]]
    ui_context: dict[str, Any]
    query: str
    retrieval_query: str
    entities: dict[str, Any]
    compact_history: str
    tool_names: list[str]
    results: list[dict[str, Any]]
    compact_results: str
    actions: list[dict[str, Any]]
    response_route: str
    candidate: str
    reply: str
    model_used: bool
    validation: str
    intermediate_message: str
    tool_calls: list[dict[str, Any]]
    match_target: dict[str, str] | None
    match_facts: str
    cards: list[dict[str, Any]]
    provider: str
    model: str


GRAPH_NODES = [
    {"id": "normalize_request", "label": "Understand request", "kind": "input", "description": "Normalize the question, dashboard context, and recent conversation."},
    {"id": "extract_entities", "label": "Resolve cricket entities", "kind": "entity", "description": "Correct team typos and resolve teams, players, years, match terms, venues, and list intent."},
    {"id": "route_tools", "label": "Select tools", "kind": "routing", "description": "Choose match, player, standings, or analytics tools from the resolved retrieval query."},
    {"id": "retrieve_facts", "label": "Retrieve cricket facts", "kind": "tool", "description": "Search the match archive for the resolved teams, seasons, and stages."},
    {"id": "load_match", "label": "Load match data", "kind": "tool", "description": "Fetch scorecard, ball-by-ball, and commentary for the match the question is about."},
    {"id": "summarize_context", "label": "Summarize evidence", "kind": "summary", "description": "Compress tool results and chat history into bounded model context."},
    {"id": "plan_ui", "label": "Plan UI actions", "kind": "action", "description": "Choose dashboard updates and the exact or model response branch."},
    {"id": "exact_response", "label": "Exact tool answer", "kind": "response", "description": "Answer analytics and control requests directly from tool output."},
    {"id": "llm_response", "label": "LLM synthesis", "kind": "model", "description": "Summarise the grounded facts with Groq or OpenRouter (local Gemma as last resort)."},
    {"id": "validate_response", "label": "Grounding check", "kind": "validation", "description": "Reject unsupported numbers, repetition, and ungrounded model output."},
    {"id": "finalize", "label": "Finalize response", "kind": "output", "description": "Package the answer, provenance, trace, and safe UI actions."},
]

GRAPH_EDGES = [
    {"from": "start", "to": "normalize_request"},
    {"from": "normalize_request", "to": "extract_entities"},
    {"from": "extract_entities", "to": "route_tools"},
    {"from": "route_tools", "to": "retrieve_facts"},
    {"from": "retrieve_facts", "to": "load_match"},
    {"from": "load_match", "to": "summarize_context"},
    {"from": "summarize_context", "to": "plan_ui"},
    {"from": "plan_ui", "to": "exact_response", "condition": "exhaustive list, exact analytics, or UI command"},
    {"from": "plan_ui", "to": "llm_response", "condition": "conversational synthesis"},
    {"from": "llm_response", "to": "validate_response"},
    {"from": "exact_response", "to": "finalize"},
    {"from": "validate_response", "to": "finalize"},
    {"from": "finalize", "to": "end"},
]


class CricbotAgent:
    """Stateful LangGraph agent with deterministic tools and Gemma synthesis."""

    def __init__(self, gemma: GemmaCompletionClient | None = None, llm: LLMRouter | None = None):
        # An explicit Gemma client (tests, local-only setups) pins the router to it.
        self.llm = llm or (LLMRouter([GemmaProvider(gemma)]) if gemma else LLMRouter())
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(PanditState)
        workflow.add_node("normalize_request", self._normalize_request)
        workflow.add_node("extract_entities", self._extract_entities)
        workflow.add_node("route_tools", self._route_tools)
        workflow.add_node("retrieve_facts", self._retrieve_facts)
        workflow.add_node("load_match", self._load_match)
        workflow.add_node("summarize_context", self._summarize_context)
        workflow.add_node("plan_ui", self._plan_ui)
        workflow.add_node("exact_response", self._exact_response)
        workflow.add_node("llm_response", self._llm_response)
        workflow.add_node("validate_response", self._validate_response)
        workflow.add_node("finalize", self._finalize)
        workflow.add_edge(START, "normalize_request")
        workflow.add_edge("normalize_request", "extract_entities")
        workflow.add_edge("extract_entities", "route_tools")
        workflow.add_edge("route_tools", "retrieve_facts")
        workflow.add_edge("retrieve_facts", "load_match")
        workflow.add_edge("load_match", "summarize_context")
        workflow.add_edge("summarize_context", "plan_ui")
        workflow.add_conditional_edges(
            "plan_ui",
            lambda state: state["response_route"],
            {"exact": "exact_response", "model": "llm_response"},
        )
        workflow.add_edge("exact_response", "finalize")
        workflow.add_edge("llm_response", "validate_response")
        workflow.add_edge("validate_response", "finalize")
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def status(self) -> dict[str, Any]:
        providers = self.llm.status()
        active = next((provider for provider in providers if provider["ready"]), None)
        return {
            "status": "ready" if active else "offline",
            "provider": active["name"] if active else None,
            "model": active["model"] if active else None,
            "providers": providers,
            "contract": "chat/completions",
            "orchestrator": "langgraph",
            "graph_version": "2.0",
        }

    @staticmethod
    def graph_definition() -> dict[str, Any]:
        return {
            "name": "Pandit Cricket Assistant",
            "version": "2.0",
            "framework": "LangGraph",
            "nodes": GRAPH_NODES,
            "edges": GRAPH_EDGES,
            "state_fields": list(PanditState.__annotations__),
        }

    def answer(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        final = None
        for event in self.iter_answer(message, history=history, ui_context=ui_context):
            if event["event"] == "result":
                final = event["data"]
        if final is None:
            raise RuntimeError("Pandit graph completed without a result")
        return final

    def iter_answer(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield node-level trace events followed by the final response."""
        current: PanditState = {
            "message": message,
            "history": history or [],
            "ui_context": ui_context or {},
        }
        trace: list[dict[str, Any]] = []
        for update in self.graph.stream(current, stream_mode="updates"):
            for node, patch in update.items():
                current.update(patch)
                trace_event = self._trace_event(len(trace) + 1, node, patch, current)
                trace.append(trace_event)
                yield {"event": "trace", "data": trace_event}

        response = {
            "reply": current.get("reply") or self._fallback_reply(message, current.get("results", []), current.get("entities")),
            "tool_calls": current.get("tool_calls", []),
            "results": current.get("results", [])[:100 if current.get("entities", {}).get("wants_all") else 5],
            "ui_actions": current.get("actions", []),
            "provider": current.get("provider") if current.get("model_used") else None,
            "model": current.get("model") if current.get("model_used") else None,
            "model_used": current.get("model_used", False),
            "cards": current.get("cards", []),
            "validation": current.get("validation", "exact-tool-output"),
            "trace": trace,
            "graph_version": "2.0",
        }
        yield {"event": "result", "data": response}

    def _normalize_request(self, state: PanditState) -> PanditState:
        compact_history = "\n".join(
            f"{item.get('role', 'user').title()}: {item.get('content', '')[:500]}"
            for item in state.get("history", [])[-6:]
        ) or "No previous messages."
        return {
            "query": state["message"].strip(),
            "compact_history": compact_history,
            "intermediate_message": "Request normalized with the current dashboard and recent chat context.",
        }

    @staticmethod
    def _extract_entities(state: PanditState) -> PanditState:
        entities = resolve_entities(
            state["query"],
            history=state.get("history", []),
            ui_context=state.get("ui_context", {}),
        )
        labels = [
            *entities["teams"], *entities["players"],
            *(str(year) for year in entities["years"]), *entities["match_terms"], *entities["topics"],
        ]
        resolved = ", ".join(labels) or "no named cricket entity"
        correction_count = len(entities["corrections"])
        return {
            "entities": entities,
            "retrieval_query": entities["retrieval_query"],
            "intermediate_message": f"Resolved {resolved}; applied {correction_count} correction{'s' if correction_count != 1 else ''}; intent is {entities['intent']}.",
        }

    @staticmethod
    def _route_tools(state: PanditState) -> PanditState:
        tool_names = select_tools(state.get("retrieval_query", state["query"]))
        return {
            "tool_names": tool_names,
            "intermediate_message": f"Selected {len(tool_names)} grounded cricket tool{'s' if len(tool_names) != 1 else ''}.",
        }

    @staticmethod
    def _retrieve_facts(state: PanditState) -> PanditState:
        entities = state.get("entities", {})
        limit = 100 if entities.get("wants_all") else 10
        # Topics ("momentum", "ball-by-ball") describe the answer, not the match.
        identifying = [*entities.get("teams", []), *(str(year) for year in entities.get("years", [])), *entities.get("match_terms", []), *entities.get("venues", [])]
        if not identifying:
            return {"results": [], "intermediate_message": "No team, season, stage, or venue named; using the match on screen if any."}
        results, _ = run_search(" ".join(identifying), limit=limit)
        # Only archive matches are real data; dashboard player/analytics/standings
        # rows are demo samples and must never ground an answer.
        results = [row for row in results if row.get("type") == "match" and (row.get("action") or {}).get("type") == "open_match"]
        return {
            "results": results,
            "intermediate_message": f"Found {len(results)} archive match{'es' if len(results) != 1 else ''}.",
        }

    @staticmethod
    def _load_match(state: PanditState) -> PanditState:
        entities = state.get("entities", {})
        results = state.get("results", [])
        target = resolve_match(entities, results, state.get("ui_context", {}))
        if target:
            built = build_match_context(target, state["query"])
            if built:
                facts, card, row = built
                others = [item for item in results if item.get("id") != row["id"]]
                return {
                    "match_target": target, "match_facts": facts, "cards": [card], "results": [row, *others],
                    "intermediate_message": f"Loaded {card['match']['title']} ({card['ball_count']} deliveries, {card['commentary_count']} with commentary).",
                }
        teams = " and ".join(entities.get("teams") or []) or "matching"
        seasons = ", ".join(str(year) for year in entities.get("years") or [])
        card = match_list_card(results, f"{teams} matches{f' in IPL {seasons}' if seasons else ''}")
        return {
            "match_target": None, "match_facts": "", "cards": [card] if card else [],
            "intermediate_message": f"No single match resolved; listing {len(card['matches']) if card else 0} candidates.",
        }

    @staticmethod
    def _summarize_context(state: PanditState) -> PanditState:
        if state.get("match_facts"):
            return {
                "compact_results": state["match_facts"],
                "intermediate_message": "Built a facts sheet from the scorecard, overs, wickets, and commentary.",
            }
        compact_results = "\n".join(
            f"- {row['title']}: {row['subtitle']} ({row['meta']})"
            for row in state.get("results", [])[:8]
        ) or "- No matching cricket data was found."
        return {
            "compact_results": compact_results,
            "intermediate_message": f"Compressed {len(state.get('results', []))} results into a bounded evidence summary.",
        }

    def _plan_ui(self, state: PanditState) -> PanditState:
        actions = self._ui_actions(state["query"], state.get("results", []), state.get("entities"), state.get("match_target"), state.get("ui_context", {}))
        response_route = "exact" if self._needs_exact_tool_answer(state["query"], state.get("entities")) else "model"
        return {
            "actions": actions,
            "response_route": response_route,
            "intermediate_message": f"Planned {len(actions)} UI action{'s' if len(actions) != 1 else ''}; routed to {response_route} response.",
        }

    def _exact_response(self, state: PanditState) -> PanditState:
        return {
            "reply": self._grounded_fallback(state),
            "model_used": False,
            "validation": "exact-tool-output",
            "intermediate_message": "Used deterministic tool output to preserve exact analytics.",
        }

    def _llm_response(self, state: PanditState) -> PanditState:
        history = [
            {"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("content", ""))[:600]}
            for item in state.get("history", [])[-6:]
        ]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": f"FACTS:\n{state['compact_results']}\n\nQUESTION: {state['query']}"},
        ]
        try:
            candidate, provider, model = self.llm.chat(messages)
        except RuntimeError as exc:
            return {
                "candidate": "", "model_used": False, "provider": "", "model": "",
                "intermediate_message": f"No LLM answered ({str(exc)[:160]}); preparing a grounded fallback.",
            }
        return {
            "candidate": candidate, "model_used": True, "provider": provider, "model": model,
            "intermediate_message": f"{provider} ({model}) summarised the facts.",
        }

    def _validate_response(self, state: PanditState) -> PanditState:
        candidate = state.get("candidate", "")
        grounded = bool(candidate) and self._numbers_are_grounded(
            candidate,
            state["compact_results"],
            state["query"],
        )
        reply = self._limit_sentences(candidate) if grounded else self._grounded_fallback(state)
        return {
            "reply": reply,
            "model_used": grounded,
            "validation": "grounded" if grounded else "fallback-after-validation",
            "intermediate_message": "Candidate passed numeric grounding checks." if grounded else "Candidate was replaced with grounded tool output.",
        }

    def _grounded_fallback(self, state: PanditState) -> str:
        cards = state.get("cards") or []
        if cards and cards[0]["type"] == "match" and state.get("entities", {}).get("intent") != "list_matches":
            return deterministic_summary(cards[0])
        return self._fallback_reply(state["query"], state.get("results", []), state.get("entities"))

    def _finalize(self, state: PanditState) -> PanditState:
        results = state.get("results", [])
        tool_calls = [
            {"name": name, "result_count": sum(1 for row in results if self._belongs(row, name))}
            for name in state.get("tool_names", [])
        ]
        return {
            "tool_calls": tool_calls,
            "intermediate_message": "Final reply, provenance, and dashboard actions packaged for the UI.",
        }

    @classmethod
    def _trace_event(
        cls,
        sequence: int,
        node: str,
        patch: PanditState,
        state: PanditState,
    ) -> dict[str, Any]:
        definition = next(item for item in GRAPH_NODES if item["id"] == node)
        return {
            "sequence": sequence,
            "node": node,
            "label": definition["label"],
            "kind": definition["kind"],
            "summary": patch.get("intermediate_message", definition["description"]),
            "patch": cls._visible_patch(node, patch),
            "snapshot": {
                "phase": node,
                "route": state.get("response_route", "pending"),
                "tools": state.get("tool_names", []),
                "result_count": len(state.get("results", [])),
                "action_count": len(state.get("actions", [])),
                "model_used": state.get("model_used", False),
                "validation": state.get("validation", "pending"),
            },
        }

    @staticmethod
    def _visible_patch(node: str, patch: PanditState) -> dict[str, Any]:
        if node == "normalize_request":
            return {
                "query": patch.get("query"),
                "history_summary": patch.get("compact_history"),
            }
        if node == "extract_entities":
            return {
                "entities": patch.get("entities", {}),
                "retrieval_query": patch.get("retrieval_query", ""),
            }
        if node == "route_tools":
            return {"tool_names": patch.get("tool_names", [])}
        if node == "retrieve_facts":
            results = patch.get("results", [])
            return {
                "result_count": len(results),
                "result_types": sorted({row.get("type", "unknown") for row in results}),
                "top_results": [row.get("title") for row in results[:3]],
            }
        if node == "summarize_context":
            return {"evidence_summary": patch.get("compact_results", "")[:900]}
        if node == "plan_ui":
            return {
                "response_route": patch.get("response_route"),
                "ui_actions": patch.get("actions", []),
            }
        if node == "load_match":
            card = (patch.get("cards") or [{}])[0]
            return {
                "match_target": patch.get("match_target"),
                "card": card.get("type"),
                "facts_preview": patch.get("match_facts", "")[:900],
            }
        if node == "llm_response":
            return {
                "provider": patch.get("provider"),
                "model": patch.get("model"),
                "model_used": patch.get("model_used", False),
                "candidate": patch.get("candidate", "")[:900],
            }
        if node in {"exact_response", "validate_response"}:
            return {
                "reply": patch.get("reply", "")[:900],
                "validation": patch.get("validation"),
            }
        if node == "finalize":
            return {"tool_calls": patch.get("tool_calls", [])}
        return {}

    @staticmethod
    def _belongs(row: dict[str, Any], tool_name: str) -> bool:
        mapping = {"search_matches": "match", "search_players": "player", "search_standings": "standing", "search_analytics": "analytics"}
        return row.get("type") == mapping[tool_name]

    @staticmethod
    def _ui_actions(
        message: str,
        results: list[dict[str, Any]],
        entities: dict[str, Any] | None = None,
        target: dict[str, str] | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        text = message.casefold()
        actions = []
        resolved = entities or {}
        context = ui_context or {}
        if resolved.get("intent") == "list_matches":
            actions.append({
                "type": "set_archive_filters",
                "season": (resolved.get("years") or [resolved.get("ui_season")])[0],
                "query": (resolved.get("teams") or [""])[0],
            })
        elif target and (target.get("match_slug") != context.get("match_slug") or target.get("series_slug") != context.get("series_slug")):
            # Only open a match Pandit actually resolved, never a guess.
            actions.append({"type": "open_match", **target})
        if "ball by ball" in text or "ball-by-ball" in text or "commentary" in text:
            actions.insert(0, {"type": "set_match_tab", "tab": "balls"})
        elif "momentum" in text or "analysis" in text or "analytics" in text:
            actions.insert(0, {"type": "set_match_tab", "tab": "analytics"})
        elif "partnership" in text or "scorecard" in text or "batting" in text or "bowling" in text:
            actions.insert(0, {"type": "set_match_tab", "tab": "scorecard"})
        if "light mode" in text:
            actions.insert(0, {"type": "theme", "value": "light"})
        elif "dark mode" in text:
            actions.insert(0, {"type": "theme", "value": "dark"})
        if "share" in text or "copy summary" in text:
            actions.append({"type": "copy_summary"})
        unique = []
        for action in actions:
            if action not in unique:
                unique.append(action)
        return unique[:4]

    @staticmethod
    def _fallback_reply(
        message: str,
        results: list[dict[str, Any]],
        entities: dict[str, Any] | None = None,
    ) -> str:
        resolved = entities or {}
        if not results:
            correction = CricbotAgent._resolution_note(resolved)
            prefix = f"{correction} " if correction else ""
            return f"{prefix}I couldn't find matching cricket data. Try a team, player, season, venue, score, standings, momentum, or pitch query."
        if resolved.get("intent") == "list_matches":
            matches = [row for row in results if row.get("type") == "match"]
            if matches:
                teams = ", ".join(resolved.get("teams", [])) or "matching"
                seasons = ", ".join(str(year) for year in resolved.get("years", [])) or "the selected season"
                note = CricbotAgent._resolution_note(resolved)
                opening = f"{note} " if note else ""
                opening += f"Here are all {len(matches)} {teams} matches from IPL {seasons}:"
                lines = [
                    f"{index}. {CricbotAgent._display_date(row.get('date', ''))} · {row['title']} — {row['subtitle']}"
                    for index, row in enumerate(matches, 1)
                ]
                return "\n".join([opening, *lines])
        text = message.casefold()
        preferred_type = None
        preferred_id = None
        if "momentum" in text or "swing" in text or "probability" in text:
            preferred_id = "analytics:momentum"
        elif "impact" in text or "best" in text or "top" in text:
            preferred_type = "analytics"
        elif "standing" in text or "table" in text or "points" in text or "rank" in text:
            preferred_type = "standing"
        elif "player" in text or "batter" in text or "bowler" in text:
            preferred_type = "player"
        lead = next((row for row in results if row.get("id") == preferred_id), None)
        lead = lead or next((row for row in results if row.get("type") == preferred_type), None)
        lead = lead or results[0]
        extra = f" I also found {len(results) - 1} related result{'s' if len(results) != 2 else ''}." if len(results) > 1 else ""
        return f"{lead['title']}: {lead['subtitle']}. {lead['meta']}.{extra}"

    @staticmethod
    def _needs_exact_tool_answer(message: str, entities: dict[str, Any] | None = None) -> bool:
        text = message.casefold()
        # Lists must be exhaustive and theme switches need no prose; everything
        # else is summarised from the facts sheet.
        command = any(phrase in text for phrase in ("light mode", "dark mode")) and len(text.split()) <= 6
        return (entities or {}).get("intent") == "list_matches" or command

    @staticmethod
    def _resolution_note(entities: dict[str, Any]) -> str:
        corrections = entities.get("corrections", [])
        if not corrections:
            return ""
        rendered = [f"“{item['from']}” as {item['to']}" for item in corrections]
        if len(rendered) == 1:
            return f"I understood {rendered[0]}."
        return f"I understood {', '.join(rendered[:-1])}, and {rendered[-1]}."

    @staticmethod
    def _display_date(value: str) -> str:
        if not value:
            return "Date unavailable"
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b")
        except ValueError:
            return value

    @staticmethod
    def _numbers_are_grounded(reply: str, facts: str, message: str) -> bool:
        def numbers(value: str) -> set[float]:
            return {float(token) for token in re.findall(r"[+-]?\d+(?:\.\d+)?", value)}

        return numbers(reply).issubset(numbers(f"{facts}\n{message}"))

    @staticmethod
    def _limit_sentences(reply: str) -> str:
        lines = []
        for line in (item.strip() for item in reply.splitlines()):
            if line and line not in lines:
                lines.append(line)
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(lines))
        return " ".join(sentences[:4]).strip()
