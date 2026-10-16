"""Offline tests for the Groq/OpenRouter/Gemma provider chain."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.agent.llm import PROVIDERS, ChatProvider, LLMRouter, load_env_file  # noqa: E402


def provider(name, handler, key="test-key"):
    env = {"GROQ_API_KEY": key, "OPENROUTER_API_KEY": key}
    with patch.dict(os.environ, env):
        config = PROVIDERS[name]()
        return ChatProvider(config, client=httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(handler)))


def answer(text):
    return lambda request: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


class LLMTests(unittest.TestCase):
    def test_openai_chat_contract(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return answer(" RCB won. ")(request)

        groq = provider("groq", handler)
        self.assertEqual(groq.chat([{"role": "user", "content": "hi"}]), "RCB won.")
        self.assertEqual(seen["path"], "/openai/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["messages"][0]["content"], "hi")

    def test_router_falls_back_in_order_and_reports_provider(self):
        failing = provider("groq", lambda request: httpx.Response(429, text="rate limited"))
        working = provider("openrouter", answer("From OpenRouter."))
        text, name, model = LLMRouter([failing, working]).chat([{"role": "user", "content": "q"}])
        self.assertEqual((text, name), ("From OpenRouter.", "openrouter"))
        self.assertTrue(model)

    def test_unconfigured_providers_are_skipped_and_errors_are_explained(self):
        unconfigured = provider("groq", answer("never"), key="")
        broken = provider("openrouter", lambda request: (_ for _ in ()).throw(httpx.ConnectError("blocked", request=request)))
        router = LLMRouter([unconfigured, broken])
        with self.assertRaises(RuntimeError) as raised:
            router.chat([{"role": "user", "content": "q"}])
        self.assertIn("openrouter", str(raised.exception))
        self.assertFalse(router.status()[0]["configured"])

    def test_bad_response_shape_is_an_error(self):
        with self.assertRaises(RuntimeError):
            provider("groq", lambda request: httpx.Response(200, json={"choices": []})).chat([{"role": "user", "content": "q"}])

    def test_env_file_does_not_override_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("# comment\nCRICBOT_TEST_A=from-file\nCRICBOT_TEST_B='quoted'\n")
            with patch.dict(os.environ, {"CRICBOT_TEST_A": "from-shell"}):
                load_env_file(path)
                self.assertEqual(os.environ["CRICBOT_TEST_A"], "from-shell")
                self.assertEqual(os.environ["CRICBOT_TEST_B"], "quoted")
            os.environ.pop("CRICBOT_TEST_B", None)


if __name__ == "__main__":
    unittest.main()
