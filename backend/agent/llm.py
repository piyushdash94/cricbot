"""Hosted LLM providers for Pandit's answer synthesis.

Pandit retrieves facts with tools; the LLM picks which stats tools to call
(``plan``) and phrases (summarises) the facts they return (``chat``).
Providers are tried in order until one answers:

    LLM_PROVIDERS=groq,openrouter,gemma      (default)

Groq and OpenRouter both expose the OpenAI ``/chat/completions`` contract.
Keys come from ``GROQ_API_KEY`` / ``OPENROUTER_API_KEY`` (see backend/.env.example).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import json

import httpx

from .gemma import GemmaCompletionClient


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from backend/.env without overriding the shell."""
    target = path or Path(__file__).resolve().parents[1] / ".env"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    key_env: str
    extra_headers: dict[str, str]
    plan_model: str = ""          # model for tool-call planning; defaults to `model`


PROVIDERS = {
    "groq": lambda: ProviderConfig(
        "groq", os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1"),
        os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), "GROQ_API_KEY", {},
        # Planning on a second model gives it its own per-model rate limit (Groq free tier: 8k tokens/min each).
        os.getenv("GROQ_PLAN_MODEL", "openai/gpt-oss-20b"),
    ),
    "openrouter": lambda: ProviderConfig(
        "openrouter", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
        os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct"), "OPENROUTER_API_KEY",
        {"HTTP-Referer": os.getenv("OPENROUTER_REFERER", "http://localhost:3000"), "X-Title": "Cricbot"},
        os.getenv("OPENROUTER_PLAN_MODEL", ""),
    ),
}


class ChatProvider:
    """One OpenAI-compatible chat completion endpoint."""

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None):
        self.config = config
        self.name = config.name
        self.model = config.model
        self.plan_model = config.plan_model or config.model
        self.api_key = os.getenv(config.key_env, "")
        self.client = client or httpx.Client(base_url=config.base_url.rstrip("/"), timeout=httpx.Timeout(45.0, connect=5.0))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}

    def _reasoning(self, model: str) -> dict[str, Any]:
        """Reasoning models (gpt-oss) spend completion tokens thinking; keep that short."""
        if "gpt-oss" not in model:
            return {}
        return {"reasoning_effort": "low"} if self.name == "groq" else {"reasoning": {"effort": "low"}}

    def _post(self, body: dict[str, Any], model: str | None = None) -> httpx.Response:
        if not self.configured:
            raise RuntimeError(f"{self.name}: {self.config.key_env} is not set")
        model = model or self.model
        try:
            return self.client.post("/chat/completions", headers=self._headers(),
                                    json={"model": model, **self._reasoning(model), **body})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name}: {exc}") from exc

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 900, temperature: float = 0.2) -> str:
        response = self._post({"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        if response.status_code != 200:
            raise RuntimeError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{self.name}: unexpected response shape") from exc
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"{self.name}: empty completion")
        return text.strip()

    def plan(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tool calls the model chose: [{"name", "args"}]. [] when it answered without a tool."""
        response = self._post({"messages": messages, "tools": tools, "tool_choice": "auto",
                               "max_tokens": 700, "temperature": 0}, model=self.plan_model)
        if response.status_code == 400 and "tool_use_failed" in response.text:
            return []                                     # the model replied in prose instead
        if response.status_code != 200:
            raise RuntimeError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{self.name}: unexpected response shape") from exc
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                continue
            if function.get("name") and isinstance(args, dict):
                calls.append({"name": function["name"], "args": args})
        return calls

    def ready(self) -> bool:
        if not self.configured:
            return False
        try:
            return self.client.get("/models", headers=self._headers(), timeout=4.0).status_code == 200
        except httpx.HTTPError:
            return False


class GemmaProvider:
    """Adapter so the local completion server fits the chat interface."""

    name = "gemma"

    def __init__(self, client: GemmaCompletionClient | None = None):
        self.gemma = client or GemmaCompletionClient()
        self.model = self.gemma.model
        self.configured = True

    supports_tools = False

    def chat(self, messages: list[dict[str, str]], **_kwargs: Any) -> str:
        prompt = "\n\n".join(f"{item['role'].title()}: {item['content']}" for item in messages) + "\n\nAssistant:"
        return self.gemma.complete(prompt)

    def ready(self) -> bool:
        return self.gemma.ready()


class LLMRouter:
    """Try providers in order; report which one answered."""

    def __init__(self, providers: list[Any] | None = None):
        if providers is None:
            order = [name.strip() for name in os.getenv("LLM_PROVIDERS", "groq,openrouter,gemma").split(",") if name.strip()]
            providers = []
            for name in order:
                if name in PROVIDERS:
                    providers.append(ChatProvider(PROVIDERS[name]()))
                elif name == "gemma":
                    providers.append(GemmaProvider())
        self.providers = providers
        self.last_errors: list[str] = []

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> tuple[str, str, str]:
        """Return (text, provider, model); raises RuntimeError if all fail."""
        self.last_errors = []
        for provider in self.providers:
            if not provider.configured:
                continue
            try:
                return provider.chat(messages, **kwargs), provider.name, provider.model
            except RuntimeError as exc:
                self.last_errors.append(str(exc)[:240])
        raise RuntimeError("; ".join(self.last_errors) or "No LLM provider is configured")

    def plan(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
        """Return (tool calls, provider, model) from the first provider that supports tools."""
        errors = []
        for provider in self.providers:
            if not provider.configured or not getattr(provider, "supports_tools", True) or not hasattr(provider, "plan"):
                continue
            try:
                return provider.plan(messages, tools), provider.name, getattr(provider, "plan_model", provider.model)
            except RuntimeError as exc:
                errors.append(str(exc)[:240])
        raise RuntimeError("; ".join(errors) or "No tool-calling LLM provider is configured")

    def status(self) -> list[dict[str, Any]]:
        return [
            {"name": provider.name, "model": provider.model, "configured": provider.configured, "ready": provider.configured and provider.ready()}
            for provider in self.providers
        ]
