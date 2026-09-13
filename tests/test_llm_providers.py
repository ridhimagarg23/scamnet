"""
test_llm_providers.py
=====================
Offline unit tests for the multi-provider LLM layer (OpenRouter +
NVIDIA NIM) and its FIXED request flow:

    OpenRouter (LLM_MODEL)
      -> no answer within OPENROUTER_DEADLINE (15 s)?
         -> NVIDIA: nvidia/nemotron-3-ultra-550b-a55b
           -> NVIDIA: nvidia/nemotron-3.5-lightning-30b-a3b

* provider normalization / aliases (``nim`` -> ``nvidia``);
* active provider/model resolution + runtime switching;
* ``LLMClient.attempt_chain()`` ordering (OpenRouter -> NVIDIA stage);
* the 15 s soft deadline (a slow OpenRouter is abandoned, never
  retried, and the NVIDIA stage answers);
* fallback on invalid JSON / unknown-model errors (next spare
  answers, the failure is recorded, the call succeeds);
* total failure raises RuntimeError listing every tried model;
* ``GET /api/llm/status``, ``GET /api/llm/models`` and
  ``POST /api/llm/select`` behaviour (no network, no secrets leaked).

Run with:

    OPENROUTER_API_KEY=test-key NVIDIA_NIM_API_KEY=test-key \
        python -m unittest discover -s tests
"""

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from config import normalize_provider, settings

#: The two models of the NVIDIA fallback stage (ultra has priority).
NVIDIA_ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
NVIDIA_LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"


@contextmanager
def both_providers_configured():
    """
    Give BOTH providers a key for the duration of the block.

    The fallback chain only contains the NVIDIA stage when its key is
    present, so every fallback test needs this.
    """

    with patch.object(settings, "OPENROUTER_API_KEY", "test-key"), patch.object(
        settings, "NVIDIA_NIM_API_KEY", "nvapi-test-key"
    ):
        yield


class _FakeClock:
    """A monotonic clock the test controls (no real sleeping)."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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

        with both_providers_configured():
            chain = LLMClient(provider="openrouter").attempt_chain()

        self.assertEqual(len(chain), len(set(chain)))
        self.assertGreaterEqual(len(chain), 1)

    def test_flow_is_openrouter_first_then_nvidia_stage(self):
        """Pasted message -> OpenRouter -> ultra -> lightning."""

        from llm.llm_client import LLMClient

        with both_providers_configured():
            chain = LLMClient().attempt_chain()

        self.assertEqual(chain[0][0], "openrouter")
        self.assertEqual(chain[0][1], settings.LLM_MODEL)

        nvidia_models = [model for provider, model in chain if provider == "nvidia"]

        self.assertEqual(nvidia_models, [NVIDIA_ULTRA, NVIDIA_LIGHTNING])


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

        with both_providers_configured():
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

        with both_providers_configured():
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


class TestFixedRequestFlow(unittest.TestCase):
    """
    The dashboard has no provider picker any more: one pasted message
    goes to OpenRouter first and only falls back to the NVIDIA
    nemotron stage when OpenRouter is too slow (15 s) or fails.
    """

    def setUp(self):
        from llm.llm_client import LLMClient

        self.LLMClient = LLMClient

    def test_nvidia_stage_is_ultra_then_lightning(self):
        self.assertEqual(settings.get_default_model("nvidia"), NVIDIA_ULTRA)
        self.assertEqual(
            settings.get_fallback_models("nvidia")[:2],
            [NVIDIA_ULTRA, NVIDIA_LIGHTNING],
        )

    def test_openrouter_deadline_defaults_to_15_seconds(self):
        self.assertEqual(settings.get_deadline("openrouter"), 15.0)

    @patch("llm.llm_client.OpenAI")
    def test_primary_inside_deadline_never_calls_nvidia(self, mock_openai_cls):
        clock = _FakeClock()

        def create_side_effect(**kwargs):
            clock.advance(3)  # answered well inside the 15 s window
            return _fake_completion('{"ok": true}')

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        with both_providers_configured(), patch("llm.llm_client._now", clock):
            client = self.LLMClient()
            result = client.generate("hi", json_output=True)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_provider, "openrouter")
        self.assertEqual(client.last_model, settings.LLM_MODEL)
        self.assertEqual(client.fallbacks_used, [])

    @patch("llm.llm_client.OpenAI")
    def test_slow_primary_falls_through_to_nemotron_ultra(
        self, mock_openai_cls
    ):
        clock = _FakeClock()
        tried = []

        def create_side_effect(**kwargs):
            tried.append(kwargs["model"])

            if kwargs["model"] == "primary/model":
                clock.advance(20)  # blew the 15 s OpenRouter deadline
                raise Exception("APITimeoutError: Request timed out.")

            return _fake_completion('{"ok": true}')

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        with both_providers_configured(), patch("llm.llm_client._now", clock):
            client = self.LLMClient(
                provider="openrouter", model="primary/model"
            )
            result = client.generate("hi", json_output=True)

        self.assertEqual(result, {"ok": True})
        # OpenRouter is attempted ONCE - a blown deadline is not retried.
        self.assertEqual(tried, ["primary/model", NVIDIA_ULTRA])
        self.assertEqual(client.last_provider, "nvidia")
        self.assertEqual(client.last_model, NVIDIA_ULTRA)
        self.assertEqual(len(client.fallbacks_used), 1)
        self.assertEqual(client.fallbacks_used[0]["provider"], "openrouter")
        self.assertEqual(client.fallbacks_used[0]["model"], "primary/model")

    @patch("llm.llm_client.OpenAI")
    def test_ultra_failure_ends_on_lightning(self, mock_openai_cls):
        clock = _FakeClock()
        tried = []

        def create_side_effect(**kwargs):
            tried.append(kwargs["model"])

            if kwargs["model"] != NVIDIA_LIGHTNING:
                clock.advance(20)
                raise Exception("upstream 500")

            return _fake_completion('{"ok": true}')

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        with both_providers_configured(), patch("llm.llm_client._now", clock):
            client = self.LLMClient(
                provider="openrouter", model="primary/model"
            )
            result = client.generate("hi", json_output=True, retries=1)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.last_model, NVIDIA_LIGHTNING)
        self.assertEqual(
            tried,
            ["primary/model", NVIDIA_ULTRA, NVIDIA_LIGHTNING],
        )

    @patch("llm.llm_client.OpenAI")
    def test_request_timeout_is_capped_by_the_deadline(
        self, mock_openai_cls
    ):
        clock = _FakeClock()
        timeouts = []

        def create_side_effect(**kwargs):
            timeouts.append(kwargs.get("timeout"))
            clock.advance(60)
            raise Exception("timeout")

        mock_openai_cls.return_value.chat.completions.create.side_effect = (
            create_side_effect
        )

        with both_providers_configured(), patch("llm.llm_client._now", clock):
            client = self.LLMClient(
                provider="openrouter", model="primary/model"
            )

            with self.assertRaises(RuntimeError):
                client.generate("hi", retries=1)

        # OpenRouter may never be given more than its 15 s budget...
        self.assertLessEqual(timeouts[0], 15.0)
        # ...while the NVIDIA stage keeps its own (longer) timeout.
        self.assertEqual(timeouts[1], settings.get_timeout("nvidia"))


class TestModelCatalog(unittest.TestCase):

    def test_nvidia_catalog_lists_only_the_two_stage_models(self):
        from llm.model_catalog import get_model_ids

        ids = get_model_ids("nvidia")

        self.assertEqual(ids, [NVIDIA_ULTRA, NVIDIA_LIGHTNING])

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
