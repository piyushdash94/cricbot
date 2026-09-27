"""LangGraph orchestration for grounded cricket chat and observable UI control."""

import json
import re
from collections.abc import Iterator
from typing import Any, TypedDict

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from .gemma import GemmaCompletionClient
from .tools import run_search, select_tools


PROMPT = PromptTemplate.from_template(
    """You are Pandit, a knowledgeable cricket friend inside Cricbot. Be warm, direct, and conversational.
Answer using only the facts below. If a fact is unavailable, say so instead of guessing. Use at most four short sentences.
Facts:
{tool_results}
Dashboard context: {ui_context}
Recent conversation: {history}
Question: {message}
Answer:"""
)


class PanditState(TypedDict, total=False):
    """State passed between the observable Pandit graph nodes."""

    message: str
    history: list[dict[str, str]]
    ui_context: dict[str, Any]
    query: str
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


GRAPH_NODES = [
    {"id": "normalize_request", "label": "Understand request", "kind": "input", "description": "Normalize the question, dashboard context, and recent conversation."},
    {"id": "route_tools", "label": "Select tools", "kind": "routing", "description": "Choose match, player, standings, or analytics search tools."},
    {"id": "retrieve_facts", "label": "Retrieve cricket facts", "kind": "tool", "description": "Run the selected deterministic tools and collect grounded results."},
    {"id": "summarize_context", "label": "Summarize evidence", "kind": "summary", "description": "Compress tool results and chat history into bounded model context."},
    {"id": "plan_ui", "label": "Plan UI actions", "kind": "action", "description": "Choose dashboard updates and the exact or model response branch."},
    {"id": "exact_response", "label": "Exact tool answer", "kind": "response", "description": "Answer analytics and control requests directly from tool output."},
    {"id": "gemma_response", "label": "Gemma synthesis", "kind": "model", "description": "Use local Gemma 4 to turn grounded facts into natural language."},
    {"id": "validate_response", "label": "Grounding check", "kind": "validation", "description": "Reject unsupported numbers, repetition, and ungrounded model output."},
    {"id": "finalize", "label": "Finalize response", "kind": "output", "description": "Package the answer, provenance, trace, and safe UI actions."},
]

GRAPH_EDGES = [
    {"from": "start", "to": "normalize_request"},
    {"from": "normalize_request", "to": "route_tools"},
    {"from": "route_tools", "to": "retrieve_facts"},
    {"from": "retrieve_facts", "to": "summarize_context"},
    {"from": "summarize_context", "to": "plan_ui"},
    {"from": "plan_ui", "to": "exact_response", "condition": "exact analytics or UI command"},
    {"from": "plan_ui", "to": "gemma_response", "condition": "conversational synthesis"},
    {"from": "gemma_response", "to": "validate_response"},
    {"from": "exact_response", "to": "finalize"},
    {"from": "validate_response", "to": "finalize"},
    {"from": "finalize", "to": "end"},
]


