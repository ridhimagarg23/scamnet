"""
test_telegram_delivery.py
=========================
Regression tests for the Telegram delivery path - everything that has
to work for "the scammer messages the bot, the bot replies" to be true
on a real deployment:

* ``connect()`` clears a stale webhook (a registered webhook makes
  ``getUpdates`` answer HTTP 409 forever, which looks exactly like a
  mute bot) and refuses to claim a connection it cannot honour;
* ``/start`` - the message Telegram sends by itself when a chat opens a
  bot, and the operator's liveness probe - is answered WITHOUT the LLM,
  without opening a case, and in any of Telegram's three spellings
  (``/start``, ``/START``, ``/start@my_bot``);
* replies are length-checked and truncated on Telegram's own unit
  (UTF-16 code units - 4096 emoji are NOT 4096 characters);
* a rate-limited send (HTTP 429 + ``retry_after``) is retried once;
* dead credentials stop the loop loudly instead of hammering Telegram;
* fetch mode works even after ``stop()`` (regression: the worker-wide
  stop event used to suppress every fetch that followed a stop);
* the HTTP endpoints expose all of it honestly.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from integrations.telegram.client import (
    MAX_TEXT_LENGTH,
    TelegramAPIError,
    TelegramIntegration,
    truncate_for_telegram,
    utf16_length,
)
from tools.conversation_service import (
    START_GREETING,
    TelegramConversationService,
    is_start_command,
    normalize_command,
)
from tools.telegram_conversation_worker import (
    AUTH_FAILURE_LIMIT,
    TelegramConversationWorker,
    fetch_once,
    get_worker,
    reset_worker,
    start_worker,
)

from tests.test_telegram_conversation import (
    StubTelegramIntegration,
    build_service,
    make_update,
)


FAKE_TOKEN = "123456:TEST-FAKE-TOKEN-do-not-leak"


# --------------------------------------------------
# HTTP transport stubs (mirror tests/test_telegram_integration.py)
# --------------------------------------------------

class StubResponse:
    def __init__(self, payload, status_code=200, json_error=False):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not JSON")
        return self._payload


class StubHTTPClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        if not self._responses:
            raise AssertionError(f"no queued response for {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_settings(token=FAKE_TOKEN):
    return SimpleNamespace(
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_API_BASE="https://api.telegram.org",
    )


def getme_ok():
    return StubResponse({
        "ok": True,
        "result": {
            "id": 111222333,
            "is_bot": True,
            "first_name": "SCAMNET Intel",
            "username": "scamnet_intel_bot",
        },
    })


def ok_true():
    return StubResponse({"ok": True, "result": True})


def conflict_error():
    """Exactly what Telegram answers while a webhook is registered."""

    return StubResponse(
        {
            "ok": False,
            "error_code": 409,
            "description": (
                "Conflict: can't use getUpdates method while webhook is "
                "active"
            ),
        },
        status_code=409,
    )


# --------------------------------------------------
# 1. Webhook conflict (the classic "bot never replies")
# --------------------------------------------------

class TestWebhookHandling(unittest.TestCase):

    def test_connect_clears_a_stale_webhook(self):
        stub = StubHTTPClient(getme_ok(), ok_true(), ok_true())
        integration = TelegramIntegration(make_settings(), http_client=stub)

        integration.connect()

        methods = [call["url"].rsplit("/", 1)[-1] for call in stub.calls]
        self.assertEqual(methods, ["getMe", "deleteWebhook", "setMyCommands"])
        self.assertTrue(integration.is_connected())
        self.assertTrue(integration.get_connection_info()["webhook_cleared"])

    def test_connect_fails_honestly_when_the_webhook_cannot_be_cleared(self):
        """
        A webhook that cannot be removed would make getUpdates answer
        409 forever - so the connection must NOT be reported as usable.
        """

        stub = StubHTTPClient(getme_ok(), conflict_error())
        integration = TelegramIntegration(make_settings(), http_client=stub)

        with self.assertRaises(TelegramAPIError) as ctx:
            integration.connect()

        self.assertIn("webhook", str(ctx.exception).lower())
        self.assertFalse(integration.is_connected())
        self.assertEqual(integration.get_status().state.value, "error")
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))

    def test_set_my_commands_failure_does_not_block_connect(self):
        """Publishing the command menu is cosmetic - never fatal."""

        stub = StubHTTPClient(
            getme_ok(),
            ok_true(),
            StubResponse(
                {"ok": False, "error_code": 500, "description": "boom"},
                status_code=500,
            ),
        )
        integration = TelegramIntegration(make_settings(), http_client=stub)

        integration.connect()

        self.assertTrue(integration.is_connected())

    def test_get_updates_reports_pending_mail(self):
        """The pending-backlog flag drives the worker's diagnostics."""

        stub = StubHTTPClient(
            getme_ok(),
            ok_true(),
            ok_true(),
            StubResponse({"ok": True, "result": []}),
        )
        integration = TelegramIntegration(make_settings(), http_client=stub)
        integration.connect()

        integration.get_updates(timeout=0, limit=10, ack=True)

        self.assertFalse(integration.last_poll_had_pending)


