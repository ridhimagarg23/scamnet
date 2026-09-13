"""
test_telegram_integration.py
============================
Offline unit tests for the REAL Telegram Bot API client
(integrations/telegram/) and its backend test endpoints
(backend/telegram_routes.py).

Every HTTP call is stubbed via the injectable ``http_client`` - these
tests NEVER contact Telegram. They pin:

* valid Telegram configuration handling (connect via getMe);
* Telegram API + network error handling (sanitised, no token leaks);
* incoming message normalization (raw update -> IncomingMessage);
* send_message / get_updates request construction (URL, JSON payload,
  long-poll timeouts, offset acknowledgement);
* honest endpoint behaviour (409 not_configured / not_connected,
  502 telegram_error, 422 validation, 200 only on real success).

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from integrations.base import (
    IntegrationNotConfiguredError,
    IntegrationState,
)
from integrations.telegram import (
    IncomingMessage,
    TelegramAPIError,
    TelegramIntegration,
    normalize_update,
)

# Obviously-fake token used ONLY inside these tests.
FAKE_TOKEN = "123456:TEST-FAKE-TOKEN-do-not-leak"


# --------------------------------------------------
# Stubs
# --------------------------------------------------

def make_settings(token=FAKE_TOKEN, api_base="https://api.telegram.org"):
    """Minimal settings stub (mirrors the Telegram keys of config.Settings)."""

    return SimpleNamespace(
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_API_BASE=api_base,
    )


class StubResponse:
    """Mimics the parts of httpx.Response the client uses."""

    def __init__(self, payload, status_code=200, json_error=False):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("response body is not JSON")
        return self._payload


class StubHTTPClient:
    """
    Mimics ``httpx.Client.post(url, json=..., timeout=...)``.

    Queued responses are returned in order; an Exception instance in
    the queue is raised instead. Every call is recorded for assertions.
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError("StubHTTPClient ran out of queued responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def getme_ok(username="scamnet_intel_bot"):
    return StubResponse({
        "ok": True,
        "result": {
            "id": 111222333,
            "is_bot": True,
            "first_name": "SCAMNET Intel",
            "username": username,
        },
    })


def getme_unauthorized():
    return StubResponse(
        {"ok": False, "error_code": 401, "description": "Unauthorized"},
        status_code=401,
    )


def make_integration(*responses, token=FAKE_TOKEN, api_base=None):
    """Build a TelegramIntegration wired to a StubHTTPClient."""

    settings = make_settings(
        token=token,
        api_base=api_base or "https://api.telegram.org",
    )
    stub = StubHTTPClient(*responses)
    return TelegramIntegration(settings, http_client=stub), stub


# A realistic raw Bot API update (private text message).
RAW_MESSAGE_UPDATE = {
    "update_id": 100,
    "message": {
        "message_id": 5,
        "from": {
            "id": 999888,
            "is_bot": False,
            "first_name": "Target",
            "username": "target_user",
            "language_code": "en",
        },
        "chat": {
            "id": 999888,
            "first_name": "Target",
            "username": "target_user",
            "type": "private",
        },
        "date": 1757000000,
        "text": "Dear SBI customer, your card is blocked: http://sbi-fake.com",
        "entities": [{"offset": 40, "length": 19, "type": "url"}],
    },
}


def updates_ok(*updates):
    return StubResponse({"ok": True, "result": list(updates)})


def ok_true():
    """``{"ok": true, "result": true}`` - deleteWebhook / setMyCommands."""
    return StubResponse({"ok": True, "result": True})


def connect_responses(*extra):
    """
    The full response queue a real ``connect()`` consumes:
    getMe -> deleteWebhook -> setMyCommands -> (caller's extra calls).
    """

    return (getme_ok(), ok_true(), ok_true()) + tuple(extra)


def make_connected_integration(*extra):
    """An integration that has already completed a REAL connect()."""

    integration, stub = make_integration(*connect_responses(*extra))
    integration.connect()
    return integration, stub


# --------------------------------------------------
# 1. Configuration + verify_connection (getMe)
# --------------------------------------------------

class TestTelegramConfiguration(unittest.TestCase):

    def test_missing_token_raises_not_configured_without_http(self):
        """No token -> IntegrationNotConfiguredError, zero HTTP calls."""

        integration, stub = make_integration(token=None)

        self.assertFalse(integration.is_configured())
        with self.assertRaises(IntegrationNotConfiguredError):
            integration.verify_connection()
        with self.assertRaises(IntegrationNotConfiguredError):
            integration.connect()
        self.assertEqual(stub.calls, [])

    def test_verify_connection_success(self):
        """getMe proves the token and returns the public bot identity."""

        integration, stub = make_integration(getme_ok())

        bot_info = integration.verify_connection()

        self.assertEqual(bot_info["username"], "scamnet_intel_bot")
        self.assertEqual(bot_info["id"], 111222333)
        # Request construction: POST https://api.telegram.org/bot<TOKEN>/getMe
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(
            stub.calls[0]["url"].endswith(f"/bot{FAKE_TOKEN}/getMe")
        )

    def test_custom_api_base_is_used(self):
        """TELEGRAM_API_BASE override reaches the request URL."""

        integration, stub = make_integration(
            getme_ok(), api_base="https://local-bot-api.example.com"
        )
        integration.verify_connection()

        self.assertTrue(
            stub.calls[0]["url"].startswith(
                "https://local-bot-api.example.com/bot"
            )
        )

    def test_invalid_token_raises_sanitised_error(self):
        """401 Unauthorized -> TelegramAPIError(401) without the token."""

        integration, _ = make_integration(getme_unauthorized())

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.verify_connection()

        self.assertEqual(ctx.exception.error_code, 401)
        self.assertIn("Unauthorized", str(ctx.exception))
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_network_error_never_leaks_token(self):
        """
        Transport failures can carry the request URL (which embeds the
        token) - the raised error must contain only the exception TYPE.
        """

        poisoned = RuntimeError(f"connection refused while opening {FAKE_TOKEN}")
        integration, _ = make_integration(poisoned)

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.verify_connection()

        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))

    def test_non_json_response_handled(self):
        """A proxy garbage page -> clean TelegramAPIError, no crash."""

        integration, _ = make_integration(
            StubResponse(None, status_code=502, json_error=True)
        )

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.verify_connection()

        self.assertIn("non-JSON", str(ctx.exception))
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