class CricbotAgent:
    """Stateful LangGraph agent with deterministic tools and Gemma synthesis."""

    def __init__(self, gemma: GemmaCompletionClient | None = None):
        self.gemma = gemma or GemmaCompletionClient()
        self.chain = (
            PROMPT
            | RunnableLambda(lambda prompt: self.gemma.complete(prompt.to_string()))
            | StrOutputParser()
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(PanditState)
        workflow.add_node("normalize_request", self._normalize_request)
        workflow.add_node("route_tools", self._route_tools)
        workflow.add_node("retrieve_facts", self._retrieve_facts)
        workflow.add_node("summarize_context", self._summarize_context)
        workflow.add_node("plan_ui", self._plan_ui)
        workflow.add_node("exact_response", self._exact_response)
        workflow.add_node("gemma_response", self._gemma_response)
        workflow.add_node("validate_response", self._validate_response)
        workflow.add_node("finalize", self._finalize)
        workflow.add_edge(START, "normalize_request")
        workflow.add_edge("normalize_request", "route_tools")
        workflow.add_edge("route_tools", "retrieve_facts")
        workflow.add_edge("retrieve_facts", "summarize_context")
        workflow.add_edge("summarize_context", "plan_ui")
        workflow.add_conditional_edges(
            "plan_ui",
            lambda state: state["response_route"],
            {"exact": "exact_response", "model": "gemma_response"},
        )
        workflow.add_edge("exact_response", "finalize")
        workflow.add_edge("gemma_response", "validate_response")
        workflow.add_edge("validate_response", "finalize")
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def status(self) -> dict[str, Any]:
        ready = self.gemma.ready()
        return {
            "status": "ready" if ready else "offline",
            "model": self.gemma.model,
            "base_url": self.gemma.base_url,
            "contract": "v1/completions",
            "orchestrator": "langgraph",
            "graph_version": "1.1",
        }

    @staticmethod
    def graph_definition() -> dict[str, Any]:
        return {
            "name": "Pandit Cricket Assistant",
            "version": "1.1",
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
            "reply": current.get("reply") or self._fallback_reply(message, current.get("results", [])),
            "tool_calls": current.get("tool_calls", []),
            "results": current.get("results", [])[:5],
            "ui_actions": current.get("actions", []),
            "model": self.gemma.model,
            "model_used": current.get("model_used", False),
            "validation": current.get("validation", "exact-tool-output"),
            "trace": trace,
            "graph_version": "1.1",
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
    def _route_tools(state: PanditState) -> PanditState:
        tool_names = select_tools(state["query"])
        return {
            "tool_names": tool_names,
            "intermediate_message": f"Selected {len(tool_names)} grounded cricket tool{'s' if len(tool_names) != 1 else ''}.",
        }

    @staticmethod
    def _retrieve_facts(state: PanditState) -> PanditState:
        results, _ = run_search(state["query"], limit=10)
        return {
            "results": results,
            "intermediate_message": f"Retrieved {len(results)} grounded result{'s' if len(results) != 1 else ''}.",
        }

    @staticmethod
    def _summarize_context(state: PanditState) -> PanditState:
        compact_results = "\n".join(
            f"- {row['title']}: {row['subtitle']} ({row['meta']})"
            for row in state.get("results", [])[:8]
        ) or "- No matching cricket data was found."
        return {
            "compact_results": compact_results,
            "intermediate_message": f"Compressed {len(state.get('results', []))} results into a bounded evidence summary.",
        }

    def _plan_ui(self, state: PanditState) -> PanditState:
        actions = self._ui_actions(state["query"], state.get("results", []))
        response_route = "exact" if self._needs_exact_tool_answer(state["query"]) else "model"
        return {
            "actions": actions,
            "response_route": response_route,
            "intermediate_message": f"Planned {len(actions)} UI action{'s' if len(actions) != 1 else ''}; routed to {response_route} response.",
        }

    def _exact_response(self, state: PanditState) -> PanditState:
        return {
            "reply": self._fallback_reply(state["query"], state.get("results", [])),
            "model_used": False,
            "validation": "exact-tool-output",
            "intermediate_message": "Used deterministic tool output to preserve exact analytics.",
        }

    def _gemma_response(self, state: PanditState) -> PanditState:
        candidate = ""
        try:
            candidate = self.chain.invoke({
                "message": state["query"],
                "history": state["compact_history"],
                "ui_context": json.dumps(state.get("ui_context", {}), ensure_ascii=False),
                "tool_results": state["compact_results"],
            }).strip()
        except (RuntimeError, OSError):
            pass
        return {
            "candidate": candidate,
            "model_used": bool(candidate),
            "intermediate_message": "Gemma produced a grounded candidate." if candidate else "Gemma was unavailable; prepared a deterministic fallback.",
        }

    def _validate_response(self, state: PanditState) -> PanditState:
        candidate = state.get("candidate", "")
        grounded = bool(candidate) and self._numbers_are_grounded(
            candidate,
            state["compact_results"],
            state["query"],
        )
        reply = self._limit_sentences(candidate) if grounded else self._fallback_reply(
            state["query"], state.get("results", [])
        )
        return {
            "reply": reply,
            "validation": "grounded" if grounded else "fallback-after-validation",
            "intermediate_message": "Candidate passed numeric grounding checks." if grounded else "Candidate was replaced with grounded tool output.",
        }

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
        if node == "gemma_response":
            return {
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
    def _ui_actions(message: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        text = message.casefold()
        actions = []
        for row in results:
            action = row.get("action")
            if action and action not in actions:
                actions.append(action)
            if len(actions) >= 2:
                break
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
    def _fallback_reply(message: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return "I couldn't find that in the current match workspace. Try a team, player, score, standings, momentum, or pitch query."
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
    def _needs_exact_tool_answer(message: str) -> bool:
        text = message.casefold()
        return any(token in text for token in (
            "momentum", "swing", "probability", "impact", "best", "top",
            "standings", "points table", "rank", "pitch", "conditions",
        ))

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
