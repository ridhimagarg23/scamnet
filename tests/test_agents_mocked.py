"""
test_agents_mocked.py
=====================
Offline unit tests for the three LLM agents.

The LLM is replaced by a MagicMock (``patch`` on
``llm.llm_client.LLMClient.generate``), so these tests exercise the
agents' full plumbing - prompt construction, entity extraction,
validation and risk scoring - without any network call or API key.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from unittest.mock import patch

from agents.conversation_agent import ConversationAgent
from agents.investigation_agent import InvestigationAgent
from agents.report_agent import ReportAgent
from tools.adaptive_investigation_engine import InvestigationState, InvestigationProfile
from utils.schemas import (
    ConversationResult,
    InvestigationResult,
    ReportResult,
)


class TestAgentsMocked(unittest.TestCase):

    @patch("llm.llm_client.LLMClient.generate")
    def test_investigation_agent(self, mock_generate):
        """
        InvestigationAgent must combine the (mocked) LLM verdict with
        real regex-extracted IOCs and a RiskEngine score.

        The sample message embeds http://sbi-fake.com - the entity
        extractor should find it without any help from the LLM.
        """
        # Mock the LLM Response for investigation
        mock_generate.return_value = {
            "is_scam": True,
            "confidence": 95,
            "threat_type": "Banking Phishing",
            "summary": "Suspicious SMS claiming account block"
        }

        agent = InvestigationAgent()
        message = "Dear SBI customer, your card is blocked. Link: http://sbi-fake.com"
        result = agent.run(message)

        self.assertIsInstance(result, InvestigationResult)
        self.assertTrue(result.is_scam)
        self.assertEqual(result.confidence, 95)
        self.assertEqual(result.threat_type, "Banking Phishing")
        self.assertIn("http://sbi-fake.com", result.urls)
        self.assertEqual(result.risk_level, "HIGH")  # RiskEngine evaluates threat score

    @patch("llm.llm_client.LLMClient.generate")
    def test_conversation_agent(self, mock_generate):
        """
        ConversationAgent must pass through the generated reply and
        objective into a validated ConversationResult.
        """
        # Mock the LLM Response for conversation
        mock_generate.return_value = {
            "reply": "Why was it blocked? I need my money.",
            "objective": "Pretend to be concerned and query process.",
            "expected_outcome": "Scammer details verification steps."
        }

        investigation = InvestigationResult(
            is_scam=True,
            confidence=95,
            threat_type="Banking Phishing",
            summary="Fake alert",
            risk_score=90,
            risk_level="HIGH"
        )

        state = InvestigationState(
            turn_number=1,
            current_objective="Extract bank details",
            current_strategy="Anxious victim",
            profile=InvestigationProfile(
                language="English",
                communication_style="Anxious",
                digital_literacy="Low"
            )
        )

        agent = ConversationAgent()
        result = agent.run(
            investigation=investigation,
            investigation_state=state,
            latest_message="Verify now",
            conversation_history=""
        )

        self.assertIsInstance(result, ConversationResult)
        self.assertEqual(result.reply, "Why was it blocked? I need my money.")
        self.assertEqual(result.objective, "Pretend to be concerned and query process.")

    @patch("llm.llm_client.LLMClient.generate")
    def test_report_agent(self, mock_generate):
        """
        ReportAgent must wrap the (mocked) title + markdown into a
        validated ReportResult.
        """
        # Mock the LLM Response for report generation
        mock_generate.return_value = {
            "title": "TraceAI Investigation Report: Banking Phishing",
            "markdown": "# Scam Report\n- Threat: Phishing"
        }

        investigation = InvestigationResult(
            is_scam=True,
            confidence=95,
            threat_type="Banking Phishing",
            summary="Fake alert",
            risk_score=90,
            risk_level="HIGH"
        )

        conversation = ConversationResult(
            reply="Reply",
            objective="Objective",
            expected_outcome="Outcome"
        )

        agent = ReportAgent()
        result = agent.run(investigation, conversation)

        self.assertIsInstance(result, ReportResult)
        self.assertEqual(result.title, "TraceAI Investigation Report: Banking Phishing")
        self.assertIn("Scam Report", result.markdown)