# --------------------------------------------------
# 2. UTF-16 length handling (Telegram's real limit)
# --------------------------------------------------

class TestUtf16Limits(unittest.TestCase):

    def test_utf16_length_counts_code_units_not_characters(self):
        self.assertEqual(utf16_length("abc"), 3)
        # One emoji = two UTF-16 code units.
        self.assertEqual(utf16_length("\U0001F600"), 2)
        self.assertEqual(utf16_length("\U0001F600" * 5), 10)

    def test_truncation_is_a_noop_below_the_limit(self):
        self.assertEqual(truncate_for_telegram("hello"), "hello")

    def test_truncation_respects_the_utf16_budget(self):
        text = "\U0001F600" * 5000  # 10 000 UTF-16 units

        truncated = truncate_for_telegram(text, MAX_TEXT_LENGTH)

        self.assertLessEqual(utf16_length(truncated), MAX_TEXT_LENGTH)
        self.assertTrue(truncated.endswith("\u2026"))
        # Never cut a surrogate pair in half.
        truncated.encode("utf-16-le")

    def test_send_message_rejects_emoji_over_the_limit(self):
        stub = StubHTTPClient()
        integration = TelegramIntegration(make_settings(), http_client=stub)

        with self.assertRaises(ValueError):
            integration.send_message(1, "\U0001F600" * 3000)

        self.assertEqual(stub.calls, [])  # no network call was made

    def test_send_message_accepts_text_inside_the_budget(self):
        stub = StubHTTPClient(StubResponse({
            "ok": True,
            "result": {"message_id": 7, "chat": {"id": 1}},
        }))
        integration = TelegramIntegration(make_settings(), http_client=stub)

        result = integration.send_message(1, "नमस्ते \U0001F44B")

        self.assertEqual(result["message_id"], 7)


# --------------------------------------------------
# 3. Chat actions / command menu
# --------------------------------------------------

class TestBotApiOperations(unittest.TestCase):

    def test_send_chat_action(self):
        stub = StubHTTPClient(ok_true())
        integration = TelegramIntegration(make_settings(), http_client=stub)

        self.assertTrue(integration.send_chat_action(42, "typing"))
        self.assertTrue(stub.calls[0]["url"].endswith("/sendChatAction"))
        self.assertEqual(stub.calls[0]["json"], {"chat_id": 42, "action": "typing"})

    def test_send_chat_action_validates_input(self):
        integration = TelegramIntegration(make_settings(), http_client=StubHTTPClient())

        with self.assertRaises(ValueError):
            integration.send_chat_action(0)
        with self.assertRaises(ValueError):
            integration.send_chat_action(1, "   ")

    def test_set_my_commands_validates_and_sends(self):
        stub = StubHTTPClient(ok_true())
        integration = TelegramIntegration(make_settings(), http_client=stub)

        self.assertTrue(
            integration.set_my_commands([
                {"command": "start", "description": "Check the bot is online"},
            ])
        )
        self.assertEqual(
            stub.calls[0]["json"]["commands"][0]["command"], "start"
        )

        for bad in ([], [{"command": "/start"}], ["start"]):
            with self.assertRaises(ValueError):
                integration.set_my_commands(bad)


