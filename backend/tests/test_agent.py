"""Unit tests for Gemma completion and LangChain search tooling."""

import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.agent.gemma import GemmaCompletionClient  # noqa: E402
from backend.agent.service import CricbotAgent  # noqa: E402
from backend.agent.tools import run_search, select_tools  # noqa: E402


class AgentTests(unittest.TestCase):
    def test_gemma_completion_contract(self):
        def handler(request: httpx.Request):
            self.assertEqual(request.url.path, "/v1/completions")
            payload = __import__("json").loads(request.content)
            self.assertEqual(payload["model"], "gemma-4-E4B")
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["stop"], ["\n\n"])
            return httpx.Response(200, json={
                "choices": [{"text": " New Delhi", "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            })

        http = httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
        client = GemmaCompletionClient(client=http)
        self.assertEqual(client.complete("Question: Capital of India?\nAnswer:"), "New Delhi")

    def test_gemma_trims_replayed_prompt(self):
        def handler(_request: httpx.Request):
            return httpx.Response(200, json={
                "choices": [{"text": "Krunal Pandya led the impact chart.\nFacts: repeated prompt"}],
            })

        http = httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handler))
        client = GemmaCompletionClient(client=http)
        self.assertEqual(client.complete("Question: Highest impact?\nAnswer:"), "Krunal Pandya led the impact chart.")

    def test_natural_language_router_selects_relevant_tools(self):
        selected = select_tools("Show RCB's momentum and biggest impact player")
        self.assertIn("search_matches", selected)
        self.assertIn("search_players", selected)
        self.assertIn("search_analytics", selected)

    def test_search_results_include_ui_actions(self):
        results, sources = run_search("Show the IPL standings")
        self.assertEqual(sources, ["search_standings"])
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["action"]["type"], "navigate")

    def test_numeric_grounding_rejects_new_numbers(self):
        self.assertTrue(CricbotAgent._numbers_are_grounded("Krunal scored +50.4.", "Krunal · +50.4", "impact"))
        self.assertFalse(CricbotAgent._numbers_are_grounded("The swing came in over 18.", "over 20 · -14.0", "momentum"))

    def test_repeated_completion_lines_are_deduplicated(self):
        reply = "RCB won by 6 runs\nRCB won by 6 runs\nRCB won by 6 runs"
        self.assertEqual(CricbotAgent._limit_sentences(reply), "RCB won by 6 runs")


if __name__ == "__main__":
    unittest.main()
