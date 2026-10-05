"""Hosted LLM providers for Pandit's answer synthesis.

Pandit retrieves facts deterministically; the LLM only phrases (summarises)
those facts. Providers are tried in order until one answers:

    LLM_PROVIDERS=groq,openrouter,gemma      (default)

Groq and OpenRouter both expose the OpenAI ``/chat/completions`` contract.
Keys come from ``GROQ_API_KEY`` / ``OPENROUTER_API_KEY`` (see backend/.env.example).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

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


PROVIDERS = {
    "groq": lambda: ProviderConfig(
        "groq", os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1"),
        os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), "GROQ_API_KEY", {},
    ),
    "openrouter": lambda: ProviderConfig(
        "openrouter", os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"),
        os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct"), "OPENROUTER_API_KEY",
        {"HTTP-Referer": os.getenv("OPENROUTER_REFERER", "http://localhost:3000"), "X-Title": "Cricbot"},
    ),
}


class ChatProvider:
    """One OpenAI-compatible chat completion endpoint."""

    def __init__(self, config: ProviderConfig, client: httpx.Client | None = None):
        self.config = config
        self.name = config.name
        self.model = config.model
        self.api_key = os.getenv(config.key_env, "")
        self.client = client or httpx.Client(base_url=config.base_url.rstrip("/"), timeout=httpx.Timeout(45.0, connect=5.0))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 400, temperature: float = 0.2) -> str:
        if not self.configured:
            raise RuntimeError(f"{self.name}: {self.config.key_env} is not set")
        try:
            response = self.client.post(
                "/chat/completions",
                headers=self._headers(),
                json={"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.name}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{self.name}: unexpected response shape") from exc
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"{self.name}: empty completion")
        return text.strip()

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

    def status(self) -> list[dict[str, Any]]:
        return [
            {"name": provider.name, "model": provider.model, "configured": provider.configured, "ready": provider.configured and provider.ready()}
            for provider in self.providers
        ]