# --------------------------------------------------
# 4. /start command parsing + service handling
# --------------------------------------------------

class TestStartCommand(unittest.TestCase):

    def test_command_normalization(self):
        cases = {
            "/start": "/start",
            "/START": "/start",
            "/Start": "/start",
            "/start@scamnet_intel_bot": "/start",
            "/start hello there": "/start",
            "  /start  ": "/start",
            "/stop": "/stop",
            "start": "",
            "hello": "",
            "": "",
        }

        for text, expected in cases.items():
            self.assertEqual(normalize_command(text), expected, msg=text)

        self.assertTrue(is_start_command("/start@scamnet_intel_bot"))
        self.assertFalse(is_start_command("Hi, I have a question"))

    def test_handle_start_does_not_open_a_case(self):
        """Greeting must never create case state (Telegram auto-sends it)."""

        service = TelegramConversationService(archive=False)

        result = service.handle_start(424242)

        self.assertEqual(result["kind"], "start")
        self.assertEqual(result["reply"], START_GREETING)
        self.assertEqual(result["turn"], 0)
        self.assertEqual(service.active_chat_ids(), [])

    def test_greeting_keeps_the_cover(self):
        """The greeting must not reveal an investigation to a scammer."""

        lowered = START_GREETING.lower()

        for leak in ("scam", "investigat", "evidence", "traceai", "persona"):
            self.assertNotIn(leak, lowered)

    def test_handle_start_validates_chat_id(self):
        service = TelegramConversationService(archive=False)

        for bad in (0, "42", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                service.handle_start(bad)


# --------------------------------------------------
# 5. Worker behaviour
# --------------------------------------------------

class TestWorkerDelivery(unittest.TestCase):

    def setUp(self):
        reset_worker()

    def tearDown(self):
        reset_worker()

    def test_start_command_is_answered_without_the_llm(self):
        """
        /start must work even when the LLM provider is down: it never
        reaches the conversation agent.
        """

        class ExplodingService(TelegramConversationService):
            def __init__(self):
                super().__init__(archive=False)

            def handle_message(self, chat_id, text, sender_username=None):
                raise AssertionError("the LLM path must not run for /start")

        integration = StubTelegramIntegration(
            updates=[make_update(text="/start")]
        )
        worker = TelegramConversationWorker(
            integration, service=ExplodingService(), poll_timeout=0
        )

        worker.run_once()

        self.assertEqual(len(integration.sent), 1)
        self.assertEqual(integration.sent[0]["text"], START_GREETING)
        self.assertEqual(worker.greetings, 1)
        self.assertTrue(worker.wake_message_sent)
        self.assertEqual(worker.failed, 0)

    def test_group_style_start_command_is_also_answered(self):
        integration = StubTelegramIntegration(
            updates=[make_update(text="/START@scamnet_intel_bot")]
        )
        worker = TelegramConversationWorker(
            integration,
            service=TelegramConversationService(archive=False),
            poll_timeout=0,
        )

        worker.run_once()

        self.assertEqual(integration.sent[0]["text"], START_GREETING)

    def test_start_does_not_touch_case_state(self):
        service, investigation_agent, _, _ = build_service()
        integration = StubTelegramIntegration(
            updates=[make_update(text="/start")]
        )
        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        worker.run_once()

        self.assertEqual(service.active_chat_ids(), [])
        self.assertEqual(investigation_agent.calls, [])

    def test_typing_action_is_sent_before_the_reply(self):
        service, _, _, _ = build_service(replies=["Kaise verify karu?"])
        integration = StubTelegramIntegration(
            updates=[make_update(text="Your card is blocked")]
        )
        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        worker.run_once()

        self.assertEqual(
            integration.chat_actions,
            [{"chat_id": 42, "action": "typing"}],
        )
        self.assertEqual(len(integration.sent), 1)

    def test_over_long_reply_is_truncated_on_the_utf16_budget(self):
        long_reply = "\U0001F600" * 3000  # 6000 UTF-16 units
        service, _, _, _ = build_service(replies=[long_reply])
        integration = StubTelegramIntegration(updates=[make_update()])

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )
        handled = worker.run_once()

        self.assertEqual(handled, 1)  # delivered, not dropped
        delivered = integration.sent[0]["text"]
        self.assertLessEqual(utf16_length(delivered), MAX_TEXT_LENGTH)
        self.assertTrue(delivered.endswith("\u2026"))

    def test_rate_limited_send_is_retried_once(self):
        """Telegram 429 + retry_after must not lose the persona reply."""

        class RateLimitedThenFine(StubTelegramIntegration):
            def send_message(self, chat_id, text):
                self.attempts = getattr(self, "attempts", 0) + 1
                if self.attempts == 1:
                    raise TelegramAPIError(
                        "Telegram Bot API error on 'sendMessage' "
                        "(code 429): Too Many Requests",
                        error_code=429,
                        retry_after=1,
                    )
                return super().send_message(chat_id, text)

        service, _, _, _ = build_service(replies=["Persona reply"])
        integration = RateLimitedThenFine(updates=[make_update()])

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )
        worker.run_once()

        self.assertEqual(integration.attempts, 2)
        self.assertEqual(integration.sent[0]["text"], "Persona reply")
        self.assertEqual(worker.processed, 1)
        self.assertEqual(worker.failed, 0)

    def test_non_rate_limit_send_errors_are_not_retried(self):
        service, _, _, _ = build_service(replies=["Persona reply"])
        integration = StubTelegramIntegration(
            updates=[make_update()],
            send_error=TelegramAPIError(
                "Telegram Bot API error on 'sendMessage' (code 400): "
                "Bad Request: chat not found",
                error_code=400,
            ),
        )

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )
        worker.run_once()

        self.assertEqual(worker.failed, 1)
        self.assertEqual(integration.sent, [])
        self.assertEqual(worker.errors[0]["stage"], "send")

    def test_revoked_token_stops_the_loop(self):
        """Dead credentials stop the worker loudly (no infinite 409s)."""

        class AlwaysUnauthorized(StubTelegramIntegration):
            def get_updates(self, timeout=None, limit=None, ack=None):
                raise TelegramAPIError(
                    "Telegram Bot API error on 'getUpdates' (code 401): "
                    "Unauthorized",
                    error_code=401,
                )

        integration = AlwaysUnauthorized()
        worker = TelegramConversationWorker(
            integration,
            service=TelegramConversationService(archive=False),
            poll_timeout=0,
            idle_sleep=0.01,
            error_backoff=0.01,
        )

        self.assertTrue(worker.start())
        worker._thread.join(timeout=10)

        self.assertFalse(worker.is_running())
        self.assertEqual(
            worker.stopped_reason, "telegram_authorization_failed"
        )
        self.assertEqual(worker.consecutive_failures, AUTH_FAILURE_LIMIT)

    def test_pending_backlog_is_answered_and_recorded(self):
        """
        Mail that arrived while nothing was polling (fresh deploy, a
        restart, a host that slept) must still be answered - including
        an automatic Telegram /start, which gets the greeting instead of
        a persona line.
        """

        class PendingIntegration(StubTelegramIntegration):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # The client marks the first poll of a fresh process.
                self.last_poll_had_pending = False

            def get_updates(self, timeout=None, limit=None, ack=None):
                batch = super().get_updates(timeout=timeout, limit=limit, ack=ack)
                self.last_poll_had_pending = bool(batch)
                return batch

        service, _, _, _ = build_service(replies=["Persona reply"])
        integration = PendingIntegration(
            updates=[
                make_update(update_id=1, text="/start"),
                make_update(update_id=2, text="Your card is blocked"),
            ]
        )
        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        handled = worker.run_once()

        self.assertEqual(handled, 1)  # /start is a greeting, not a turn
        self.assertTrue(worker.pending_backlog_seen)
        self.assertEqual(worker.greetings, 1)
        self.assertEqual(
            [message["text"] for message in integration.sent],
            [START_GREETING, "Persona reply"],
        )

    def test_stats_expose_diagnostics(self):
        service, _, _, _ = build_service(replies=["ok"])
        integration = StubTelegramIntegration(updates=[make_update()])
        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )
        worker.run_once()

        stats = worker.stats()

        for key in (
            "running", "polls", "processed", "failed", "greetings",
            "last_poll_at", "last_delivery", "stopped_reason",
            "poll_timeout", "poll_limit", "active_chats",
        ):
            self.assertIn(key, stats)

        diagnostics = worker.diagnostics()
        self.assertIn("stats", diagnostics)
        self.assertIn("recent_deliveries", diagnostics)
        self.assertEqual(diagnostics["recent_deliveries"][0]["text"], "ok")

    def test_delivered_history_is_bounded(self):
        service, _, _, _ = build_service()
        integration = StubTelegramIntegration()
        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        for index in range(130):
            integration.enqueue(make_update(update_id=index + 1))
            worker.run_once()

        self.assertLessEqual(len(worker.delivered), 100)


