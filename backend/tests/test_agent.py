"""Unit tests for Gemma completion and LangChain search tooling."""

import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.agent.gemma import GemmaCompletionClient  # noqa: E402
from backend.agent.entities import resolve_entities  # noqa: E402
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

    def test_historical_query_routes_to_match_archive(self):
        self.assertEqual(select_tools("Find the 2024 final"), ["search_matches"])

    def test_entity_resolver_corrects_team_typo_and_relative_year(self):
        entities = resolve_entities("can list all rcv matches last year", current_year=2026)
        self.assertEqual(entities["teams"], ["RCB"])
        self.assertEqual(entities["years"], [2025])
        self.assertEqual(entities["intent"], "list_matches")
        self.assertTrue(entities["wants_all"])
        self.assertEqual(entities["retrieval_query"], "RCB 2025")
        self.assertEqual(len(entities["corrections"]), 2)

    def test_entity_resolver_carries_team_from_conversation(self):
        entities = resolve_entities(
            "What about last year?",
            history=[{"role": "user", "content": "Show me all RCB matches"}],
            current_year=2026,
        )
        self.assertEqual(entities["teams"], ["RCB"])
        self.assertEqual(entities["years"], [2025])

    def test_entity_resolver_does_not_carry_into_named_fixture(self):
        entities = resolve_entities(
            "Show me the 2024 final",
            history=[{"role": "user", "content": "How did Krunal Pandya bowl for RCB?"}],
            current_year=2026,
        )
        self.assertEqual(entities["teams"], [])
        self.assertEqual(entities["players"], [])
        self.assertEqual(entities["retrieval_query"], "2024 final")

    def test_common_words_are_not_team_typos(self):
        for word in ("ask", "got", "mind", "miss"):
            self.assertEqual(resolve_entities(f"can you {word} about it")["teams"], [], word)
        self.assertEqual(resolve_entities("show mii matches")["teams"], ["MI"])
        greeting = [{"role": "assistant", "content": "Search a season, or ask me to find one."}]
        context = {"teams": ["RCB", "PBKS"]}
        self.assertEqual(resolve_entities("Show me the ball-by-ball", history=greeting, ui_context=context)["teams"], ["RCB", "PBKS"])

    def test_entity_resolver_uses_visible_match_when_nothing_is_named(self):
        context = {"teams": ["RCB", "PBKS"], "season": 2025}
        self.assertEqual(resolve_entities("Show me the ball-by-ball", ui_context=context)["teams"], ["RCB", "PBKS"])
        self.assertEqual(resolve_entities("Show me the 2024 final", ui_context=context)["teams"], [])
        self.assertEqual(resolve_entities("How did CSK do?", ui_context=context)["teams"], ["CSK"])

    @patch("backend.agent.tools.search_ipl_archive")
    def test_list_intent_returns_every_match_and_filters_ui(self, archive):
        archive.return_value = [
            {
                "series_slug": "ipl-2025", "match_slug": f"match-{index}",
                "title": title, "status_text": result, "season": 2025,
                "date": f"2025-04-0{index}T00:00:00Z", "label": f"Match {index}",
                "venue": "Bengaluru", "teams": [{"score": "180/6"}, {"score": "170/8"}],
            }
            for index, (title, result) in enumerate((("RCB vs CSK", "RCB won"), ("MI vs RCB", "MI won")), 1)
        ]
        result = CricbotAgent().answer("list all rcv matches in 2025")
        self.assertFalse(result["model_used"])
        self.assertIn("all 2 RCB matches", result["reply"])
        self.assertIn("RCB vs CSK", result["reply"])
        self.assertIn("MI vs RCB", result["reply"])
        self.assertEqual(result["ui_actions"], [{"type": "set_archive_filters", "season": 2025, "query": "RCB"}])
        self.assertIn("extract_entities", [event["node"] for event in result["trace"]])

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

    def test_langgraph_trace_contains_state_patches(self):
        client = GemmaCompletionClient(client=httpx.Client(
            base_url="http://127.0.0.1:8080",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                "choices": [{"text": "RCB won by 6 runs."}],
            })),
        ))
        result = CricbotAgent(gemma=client).answer("Tell me about RCB vs PBKS")
        entities = next(event for event in result["trace"] if event["node"] == "extract_entities")
        route = next(event for event in result["trace"] if event["node"] == "route_tools")
        self.assertEqual(entities["patch"]["entities"]["teams"], ["RCB", "PBKS"])
        self.assertIn("search_matches", route["patch"]["tool_names"])
        self.assertEqual(result["validation"], "grounded")

    def test_offline_gemma_falls_back_to_deterministic_reply(self):
        def refuse(request: httpx.Request):
            raise httpx.ConnectError("Connection refused", request=request)

        client = GemmaCompletionClient(client=httpx.Client(
            base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(refuse),
        ))
        with self.assertRaises(RuntimeError):
            client.complete("Question: Anything?\nAnswer:")
        result = CricbotAgent(gemma=client).answer("switch to light mode")
        self.assertTrue(result["reply"])
        self.assertFalse(result["model_used"])


if __name__ == "__main__":
    unittest.main()