# --------------------------------------------------
# 2. connect / disconnect / health
# --------------------------------------------------

class TestTelegramLifecycle(unittest.TestCase):

    def test_connect_success_marks_genuinely_connected(self):
        """connected=True only after Telegram itself accepted getMe."""

        integration, stub = make_integration(*connect_responses())

        integration.connect()

        self.assertTrue(integration.is_connected())

        # connect() issued real Bot API calls in a safe order:
        # getMe -> deleteWebhook -> setMyCommands.
        methods = [call["url"].rsplit("/", 1)[-1] for call in stub.calls]
        self.assertEqual(methods, ["getMe", "deleteWebhook", "setMyCommands"])

        # ...and warmed the health cache: is_connected() must not hit
        # the network again.
        self.assertTrue(integration.is_connected())
        self.assertEqual(len(stub.calls), 3)
        self.assertTrue(integration.get_connection_info()["webhook_cleared"])

        status = integration.get_status()
        self.assertTrue(status.configured)
        self.assertTrue(status.available)
        self.assertTrue(status.connected)
        self.assertEqual(status.state, IntegrationState.CONNECTED)
        self.assertEqual(
            status.connection_info.get("bot_username"), "scamnet_intel_bot"
        )
        # Secret never reaches the serialised status.
        self.assertNotIn(FAKE_TOKEN, status.model_dump_json())

    def test_connect_failure_stays_disconnected_and_reports_error(self):
        """Rejected token -> raise, stay disconnected, honest ERROR state."""

        integration, _ = make_integration(getme_unauthorized())

        with self.assertRaises(TelegramAPIError):
            integration.connect()

        self.assertFalse(integration.is_connected())
        status = integration.get_status()
        self.assertFalse(status.connected)
        self.assertEqual(status.state, IntegrationState.ERROR)
        self.assertEqual(status.connection_info, {})
        self.assertNotIn(FAKE_TOKEN, status.model_dump_json())

    def test_disconnect_clears_session_state(self):
        """disconnect() is a local, honest state reset (no fake teardown)."""

        integration, _ = make_connected_integration()
        integration.disconnect()

        self.assertFalse(integration.is_connected())
        status = integration.get_status()
        self.assertEqual(status.state, IntegrationState.DISCONNECTED)
        # Still usable: configured + real flow implemented.
        self.assertTrue(status.available)

    def test_check_health_ttl_cache_avoids_getme_per_status(self):
        """Repeated is_connected() calls reuse the cached getMe result."""

        integration, stub = make_connected_integration()

        for _ in range(5):
            self.assertTrue(integration.is_connected())

        # connect() ran getMe + deleteWebhook + setMyCommands; the five
        # is_connected() calls added nothing.
        self.assertEqual(len(stub.calls), 3)

    def test_check_health_failure_flips_to_disconnected(self):
        """An expired-cache health ping that fails drops the session."""

        integration, stub = make_integration(
            *connect_responses(getme_unauthorized())
        )
        integration.connect()

        # connect() used getMe + deleteWebhook + setMyCommands.
        self.assertEqual(len(stub.calls), 3)

        # Force the TTL cache to expire.
        integration._last_health_at = time.monotonic() - 10_000

        self.assertFalse(integration.is_connected())
        status = integration.get_status()
        self.assertFalse(status.connected)

        # 3 connect() calls + the failing health-check getMe.
        self.assertEqual(len(stub.calls), 4)