# --------------------------------------------------
# 6. Fetch mode
# --------------------------------------------------

class TestFetchMode(unittest.TestCase):

    def setUp(self):
        reset_worker()

    def tearDown(self):
        reset_worker()

    def _fetch(self, integration, service):
        """Run one fetch against a service we control (no network)."""

        with patch(
            "tools.telegram_conversation_worker.get_conversation_service",
            return_value=service,
        ):
            return fetch_once(integration)

    def test_fetch_answers_a_queued_message(self):
        service, _, _, _ = build_service(replies=["Persona reply"])
        integration = StubTelegramIntegration(
            updates=[make_update(text="card blocked")]
        )

        result = self._fetch(integration, service)

        self.assertEqual(result["handled"], 1)
        self.assertEqual(integration.sent[0]["text"], "Persona reply")

    def test_fetch_keeps_working_after_stop(self):
        """
        Regression: the worker-wide stop event used to suppress every
        message handled after stop(), so fetch mode silently answered
        nothing once the polling loop had been stopped.
        """

        service, _, _, _ = build_service(replies=["reply one", "reply two"])
        integration = StubTelegramIntegration(
            updates=[make_update(text="before stop")]
        )

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0, idle_sleep=0.01
        )
        self.assertTrue(worker.start())

        deadline = time.time() + 5.0
        while time.time() < deadline and not integration.sent:
            time.sleep(0.01)

        self.assertTrue(worker.stop(timeout=5.0))
        self.assertEqual(integration.sent[0]["text"], "reply one")

        # Fetch mode must keep answering after the thread was stopped.
        integration.enqueue(make_update(update_id=99, text="after stop"))

        result = self._fetch(integration, service)

        self.assertEqual(result["handled"], 1)
        self.assertEqual(integration.sent[1]["text"], "reply two")

    def test_fetch_does_not_reanswer_acknowledged_updates(self):
        service, _, _, _ = build_service(replies=["one"])
        integration = StubTelegramIntegration(
            updates=[make_update(text="first")]
        )

        first = self._fetch(integration, service)
        second = self._fetch(integration, service)

        self.assertEqual(first["handled"], 1)
        self.assertEqual(second["handled"], 0)
        self.assertEqual(len(integration.sent), 1)

    def test_fetch_refuses_nothing_but_reports_llm_failures(self):
        """A failing agent is recorded per message - never raised away."""

        class ExplodingService:
            def handle_message(self, chat_id, text, sender_username=None):
                raise RuntimeError("LLM provider down")

            def handle_start(self, chat_id, sender_username=None):
                return {"chat_id": chat_id, "reply": "hi"}

            def active_chat_ids(self):
                return []

        integration = StubTelegramIntegration(
            updates=[make_update(text="hello")]
        )

        result = self._fetch(integration, ExplodingService())

        self.assertEqual(result["handled"], 0)
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertIn("LLM provider down", result["stats"]["last_error"])


