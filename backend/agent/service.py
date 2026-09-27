"""LangChain orchestration for grounded cricket chat and UI control."""

import json
import re
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda

from .gemma import GemmaCompletionClient
from .tools import run_search


PROMPT = PromptTemplate.from_template(
    """You are Pandit, Cricbot's cricket assistant. Answer using only the facts below.
If a fact is unavailable, say so instead of guessing. Use at most four short sentences.
Facts:
{tool_results}
Dashboard context: {ui_context}
Recent conversation: {history}
Question: {message}
Answer:"""
)


class CricbotAgent:
    """Completion-model agent with deterministic LangChain tool execution.

    Gemma 4 E4B is a base completion model without native tool calling. Tool
    routing therefore remains deterministic and inspectable; LangChain tools
    ground the context before Gemma writes the natural-language response.
    """

    def __init__(self, gemma: GemmaCompletionClient | None = None):
        self.gemma = gemma or GemmaCompletionClient()
        self.chain = (
            PROMPT
            | RunnableLambda(lambda prompt: self.gemma.complete(prompt.to_string()))
            | StrOutputParser()
        )

    def status(self) -> dict[str, Any]:
        ready = self.gemma.ready()
        return {
            "status": "ready" if ready else "offline",
            "model": self.gemma.model,
            "base_url": self.gemma.base_url,
            "contract": "v1/completions",
        }

    def answer(
        self,
        message: str,
        history: list[dict[str, str]] | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        results, tool_names = run_search(message, limit=10)
        actions = self._ui_actions(message, results)
        compact_history = "\n".join(
            f"{item.get('role', 'user').title()}: {item.get('content', '')[:500]}"
            for item in (history or [])[-6:]
        ) or "No previous messages."
        compact_results = "\n".join(
            f"- {row['title']}: {row['subtitle']} ({row['meta']})"
            for row in results[:8]
        ) or "- No matching cricket data was found."
        model_used = False
        reply = ""
        if not self._needs_exact_tool_answer(message):
            try:
                candidate = self.chain.invoke({
                    "message": message,
                    "history": compact_history,
                    "ui_context": json.dumps(ui_context or {}, ensure_ascii=False),
                    "tool_results": compact_results,
                }).strip()
                model_used = bool(candidate)
                if self._numbers_are_grounded(candidate, compact_results, message):
                    reply = self._limit_sentences(candidate)
            except (RuntimeError, OSError):
                pass

        if not reply:
            reply = self._fallback_reply(message, results)

        return {
            "reply": reply or self._fallback_reply(message, results),
            "tool_calls": [{"name": name, "result_count": sum(1 for row in results if self._belongs(row, name))} for name in tool_names],
            "results": results[:5],
            "ui_actions": actions,
            "model": self.gemma.model,
            "model_used": model_used,
        }

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
        if "momentum" in text:
            actions.insert(0, {"type": "set_view", "view": "momentum"})
        elif "partnership" in text or "stand" in text:
            actions.insert(0, {"type": "set_view", "view": "stands"})
        elif "scorecard" in text or "batting" in text:
            actions.insert(0, {"type": "set_view", "view": "scorecard"})
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
