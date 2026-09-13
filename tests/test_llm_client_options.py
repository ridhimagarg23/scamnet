"""
test_llm_client_options.py
==========================
Offline unit tests for LLMClient request options and per-agent budgets.

``POST /analyze`` runs three sequential LLM calls, so every agent passes
a ``max_tokens`` budget matched to its output shape: small JSON verdicts
need far fewer tokens than the markdown report, and the cap keeps one
slow/verbose model from stalling a turn (or the per-token bill) without
bound. These tests lock both the plumbing and the budgets.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from unittest.mock import MagicMock, patch

from agents.conversation_agent import ConversationAgent
from agents.investigation_agent import InvestigationAgent
from agents.report_agent import ReportAgent
from llm.llm_client import LLMClient
from tools.adaptive_investigation_engine import (
    InvestigationProfile,
    InvestigationState,
)
from utils.schemas import ConversationResult, InvestigationResult


def _fake_completion(text: str):
    """A minimal OpenAI-compatible chat.completions.create() response."""

    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _sample_investigation() -> InvestigationResult:
    return InvestigationResult(
        is_scam=True,
        confidence=95,
        threat_type="Banking Phishing",
        summary="Fake alert",
        risk_score=90,
        risk_level="HIGH",
    )


def _sample_state() -> InvestigationState:
    return InvestigationState(
        turn_number=1,
        current_objective="Extract bank details",
        current_strategy="Anxious victim",
        profile=InvestigationProfile(
            language="English",
            communication_style="Anxious",
            digital_literacy="Low",
        ),
    )


class TestMaxTokensPlumbing(unittest.TestCase):

    @patch("llm.llm_client.OpenAI")
    def test_max_tokens_forwarded_to_provider(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = (
            _fake_completion('{"ok": true}')
        )

        client = LLMClient()
        result = client.generate("hi", json_output=True, max_tokens=800)

        self.assertEqual(result, {"ok": True})
        _, kwargs = (
            mock_openai_cls.return_value.chat.completions.create.call_args
        )
        self.assertEqual(kwargs["max_tokens"], 800)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    @patch("llm.llm_client.OpenAI")
    def test_max_tokens_omitted_by_default(self, mock_openai_cls):
        mock_openai_cls.return_value.chat.completions.create.return_value = (
            _fake_completion("hello")
        )

        client = LLMClient()
        result = client.generate("hi")

        self.assertEqual(result, "hello")
        _, kwargs = (
            mock_openai_cls.return_value.chat.completions.create.call_args
        )
        self.assertNotIn("max_tokens", kwargs)


class TestAgentBudgets(unittest.TestCase):
    """Each agent's budget matches its output shape (small JSON vs report)."""

    @patch("llm.llm_client.LLMClient.generate")
    def test_investigation_budget(self, mock_generate):
        mock_generate.return_value = {
            "is_scam": True,
            "confidence": 95,
            "threat_type": "Banking Phishing",
            "summary": "Suspicious SMS",
        }

        InvestigationAgent().run("Your card is blocked.")

        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["max_tokens"], 800)

    @patch("llm.llm_client.LLMClient.generate")
    def test_conversation_budget(self, mock_generate):
        mock_generate.return_value = {
            "reply": "Why was it blocked?",
            "objective": "Query the process.",
            "expected_outcome": "Scammer explains.",
        }

        ConversationAgent().run(
            investigation=_sample_investigation(),
            investigation_state=_sample_state(),
            latest_message="Verify now",
            conversation_history="",
        )

        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["max_tokens"], 800)

    @patch("llm.llm_client.LLMClient.generate")
    def test_report_budget_is_larger(self, mock_generate):
        mock_generate.return_value = {
            "title": "Report",
            "markdown": "# Report",
        }

        conversation = ConversationResult(
            reply="Reply",
            objective="Objective",
            expected_outcome="Outcome",
        )

        ReportAgent().run(_sample_investigation(), conversation)

        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["max_tokens"], 2500)


if __name__ == "__main__":
    unittest.main()