# --------------------------------------------------
# 7. HTTP endpoints
# --------------------------------------------------

class TestDeliveryEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from backend.api import app

        cls.TestClient = TestClient
        cls.app = app

    def setUp(self):
        self.client = self.TestClient(self.app)
        reset_worker()

    def tearDown(self):
        reset_worker()

    def test_wake_endpoint_sends_the_greeting(self):
        integration = StubTelegramIntegration()

        with patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ), patch(
            "backend.telegram_routes.get_conversation_service",
            return_value=_real_service(),
        ):
            response = self.client.post(
                "/api/telegram/conversation/wake", json={"chat_id": 4242}
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "sent")
        self.assertEqual(integration.sent[0]["text"], START_GREETING)
        self.assertTrue(body["message_id"])

    def test_wake_endpoint_validates_chat_id(self):
        integration = StubTelegramIntegration()

        with patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ):
            response = self.client.post(
                "/api/telegram/conversation/wake", json={"chat_id": 0}
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(integration.sent, [])

    def test_fetch_endpoint_polls_once(self):
        integration = StubTelegramIntegration(
            updates=[make_update(text="card blocked")]
        )
        service, _, _, _ = build_service(replies=["Persona reply"])

        with patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ), patch(
            "backend.telegram_routes.fetch_once",
            side_effect=lambda integ: fetch_once(integ),
        ), patch(
            "tools.telegram_conversation_worker.get_conversation_service",
            return_value=service,
        ):
            response = self.client.post("/api/telegram/conversation/fetch")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "fetched")
        self.assertEqual(body["handled"], 1)
        self.assertIn("llm_configured", body)

    def test_manual_turn_answers_start_without_the_llm(self):
        integration = StubTelegramIntegration()

        with patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ), patch(
            "backend.telegram_routes.get_conversation_service",
            return_value=_real_service(),
        ), patch(
            "backend.telegram_routes._require_llm_configured"
        ) as llm_guard:
            response = self.client.post(
                "/api/telegram/conversation/message",
                json={"chat_id": 4242, "text": "/start"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "greeted")
        # The LLM guard is never even consulted for /start.
        llm_guard.assert_not_called()

    def test_status_reports_delivery_mode_and_diagnostics(self):
        response = self.client.get("/api/telegram/conversation/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(body["delivery_mode"], ("push", "fetch"))
        self.assertIn("llm_configured", body)
        self.assertIn("connection_info", body["telegram"])

    def test_connect_auto_starts_the_reply_loop(self):
        """
        THE real-world bug: connecting the bot used to leave it mute,
        because a second endpoint had to be called before it polled.
        """

        integration = StubTelegramIntegration()

        with patch(
            "backend.api.get_integration", return_value=integration
        ), patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ):
            response = self.client.post("/api/integrations/telegram/connect")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertTrue(body["connected"])
        self.assertIsNotNone(body["worker"])
        self.assertTrue(body["worker"]["running"])

        # ...and it really polls: a queued message is answered and sent.
        service, _, _, _ = build_service(replies=["Persona reply"])
        worker = get_worker()
        worker.service = service

        integration.enqueue(make_update(text="Your card is blocked"))

        deadline = time.time() + 10.0
        while time.time() < deadline and not integration.sent:
            time.sleep(0.05)

        self.assertEqual(integration.sent[0]["text"], "Persona reply")

    def test_disconnect_stops_the_reply_loop(self):
        integration = StubTelegramIntegration()
        worker = start_worker(
            integration,
            service=TelegramConversationService(archive=False),
            poll_timeout=0,
            idle_sleep=0.01,
        )

        with patch(
            "backend.api.get_integration", return_value=integration
        ), patch(
            "backend.telegram_routes.get_integration", return_value=integration
        ):
            response = self.client.post(
                "/api/integrations/telegram/disconnect"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["connected"])
        self.assertFalse(worker.is_running())
        self.assertFalse(response.json()["worker"]["running"])

    def test_auto_start_can_be_disabled(self):
        """TELEGRAM_AUTO_START_WORKER=0 keeps the loop manual."""

        integration = StubTelegramIntegration()

        with patch(
            "backend.api.get_integration", return_value=integration
        ), patch(
            "backend.api.settings"
        ) as fake_settings:
            fake_settings.TELEGRAM_AUTO_START_WORKER = False
            response = self.client.post("/api/integrations/telegram/connect")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["worker"])
        self.assertIsNone(get_worker())

    def test_boot_bootstrap_is_silent_without_a_token(self):
        """No token at boot: log it, start nothing, never raise."""

        from backend import api as api_module

        fake = SimpleNamespace(
            is_configured=lambda: False,
            is_connected=lambda: False,
        )

        with patch(
            "backend.api.get_integration", return_value=fake
        ), patch(
            "backend.api.start_worker"
        ) as start:
            api_module._bootstrap_telegram_on_boot()

        start.assert_not_called()

    def test_boot_bootstrap_connects_and_starts_when_configured(self):
        """A working token at boot brings the bot up with no clicks."""

        from backend import api as api_module

        connected = {"value": False}

        class BootIntegration(StubTelegramIntegration):
            def connect(self):
                connected["value"] = True
                self._connected = True

            def is_configured(self):
                return True

            def is_connected(self):
                return connected["value"]

        integration = BootIntegration()

        with patch(
            "backend.api.get_integration", return_value=integration
        ), patch(
            "backend.api.settings"
        ) as fake_settings:
            fake_settings.TELEGRAM_AUTO_START_WORKER = True

            api_module._bootstrap_telegram_on_boot()

        self.assertTrue(connected["value"])
        worker = get_worker()
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_running())
        worker.stop()

    def test_commands_endpoint_documents_start(self):
        response = self.client.get("/api/telegram/conversation/commands")

        self.assertEqual(response.status_code, 200)
        commands = response.json()["commands"]
        self.assertTrue(
            any(entry["command"] == "/start" for entry in commands)
        )
        self.assertFalse(commands[0]["needs_llm"])