# --------------------------------------------------
# 3. Message normalization
# --------------------------------------------------

class TestMessageNormalization(unittest.TestCase):

    def test_normalize_maps_raw_update_to_internal_structure(self):
        normalized = normalize_update(RAW_MESSAGE_UPDATE)

        self.assertIsInstance(normalized, IncomingMessage)
        self.assertEqual(
            normalized.model_dump(),
            {
                "update_id": 100,
                "chat_id": 999888,
                "message_id": 5,
                "sender_id": 999888,
                "sender_username": "target_user",
                "text": (
                    "Dear SBI customer, your card is blocked: "
                    "http://sbi-fake.com"
                ),
                "timestamp": 1757000000,
            },
        )

    def test_normalized_schema_is_telegram_independent(self):
        """The internal structure exposes exactly the documented fields."""

        self.assertEqual(
            set(IncomingMessage.model_fields),
            {
                "update_id",
                "chat_id",
                "message_id",
                "sender_id",
                "sender_username",
                "text",
                "timestamp",
            },
        )

    def test_normalize_skips_non_message_updates(self):
        self.assertIsNone(normalize_update({"update_id": 1, "channel_post": {"text": "x"}}))
        self.assertIsNone(normalize_update({"update_id": 2, "edited_message": {"text": "x"}}))
        self.assertIsNone(normalize_update({"update_id": 3}))

    def test_normalize_skips_non_text_messages(self):
        photo_update = {
            "update_id": 4,
            "message": {
                "message_id": 9,
                "chat": {"id": 555, "type": "private"},
                "date": 1757000001,
                "photo": [{"file_id": "abc"}],
            },
        }
        self.assertIsNone(normalize_update(photo_update))

    def test_normalize_never_raises_on_malformed_input(self):
        for malformed in (
            None, "text", 42, [], {},
            {"update_id": True, "message": RAW_MESSAGE_UPDATE["message"]},
            {"update_id": 5, "message": {"message_id": 1, "text": "hi", "chat": {}}},
            {"update_id": 6, "message": {"message_id": 1, "text": "hi", "date": 1}},
        ):
            self.assertIsNone(normalize_update(malformed))

    def test_normalize_tolerates_missing_optional_sender_fields(self):
        minimal = {
            "update_id": 7,
            "message": {
                "message_id": 2,
                "chat": {"id": -100123, "type": "supergroup"},
                "date": 1757000002,
                "text": "hello",
            },
        }
        normalized = normalize_update(minimal)

        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized.sender_id)
        self.assertIsNone(normalized.sender_username)
        self.assertEqual(normalized.chat_id, -100123)


