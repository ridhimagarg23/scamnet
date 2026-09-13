"""
test_llm_providers.py
=====================
Offline unit tests for the multi-provider LLM layer (OpenRouter +
NVIDIA NIM) and its automatic fallback chain.

* provider normalization / aliases (``nim`` -> ``nvidia``);
* active provider/model resolution + runtime switching;
* ``LLMClient.attempt_chain()`` ordering (primary -> spares ->
  other provider);
* fallback on invalid JSON / unknown-model errors (next spare
  answers, the failure is recorded, the call succeeds);
* total failure raises RuntimeError listing every tried model;
* ``GET /api/llm/status``, ``GET /api/llm/models`` and
  ``POST /api/llm/select`` behaviour (no network, no secrets leaked).

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from unittest.mock import MagicMock, patch

from config import normalize_provider


def _fake_completion(text: str):
    """A minimal OpenAI-compatible chat.completions.create() response."""

    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


class TestProviderNormalization(unittest.TestCase):

    def test_aliases_normalize_to_canonical_ids(self):
        self.assertEqual(normalize_provider("openrouter"), "openrouter")
        self.assertEqual(normalize_provider("nvidia"), "nvidia")
        self.assertEqual(normalize_provider("nim"), "nvidia")
        self.assertEqual(normalize_provider("NVIDIA-NIM"), "nvidia")
        self.assertIsNone(normalize_provider(None))
        self.assertIsNone(normalize_provider("  "))

    def test_known_provider_check(self):
        from config import settings

        self.assertTrue(settings.is_known_provider("openrouter"))
        self.assertTrue(settings.is_known_provider("nim"))
        self.assertFalse(settings.is_known_provider("bogus"))


class TestActiveSelection(unittest.TestCase):

    def test_resolve_defaults_to_active(self):
        from config import settings

        provider, model = settings.resolve_provider_model(None, None)

        self.assertEqual(provider, settings.ACTIVE_PROVIDER)
        self.assertEqual(model, settings.ACTIVE_MODEL)

    def test_resolve_switch_uses_provider_default(self):
        from config import settings

        other = (
            "nvidia" if settings.ACTIVE_PROVIDER == "openrouter"
            else "openrouter"
        )
        provider, model = settings.resolve_provider_model(other, None)

        self.assertEqual(provider, other)
        self.assertEqual(model, settings.get_default_model(other))

    def test_explicit_model_wins(self):
        from config import settings

        provider, model = settings.resolve_provider_model(
            "nvidia", "custom/model-id"
        )

        self.assertEqual(provider, "nvidia")
        self.assertEqual(model, "custom/model-id")

    def test_set_active_rejects_unknown_provider(self):
        from config import settings

        with self.assertRaises(ValueError):
            settings.set_active("bogus")

    def test_set_active_round_trip(self):
        from config import settings

        original = (settings.ACTIVE_PROVIDER, settings.ACTIVE_MODEL)

        try:
            provider, model = settings.set_active("nvidia")

            self.assertEqual(provider, "nvidia")
            self.assertEqual(model, settings.get_default_model("nvidia"))

            provider, model = settings.set_active(
                "nim", "custom/model-id"
            )

            self.assertEqual(provider, "nvidia")
            self.assertEqual(model, "custom/model-id")
        finally:
            settings.ACTIVE_PROVIDER, settings.ACTIVE_MODEL = original


class TestAttemptChain(unittest.TestCase):

    def test_chain_starts_with_primary_then_spares(self):
        from llm.llm_client import LLMClient

        client = LLMClient(provider="openrouter", model="primary/model")

        chain = client.attempt_chain()

        self.assertEqual(chain[0], ("openrouter", "primary/model"))

        spares = [
            model
            for provider, model in chain[1:]
            if provider == "openrouter"
        ]

        for spare in client.provider and spares:
            self.assertNotEqual(spare, "primary/model")

    def test_chain_has_no_duplicates(self):
        from llm.llm_client import LLMClient

        chain = LLMClient(provider="openrouter").attempt_chain()

        self.assertEqual(len(chain), len(set(chain)))
        self.assertGreaterEqual(len(chain), 1)


class TestFallbackBehaviour(unittest.TestCase):

    @patch("llm.llm_client.OpenAI")
    def test_invalid_json_falls_back_to_next_spare(self, mock_openai_cls):
        from llm.llm_client import LLMClient

        tried = []

        def create_side_effect(**kwargs):
            tried.append(kwargs["model"])

            if kwargs["model"] == "primary/model":
                return _fake_completion("not json {{{")

            return _fake_completion('{"ok": true}')

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        client = LLMClient(provider="openrouter", model="primary/model")
        result = client.generate("hi", json_output=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(tried), 2)
        self.assertEqual(client.last_model, tried[-1])
        self.assertEqual(len(client.fallbacks_used), 1)
        self.assertEqual(client.fallbacks_used[0]["model"], "primary/model")

    @patch("llm.llm_client.OpenAI")
    def test_unknown_model_hops_without_retrying_same_id(
        self, mock_openai_cls
    ):
        from llm.llm_client import LLMClient

        tried = []

        def create_side_effect(**kwargs):
            tried.append(kwargs["model"])

            if kwargs["model"] == "primary/model":
                raise Exception("404 model_not_found: no such model")

            return _fake_completion("hello")

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        client = LLMClient(provider="openrouter", model="primary/model")
        result = client.generate("hi")

        self.assertEqual(result, "hello")
        # The dead id is attempted exactly ONCE, then the spare answers.
        self.assertEqual(tried.count("primary/model"), 1)
        self.assertEqual(len(tried), 2)

    @patch("llm.llm_client.OpenAI")
    def test_total_failure_lists_every_tried_model(self, mock_openai_cls):
        from llm.llm_client import LLMClient

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            Exception("boom 500")
        )

        client = LLMClient(provider="openrouter", model="primary/model")

        with self.assertRaises(RuntimeError) as ctx:
            client.generate("hi", retries=1)

        self.assertGreaterEqual(len(client.fallbacks_used), 1)
        self.assertIn("primary/model", str(ctx.exception))

    def test_missing_provider_key_hops_to_configured_provider(self):
        import llm.llm_client as llm_client_module
        from llm.llm_client import LLMClient

        # Only the OpenRouter test key exists in this suite: asking for
        # NVIDIA must hop to OpenRouter instead of raising.
        with patch.object(
            llm_client_module.settings, "NVIDIA_NIM_API_KEY", None
        ):
            client = LLMClient(provider="nvidia")

        self.assertEqual(client.provider, "openrouter")


class TestLlmEndpoints(unittest.TestCase):

    def test_status_reports_both_providers_without_secrets(self):
        from backend.api import llm_status

        payload = llm_status()

        self.assertIn(payload["active_provider"], ("openrouter", "nvidia"))
        self.assertTrue(payload["active_model"])
        self.assertIn("openrouter", payload["providers"])
        self.assertIn("nvidia", payload["providers"])

        serialized = str(payload)

        self.assertNotIn("test-key", serialized)
        self.assertNotIn("nvapi", serialized)

    def test_models_lists_catalog_per_provider(self):
        from backend.api import llm_models

        for provider in ("openrouter", "nvidia", "nim"):
            payload = llm_models(provider=provider)

            self.assertIn(payload["provider"], ("openrouter", "nvidia"))
            self.assertTrue(payload["models"])
            self.assertTrue(
                all("id" in entry for entry in payload["models"])
            )

    def test_models_rejects_unknown_provider(self):
        from fastapi import HTTPException

        from backend.api import llm_models

        with self.assertRaises(HTTPException) as ctx:
            llm_models(provider="bogus")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_select_switches_and_restores_active(self):
        from config import settings
        from backend.api import LLMSelectRequest, llm_select

        original = (settings.ACTIVE_PROVIDER, settings.ACTIVE_MODEL)

        try:
            payload = llm_select(
                LLMSelectRequest(provider="openrouter", model=None)
            )

            self.assertEqual(payload["active_provider"], "openrouter")
            self.assertTrue(payload["model_known"])
        finally:
            settings.ACTIVE_PROVIDER, settings.ACTIVE_MODEL = original

    def test_select_rejects_unconfigured_provider(self):
        import backend.api as api_module
        from fastapi import HTTPException

        from backend.api import LLMSelectRequest, llm_select

        with patch.object(
            api_module.settings, "NVIDIA_NIM_API_KEY", None
        ):
            with self.assertRaises(HTTPException) as ctx:
                llm_select(LLMSelectRequest(provider="nvidia"))

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(
            ctx.exception.detail["status"],
            "llm_provider_not_configured",
        )

    def test_select_rejects_unknown_provider(self):
        from fastapi import HTTPException

        from backend.api import LLMSelectRequest, llm_select

        with self.assertRaises(HTTPException) as ctx:
            llm_select(LLMSelectRequest(provider="bogus"))

        self.assertEqual(ctx.exception.status_code, 400)


class TestModelCatalog(unittest.TestCase):

    def test_nvidia_catalog_has_fast_default(self):
        from llm.model_catalog import get_model_ids

        ids = get_model_ids("nvidia")

        self.assertIn("meta/llama-3.1-8b-instruct", ids)

    def test_describe_unknown_model_synthesizes_entry(self):
        from llm.model_catalog import describe_model

        entry = describe_model("nvidia", "future/brand-new-model")

        self.assertEqual(entry["id"], "future/brand-new-model")
        self.assertTrue(entry.get("custom"))

    def test_custom_models_file_never_crashes(self):
        from llm import model_catalog

        with patch.object(
            model_catalog, "_custom_models_path"
        ) as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.read_text.return_value = (
                "{not valid json"
            )

            models = model_catalog.get_models("nvidia")

        self.assertTrue(models)


if __name__ == "__main__":
    unittest.main()