def _real_service() -> TelegramConversationService:
    """A real service with no collaborators (greeting needs none)."""

    return TelegramConversationService(archive=False)


# --------------------------------------------------
# 8. Memory archive under concurrent writers
# --------------------------------------------------

class TestMemoryArchiveConcurrency(unittest.TestCase):

    def test_concurrent_saves_never_lose_records(self):
        """
        The API and the Telegram worker archive from different threads;
        the read-modify-write cycle used to interleave and drop records.
        """

        import tempfile
        import threading
        from pathlib import Path

        from tools.memory_manager import MemoryManager

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "threat_memory.json"
            archive.write_text("[]", encoding="utf-8")

            class TempMemoryManager(MemoryManager):
                """Same code, same module-level lock, temp archive."""

                def __init__(self):
                    self.memory_file = archive

            def writer(index):
                for turn in range(10):
                    TempMemoryManager().save({
                        "case": index,
                        "turn": turn,
                        "threat_type": "Banking Phishing",
                    })

            threads = [
                threading.Thread(target=writer, args=(index,))
                for index in range(4)
            ]

            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            records = json.loads(archive.read_text(encoding="utf-8"))

            self.assertEqual(len(records), 40)

    def test_corrupt_archive_is_preserved_not_crashed_on(self):
        import tempfile
        from pathlib import Path

        from tools.memory_manager import MemoryManager

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "threat_memory.json"
            archive.write_text("{ not json", encoding="utf-8")

            class TempMemoryManager(MemoryManager):
                def __init__(self):
                    self.memory_file = archive

            manager = TempMemoryManager()

            self.assertEqual(manager.load(), [])

            manager.save({"threat_type": "Banking Phishing"})

            records = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(len(records), 1)

            backups = list(Path(tmp).glob("threat_memory.corrupt-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertIn("not json", backups[0].read_text())


if __name__ == "__main__":
    unittest.main()
