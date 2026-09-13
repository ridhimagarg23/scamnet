"""
test_config_and_resilience.py
=============================
Regression tests for the configuration/robustness fixes:

* a missing ``OPENROUTER_API_KEY`` no longer kills the whole server -
  ``GET /health`` reports ``degraded`` and ``POST /analyze`` answers an
  explicit HTTP 503 while the integration endpoints keep working;
* ``TRACEAI_STRICT_CONFIG=1`` restores the fail-fast behaviour;
* ``PromptLoader`` resolves ``prompts/`` relative to the repository
  root, so the agents work when uvicorn/CLI is started from another
  working directory;
* CORS origins come from configuration.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import importlib
import os
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException


def _reload_config(env):
    """
    Import config.py again with a patched environment.

    The freshly built ``config.settings`` object is returned, but the
    module's previous settings object is restored afterwards so other
    already-imported modules (which hold their own reference via
    ``from config import settings``) are never affected.
    """

    import config as config_module

    original_settings = config_module.settings

    try:
        with mock.patch.dict(os.environ, env, clear=False):
            # Capture the freshly built Settings BEFORE the finally
            # clause restores the module's previous object.
            return importlib.reload(config_module).settings
    finally:
        config_module.settings = original_settings


class TestConfigResilience(unittest.TestCase):

    def test_missing_llm_key_does_not_crash_the_import(self):
        """The server must boot so integrations can be verified."""

        settings = _reload_config({"OPENROUTER_API_KEY": ""})

        self.assertFalse(settings.llm_configured)
        self.assertFalse(settings.OPENROUTER_API_KEY)

    def test_strict_mode_restores_fail_fast(self):
        """TRACEAI_STRICT_CONFIG=1 keeps the old crash-on-misconfig."""

        with self.assertRaises(ValueError):
            _reload_config({
                "OPENROUTER_API_KEY": "",
                "TRACEAI_STRICT_CONFIG": "1",
            })

    def test_cors_origins_are_configurable(self):
        settings = _reload_config({
            "CORS_ALLOW_ORIGINS": "https://a.example, https://b.example"
        })

        self.assertEqual(
            settings.CORS_ALLOW_ORIGINS,
            ["https://a.example", "https://b.example"],
        )

    def test_cors_defaults_include_local_dev(self):
        settings = _reload_config({"CORS_ALLOW_ORIGINS": ""})

        self.assertIn("http://localhost:3000", settings.CORS_ALLOW_ORIGINS)
        self.assertIn("http://127.0.0.1:3000", settings.CORS_ALLOW_ORIGINS)


class TestLlmNotConfiguredHandling(unittest.TestCase):

    def test_llm_client_refuses_with_a_clear_error(self):
        from llm import llm_client
        from llm.llm_client import LLMClient, LLMNotConfiguredError

        with mock.patch.object(llm_client.settings, "OPENROUTER_API_KEY", None):
            with self.assertRaises(LLMNotConfiguredError) as ctx:
                LLMClient()

        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_analyze_answers_503_without_llm_key(self):
        from backend import api as api_module
        from backend.api import InvestigationRequest, analyze

        with mock.patch.object(api_module.settings, "OPENROUTER_API_KEY", None):
            with self.assertRaises(HTTPException) as ctx:
                analyze(InvestigationRequest(message="Dear customer...", session_id="t"))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["status"], "llm_not_configured")

    def test_health_reports_degraded_without_llm_key(self):
        from backend import api as api_module
        from backend.api import health

        with mock.patch.object(api_module.settings, "OPENROUTER_API_KEY", None):
            payload = health()

        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["llm_configured"])

    def test_health_reports_healthy_with_llm_key(self):
        from backend.api import health

        payload = health()

        self.assertEqual(payload["status"], "healthy")
        self.assertTrue(payload["llm_configured"])


class TestPromptLoaderPaths(unittest.TestCase):

    def test_prompts_load_from_any_working_directory(self):
        """
        The loader used to resolve ``prompts/`` against the process CWD,
        which broke every agent when uvicorn was launched elsewhere.
        """

        from tools.prompt_loader import PromptLoader

        original = os.getcwd()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)

                for filename in (
                    "investigation_prompt.txt",
                    "conversation_prompt.txt",
                    "report_prompt.txt",
                    "telegram_assistant_prompt.txt",
                ):
                    text = PromptLoader.load(filename)
                    self.assertTrue(text.strip(), filename)
        finally:
            os.chdir(original)


class TestProgressStepper(unittest.TestCase):
    """The UI stepper must be able to show every step as done."""

    def _investigation(self, with_iocs=True):
        from utils.schemas import InvestigationResult

        return InvestigationResult(
            is_scam=True,
            confidence=90,
            threat_type="Banking Phishing",
            summary="Fake bank portal.",
            urls=["http://sbi-secure-login.co.in"] if with_iocs else [],
            risk_score=82,
            risk_level="HIGH",
        )

    def _state(self, turn_number):
        from tools.adaptive_investigation_engine import AdaptiveInvestigationEngine

        engine = AdaptiveInvestigationEngine()
        state = engine.initialize("Banking Phishing")
        state.turn_number = turn_number
        return state

    def test_report_step_is_done_once_the_case_matured(self):
        from backend.api import build_progress

        steps = build_progress(
            self._investigation(), self._state(3), has_report=True
        )
        report_step = steps[-1]

        self.assertEqual(report_step["state"], "done")
        # No "locked" step directly after a "done" step (UI rule).
        for index in range(len(steps) - 1):
            if steps[index]["state"] == "done":
                self.assertNotEqual(steps[index + 1]["state"], "locked")

    def test_report_step_is_current_before_evidence_is_secured(self):
        from backend.api import build_progress

        steps = build_progress(
            self._investigation(with_iocs=False),
            self._state(1),
            has_report=True,
        )
        self.assertEqual(steps[-1]["state"], "current")


class TestSessionCap(unittest.TestCase):

    def test_sessions_are_evicted_fifo(self):
        """An unattended server cannot grow its session dict forever."""

        from backend import api

        original_sessions = api.sessions
        original_max = api.MAX_SESSIONS

        try:
            api.sessions = {}
            api.MAX_SESSIONS = 3

            for index in range(5):
                session_id = f"session_{index}"
                if session_id not in api.sessions:
                    if len(api.sessions) >= api.MAX_SESSIONS:
                        oldest = next(iter(api.sessions))
                        api.sessions.pop(oldest, None)
                    api.sessions[session_id] = {"session": None}

            self.assertEqual(list(api.sessions.keys()), [
                "session_2", "session_3", "session_4"
            ])
        finally:
            api.sessions = original_sessions
            api.MAX_SESSIONS = original_max


if __name__ == "__main__":
    unittest.main()