# --------------------------------------------------
# 4. get_updates (long polling + offset cursor)
# --------------------------------------------------

class TestGetUpdates(unittest.TestCase):

    def test_fetch_normalizes_and_skips_non_text_updates(self):
        integration, stub = make_integration(
            updates_ok(
                RAW_MESSAGE_UPDATE,
                {"update_id": 101, "channel_post": {"chat": {"id": 1}, "text": "x"}},
                {"update_id": 102, "edited_message": {"message_id": 1, "chat": {"id": 1}, "text": "x"}},
                {
                    "update_id": 103,
                    "message": {
                        "message_id": 8,
                        "chat": {"id": 555, "type": "private"},
                        "date": 1757000003,
                        "photo": [{"file_id": "p"}],
                    },
                },
            )
        )

        messages = integration.get_updates(timeout=0)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, RAW_MESSAGE_UPDATE["message"]["text"])
        self.assertEqual(messages[0].chat_id, 999888)

        # Request construction: POST .../getUpdates with long-poll body.
        payload = stub.calls[0]["json"]
        self.assertEqual(payload["timeout"], 0)
        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["allowed_updates"], ["message"])
        self.assertNotIn("offset", payload)  # first poll has no cursor

    def test_long_poll_timeout_extends_http_read_timeout(self):
        """HTTP read timeout must exceed the Telegram long-poll wait."""

        integration, stub = make_integration(updates_ok())

        integration.get_updates(timeout=25, limit=50)

        payload = stub.calls[0]["json"]
        self.assertEqual(payload["timeout"], 25)
        self.assertEqual(payload["limit"], 50)

        http_timeout = stub.calls[0]["timeout"]
        self.assertEqual(http_timeout.read, 35.0)   # 25 + 10 buffer
        self.assertEqual(http_timeout.connect, 5.0)

    def test_offset_cursor_advances_and_is_sent_on_next_poll(self):
        integration, stub = make_integration(
            updates_ok(RAW_MESSAGE_UPDATE),          # max update_id 100
            updates_ok(),                            # nothing new
        )

        integration.get_updates(timeout=0)
        self.assertEqual(integration._update_offset, 101)

        integration.get_updates(timeout=0)
        self.assertEqual(stub.calls[1]["json"]["offset"], 101)
        # Nothing fetched -> cursor unchanged.
        self.assertEqual(integration._update_offset, 101)

    def test_ack_false_peeks_without_consuming(self):
        integration, _ = make_integration(updates_ok(RAW_MESSAGE_UPDATE))

        messages = integration.get_updates(timeout=0, ack=False)

        self.assertEqual(len(messages), 1)
        self.assertIsNone(integration._update_offset)

    def test_explicit_offset_does_not_touch_internal_cursor(self):
        integration, stub = make_integration(updates_ok(RAW_MESSAGE_UPDATE))

        integration.get_updates(offset=500, timeout=0)

        self.assertEqual(stub.calls[0]["json"]["offset"], 500)
        self.assertIsNone(integration._update_offset)

    def test_invalid_timeout_and_limit_rejected_before_http(self):
        integration, stub = make_integration()

        for kwargs in (
            {"timeout": 99}, {"timeout": -1}, {"limit": 0}, {"limit": 101},
        ):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                integration.get_updates(**kwargs)

        self.assertEqual(stub.calls, [])

    def test_telegram_error_is_sanitised(self):
        integration, _ = make_integration(
            StubResponse(
                {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"},
                status_code=409,
            )
        )

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.get_updates(timeout=0)

        self.assertEqual(ctx.exception.error_code, 409)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


# --------------------------------------------------
# 5. send_message
# --------------------------------------------------

class TestSendMessage(unittest.TestCase):

    def test_send_message_request_construction(self):
        integration, stub = make_integration(
            StubResponse({
                "ok": True,
                "result": {
                    "message_id": 42,
                    "from": {"id": 111222333, "is_bot": True, "username": "scamnet_intel_bot"},
                    "chat": {"id": 999888, "type": "private"},
                    "date": 1757000100,
                    "text": "Hello from SCAMNET",
                },
            })
        )

        result = integration.send_message(999888, "Hello from SCAMNET")

        self.assertEqual(result, {"chat_id": 999888, "message_id": 42})
        self.assertTrue(
            stub.calls[0]["url"].endswith(f"/bot{FAKE_TOKEN}/sendMessage")
        )
        self.assertEqual(
            stub.calls[0]["json"],
            {"chat_id": 999888, "text": "Hello from SCAMNET"},
        )

    def test_send_message_supports_group_chat_ids(self):
        integration, stub = make_integration(
            StubResponse({
                "ok": True,
                "result": {
                    "message_id": 7,
                    "chat": {"id": -1001234567890, "type": "supergroup"},
                    "date": 1757000100,
                    "text": "x",
                },
            })
        )

        result = integration.send_message(-1001234567890, "x")
        self.assertEqual(result["chat_id"], -1001234567890)
        self.assertEqual(stub.calls[0]["json"]["chat_id"], -1001234567890)

    def test_invalid_inputs_rejected_before_http(self):
        bad_inputs = [
            ("abc", "text"),          # non-int chat_id
            (True, "text"),           # bool is not a chat id
            (0, "text"),              # zero chat id
            (1.5, "text"),            # float chat id
            (999, ""),                # empty text
            (999, "   "),             # blank text
            (999, None),              # non-str text
            (999, "x" * 4097),        # over Telegram's 4096 limit
        ]

        for chat_id, text in bad_inputs:
            integration, stub = make_integration()
            with self.assertRaises(ValueError, msg=f"{chat_id!r}/{text!r}"):
                integration.send_message(chat_id, text)
            self.assertEqual(stub.calls, [])

    def test_send_message_telegram_error_sanitised(self):
        integration, _ = make_integration(
            StubResponse(
                {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                status_code=400,
            )
        )

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.send_message(12345, "hello")

        self.assertEqual(ctx.exception.error_code, 400)
        self.assertIn("chat not found", str(ctx.exception))
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_unexpected_send_message_result_handled(self):
        integration, _ = make_integration(StubResponse({"ok": True, "result": {}}))

        with self.assertRaises(TelegramAPIError):
            integration.send_message(12345, "hello")


# --------------------------------------------------
# 6. Backend endpoints (TestClient, still fully mocked HTTP)
# --------------------------------------------------

class TestTelegramEndpoints(unittest.TestCase):
    """
    Exercises the FastAPI routes with a stubbed Telegram client so no
    real API call happens. ``get_integration`` is patched per-module so
    the global registry (and its real settings) is never touched.
    """

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.api import app

        cls.TestClient = TestClient
        cls.app = app

    def setUp(self):
        self.client = self.TestClient(self.app)

    # --- helpers ---

    def _connected_integration(self, *extra_responses):
        """Integration already verified via a stubbed getMe."""

        integration, stub = make_integration(*connect_responses(*extra_responses))
        integration.connect()
        return integration, stub

    # --- POST /api/integrations/telegram/connect ---

    def test_connect_endpoint_success_returns_honest_status(self):
        integration, _ = make_integration(*connect_responses())

        with patch("backend.api.get_integration", return_value=integration):
            response = self.client.post("/api/integrations/telegram/connect")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["connected"])
        self.assertTrue(body["available"])
        self.assertEqual(body["connection_info"]["bot_username"], "scamnet_intel_bot")
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_connect_endpoint_rejected_token_returns_502(self):
        integration, _ = make_integration(getme_unauthorized())

        with patch("backend.api.get_integration", return_value=integration):
            response = self.client.post("/api/integrations/telegram/connect")

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertEqual(detail["status"], "connection_failed")
        self.assertIn("Unauthorized", detail["message"])
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_connect_endpoint_unconfigured_returns_409(self):
        integration, _ = make_integration(token=None)

        with patch("backend.api.get_integration", return_value=integration):
            response = self.client.post("/api/integrations/telegram/connect")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "not_configured")

    # --- GET /api/telegram/messages ---

    def test_messages_requires_configuration(self):
        integration, _ = make_integration(token=None)

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.get("/api/telegram/messages")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "not_configured")

    def test_messages_requires_connection_first(self):
        # Configured but connect() was never called -> honest 409.
        integration, stub = make_integration()

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.get("/api/telegram/messages")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "not_connected")
        self.assertEqual(stub.calls, [])  # never reached Telegram

    def test_messages_returns_normalized_inbox(self):
        integration, _ = self._connected_integration(
            updates_ok(RAW_MESSAGE_UPDATE)
        )

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.get("/api/telegram/messages?limit=5&timeout=0")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(
            body["messages"][0],
            {
                "update_id": 100,
                "chat_id": 999888,
                "message_id": 5,
                "sender_id": 999888,
                "sender_username": "target_user",
                "text": (
                    "Dear SBI customer, your card is blocked: "
                    "http://sbi-fake.com"
                ),
                "timestamp": 1757000000,
            },
        )
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_messages_upstream_failure_returns_sanitised_502(self):
        integration, _ = self._connected_integration(
            StubResponse(
                {"ok": False, "error_code": 500, "description": "Internal Server Error"},
                status_code=500,
            )
        )

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.get("/api/telegram/messages")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["status"], "telegram_error")
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_messages_query_bounds_are_validated(self):
        integration, _ = self._connected_integration()

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            self.assertEqual(self.client.get("/api/telegram/messages?limit=0").status_code, 422)
            self.assertEqual(self.client.get("/api/telegram/messages?timeout=99").status_code, 422)

    # --- POST /api/telegram/send-test ---

    def test_send_test_success(self):
        integration, stub = self._connected_integration(
            StubResponse({
                "ok": True,
                "result": {
                    "message_id": 42,
                    "chat": {"id": 999888, "type": "private"},
                    "date": 1757000100,
                    "text": "SCAMNET connectivity test",
                },
            })
        )

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.post(
                "/api/telegram/send-test",
                json={"chat_id": 999888, "text": "SCAMNET connectivity test"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "sent", "chat_id": 999888, "message_id": 42},
        )
        self.assertTrue(stub.calls[-1]["url"].endswith("/sendMessage"))
        self.assertNotIn(FAKE_TOKEN, response.text)

    def test_send_test_validates_body(self):
        integration, stub = self._connected_integration()

        bad_bodies = [
            {"chat_id": "abc", "text": "hi"},        # non-numeric chat id
            {"chat_id": 0, "text": "hi"},            # zero chat id
            {"chat_id": 999, "text": ""},            # empty text
            {"chat_id": 999, "text": "   "},         # blank text
            {"chat_id": 999, "text": "x" * 4097},    # too long
            {"chat_id": 999},                        # missing text
        ]

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            for body in bad_bodies:
                response = self.client.post("/api/telegram/send-test", json=body)
                self.assertEqual(response.status_code, 422, msg=str(body)[:60])

        # Validation must happen BEFORE any Telegram call: only the
        # three connect() calls (getMe/deleteWebhook/setMyCommands).
        self.assertEqual(len(stub.calls), 3)
        for call in stub.calls:
            self.assertFalse(call["url"].endswith("/sendMessage"))

    def test_send_test_requires_connection_first(self):
        integration, stub = make_integration()

        with patch("backend.telegram_routes.get_integration", return_value=integration):
            response = self.client.post(
                "/api/telegram/send-test", json={"chat_id": 1, "text": "hi"}
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["status"], "not_connected")
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
