"""Client for the local Gemma 4 completion service."""

import os
from typing import Any

import httpx


class GemmaCompletionClient:
    """Small, reusable client matching the Mac Gemma completion contract."""

    def __init__(self, client: httpx.Client | None = None):
        self.base_url = os.getenv("GEMMA_API_BASE", "http://127.0.0.1:8080").rstrip("/")
        self.model = os.getenv("GEMMA_MODEL", "gemma-4-E4B")
        headers = {"Content-Type": "application/json"}
        if api_key := os.getenv("MAC_SERVING_API_KEY"):
            headers["Authorization"] = f"Bearer {api_key}"
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=3.0),
        )

    def ready(self) -> bool:
        try:
            response = self.client.get("/readyz", timeout=3.0)
            return response.status_code == 200 and response.json().get("status") == "ready"
        except (httpx.HTTPError, ValueError):
            return False

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        top_p: float = 0.95,
    ) -> str:
        response = self.client.post(
            "/v1/completions",
            json={
                "model": self.model,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "stream": False,
                "stop": ["\n\n"],
            },
        )
        if response.status_code != 200:
            diagnostic = response.text[:300]
            raise RuntimeError(f"Gemma request failed ({response.status_code}): {diagnostic}")
        payload: dict[str, Any] = response.json()
        choices = payload.get("choices") or []
        if not choices or "text" not in choices[0]:
            raise RuntimeError("Gemma response did not contain choices[0].text")
        text = str(choices[0]["text"]).strip()
        # Base completion models sometimes begin replaying the prompt. Keep the
        # generated answer while trimming those echoed sections.
        for marker in ("\nFacts:", "\nDashboard context:", "\nRecent conversation:", "\nQuestion:", "\nAnswer:"):
            if marker in text:
                text = text.split(marker, 1)[0].strip()
        return text

    def close(self):
        if self._owns_client:
            self.client.close()
