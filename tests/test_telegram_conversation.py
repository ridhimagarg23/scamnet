"""
test_telegram_conversation.py
=============================
Offline tests for the Phase 2A Telegram <-> ConversationAgent loop.

Covers the four pieces that close the loop and the HTTP surface that
drives them:

* ``prompts/telegram_assistant_prompt.txt``   - the Telegram system rules
* ``agents/conversation_agent.py``           - ``run_telegram()``
* ``tools/conversation_service.py``          - per-chat conversation state
* ``tools/telegram_conversation_worker.py``  - polling / delivery plumbing
* ``backend/telegram_routes.py``             - /conversation/* endpoints

Everything runs offline: the LLM is patched (or replaced by a stub
agent), Telegram is replaced by a stub integration and the archive is
a stub MemoryManager. No network call, no real token, no files written.

Run with:

    OPENROUTER_API_KEY=test-key python -m unittest discover -s tests
"""

import unittest
from unittest.mock import patch

from agents.conversation_agent import (
    TELEGRAM_PROMPT_FILE,
    ConversationAgent,
)
from integrations.telegram import IncomingMessage

from tools.adaptive_investigation_engine import (
    InvestigationProfile,
    InvestigationState,
)
from tools.conversation_service import TelegramConversationService
from tools.prompt_loader import PromptLoader
from tools.telegram_conversation_worker import (
    TelegramConversationWorker,
    reset_worker,
)

from utils.schemas import (
    ConversationResult,
    InvestigationResult,
)


# --------------------------------------------------
# Factories & stubs
# --------------------------------------------------

def make_investigation(**overrides) -> InvestigationResult:
    """A believable banking-phish verdict (fields overridable)."""

    data = {
        "is_scam": True,
        "confidence": 95,
        "threat_type": "Banking Phishing",
        "summary": "Fake SBI alert pushing a verification link.",
        "risk_score": 80,
        "risk_level": "HIGH",
    }

    data.update(overrides)

    return InvestigationResult(**data)


def make_state(
    objective="Collect official verification website",
    strategy="Curious",
) -> InvestigationState:
    """A ready-made InvestigationState for direct agent calls."""

    return InvestigationState(
        turn_number=1,
        current_objective=objective,
        current_strategy=strategy,
        profile=InvestigationProfile(
            language="Hinglish",
            communication_style="Polite",
            digital_literacy="Medium",
        ),
    )


def make_update(
    update_id=1,
    chat_id=42,
    text="Your SBI account is blocked. Verify now.",
    username="scammer_bot",
) -> IncomingMessage:
    """One normalized inbound Telegram message."""

    return IncomingMessage(
        update_id=update_id,
        chat_id=chat_id,
        message_id=100 + update_id,
        sender_id=777,
        sender_username=username,
        text=text,
        timestamp=1700000000,
    )


class StubInvestigationAgent:
    """
    Stands in for ``InvestigationAgent``.

    Returns a deep copy of the queued verdicts (last one repeats) so a
    test can script a different verdict per turn without the LLM.
    """

    def __init__(self, results=None):

        self._results = list(results or [])
        self.calls = []

    def run(self, message):

        self.calls.append(message)

        if not self._results:
            verdict = make_investigation()
        elif len(self._results) > 1:
            verdict = self._results.pop(0)
        else:
            verdict = self._results[0]

        # Deep copy: merge_investigations mutates the accumulated
        # result, so a shared instance would leak between turns.
        return verdict.model_copy(deep=True)


class StubConversationAgent:
    """Stands in for ``ConversationAgent.run_telegram()``."""

    def __init__(self, replies=None):

        self._replies = list(replies or [])
        self.calls = []

    def run_telegram(
        self,
        investigation,
        investigation_state,
        latest_message,
        conversation_history="",
        chat_id=None,
        sender_username=None,
    ):

        self.calls.append(
            {
                "investigation": investigation,
                "investigation_state": investigation_state,
                "latest_message": latest_message,
                "conversation_history": conversation_history,
                "chat_id": chat_id,
                "sender_username": sender_username,
            }
        )

        if not self._replies:
            reply = "Kaise verify karu? Link bhej do."
        elif len(self._replies) > 1:
            reply = self._replies.pop(0)
        else:
            reply = self._replies[0]

        return ConversationResult(
            reply=reply,
            objective=investigation_state.current_objective,
            expected_outcome="Scammer explains the verification step.",
        )


class StubMemoryManager:
    """Captures archived case facts instead of writing a JSON file."""

    def __init__(self):

        self.saved = []

    def save(self, investigation):

        self.saved.append(investigation)


class StubTelegramIntegration:
    """
    Minimal stand-in for a connected ``TelegramIntegration``.

    Queued updates are returned by the first ``get_updates()`` call
    (later polls get nothing), and every send is recorded.
    """

    def __init__(self, updates=None, connected=True, send_error=None):

        self._updates = list(updates or [])
        self._connected = connected
        self.send_error = send_error

        self.sent = []
        self.get_updates_calls = []

    def is_configured(self):
        return True

    def is_connected(self):
        return self._connected

    def connect(self):
        """Stub handshake used by the auto-start tests."""

        self._connected = True
        self.connect_calls = getattr(self, "connect_calls", 0) + 1

    def disconnect(self):
        self._connected = False

    def get_status(self):
        """Real IntegrationStatus so the connect/disconnect endpoints run."""

        from integrations.base import IntegrationState, IntegrationStatus

        return IntegrationStatus(
            id="telegram",
            name="Telegram",
            purpose="Communication & intelligence gathering",
            configured=True,
            available=True,
            connected=self._connected,
            state=(
                IntegrationState.CONNECTED
                if self._connected
                else IntegrationState.DISCONNECTED
            ),
            detail="stub",
            setup_instructions="set TELEGRAM_BOT_TOKEN",
            connection_info=self.get_connection_info(),
        )

    def get_connection_info(self):
        if not self._connected:
            return {}
        return {
            "bot_username": "scamnet_intel_bot",
            "webhook_cleared": True,
        }

    def send_chat_action(self, chat_id, action="typing"):
        self.chat_actions = getattr(self, "chat_actions", [])
        self.chat_actions.append({"chat_id": chat_id, "action": action})
        return True

    def get_updates(self, timeout=None, limit=None, ack=None):

        self.get_updates_calls.append(
            {"timeout": timeout, "limit": limit, "ack": ack}
        )

        updates, self._updates = self._updates, []

        return updates

    def enqueue(self, *updates):
        """Queue updates for the next ``get_updates()`` call."""

        self._updates.extend(updates)

    def send_message(self, chat_id, text):

        if self.send_error is not None:
            raise self.send_error

        self.sent.append({"chat_id": chat_id, "text": text})

        return {
            "chat_id": chat_id,
            "message_id": 500 + len(self.sent),
        }


class StubConversationService:
    """Stands in for the shared service inside the HTTP tests."""

    def __init__(self, chat_ids=None):

        self._chat_ids = list(chat_ids or [])
        self.calls = []
        self.reset_calls = []

    def handle_message(self, chat_id, text, sender_username=None):

        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "sender_username": sender_username,
            }
        )

        return {
            "chat_id": chat_id,
            "reply": "Stub persona reply",
            "objective": "Collect official verification website",
            "expected_outcome": "Scammer sends the link.",
            "is_scam": True,
            "confidence": 95,
            "threat_type": "Banking Phishing",
            "risk_score": 80,
            "risk_level": "HIGH",
            "strategy": "Curious",
            "current_objective": "Collect official verification website",
            "turn": len(self.calls),
            "sender_username": sender_username,
            "message": text,
        }

    def active_chat_ids(self):
        return list(self._chat_ids)

    def summary(self, chat_id):

        if chat_id not in self._chat_ids:
            return None

        return {
            "chat_id": chat_id,
            "turn": 2,
            "threat_type": "Banking Phishing",
            "current_objective": "Collect employee ID",
            "strategy": "Confused",
            "risk_score": 80,
            "risk_level": "HIGH",
            "messages": 4,
        }

    def reset(self, chat_id):

        self.reset_calls.append(chat_id)

        if chat_id in self._chat_ids:
            self._chat_ids.remove(chat_id)
            return True

        return False


class FakeWorker:
    """Pre-baked worker stats for the start/stop/status endpoints."""

    def __init__(self, running=True):

        self._running = running

    def is_running(self):
        return self._running

    def stats(self):
        return {
            "running": self._running,
            "polls": 7,
            "processed": 3,
            "skipped": 1,
            "failed": 0,
            "error_count": 0,
            "last_error": None,
            "active_chats": [42],
        }


def build_service(
    investigation_results=None,
    replies=None,
    archive=True,
) -> tuple:
    """A real service wired to stub agents (no LLM, no filesystem)."""

    investigation_agent = StubInvestigationAgent(
        investigation_results
    )
    conversation_agent = StubConversationAgent(replies)
    memory_manager = StubMemoryManager()

    service = TelegramConversationService(
        investigation_agent=investigation_agent,
        conversation_agent=conversation_agent,
        memory_manager=memory_manager,
        archive=archive,
    )

    return service, investigation_agent, conversation_agent, memory_manager


# --------------------------------------------------
# 1. The Telegram system prompt
# --------------------------------------------------

class TestTelegramAssistantPrompt(unittest.TestCase):

    def test_telegram_prompt_file_loads(self):
        """The Telegram prompt is loadable and non-trivial."""

        prompt = PromptLoader.load(TELEGRAM_PROMPT_FILE)

        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 500)
        self.assertIn("Telegram", prompt)

    def test_telegram_prompt_encodes_safety_and_output_contract(self):
        """
        The prompt must pin the safety rules and the JSON contract the
        service relies on (``reply``/``objective``/``expected_outcome``).
        """

        prompt = PromptLoader.load(TELEGRAM_PROMPT_FILE)

        # Safety rules.
        self.assertIn("Never reveal that you are an AI", prompt)
        self.assertIn("Never send OTPs", prompt)
        self.assertIn(
            "Safety always outranks intelligence gathering", prompt
        )

        # Telegram register + output contract.
        self.assertIn("Keep replies under 35 words", prompt)
        self.assertIn('"reply"', prompt)
        self.assertIn('"objective"', prompt)
        self.assertIn('"expected_outcome"', prompt)
        self.assertIn("Return ONLY JSON", prompt)

    def test_telegram_prompt_is_distinct_from_dashboard_prompt(self):
        """
        The Telegram channel must not silently reuse the dashboard
        prompt - the two have different system rules.
        """

        telegram_prompt = PromptLoader.load(TELEGRAM_PROMPT_FILE)
        dashboard_prompt = PromptLoader.load(
            "conversation_prompt.txt"
        )

        self.assertNotEqual(telegram_prompt, dashboard_prompt)
        self.assertIn(
            "Telegram Conversation Assistant", telegram_prompt
        )
        self.assertNotIn(
            "Conversation Generator", telegram_prompt
        )


# --------------------------------------------------
# 2. ConversationAgent Telegram mode
# --------------------------------------------------

class TestConversationAgentTelegramMode(unittest.TestCase):

    @patch("llm.llm_client.LLMClient.generate")
    def test_run_telegram_returns_validated_conversation_result(
        self, mock_generate
    ):
        """run_telegram() produces the same validated schema as run()."""

        mock_generate.return_value = {
            "reply": "Kaun se branch se bol rahe hain?",
            "objective": "Collect employee ID",
            "expected_outcome": "Scammer names a branch.",
        }

        agent = ConversationAgent()

        result = agent.run_telegram(
            investigation=make_investigation(),
            investigation_state=make_state(),
            latest_message="Verify your account now.",
        )

        self.assertIsInstance(result, ConversationResult)
        self.assertEqual(
            result.reply, "Kaun se branch se bol rahe hain?"
        )
        self.assertEqual(result.objective, "Collect employee ID")

        # JSON mode is still requested for the Telegram channel.
        self.assertTrue(
            mock_generate.call_args.kwargs["json_output"]
        )

    @patch("llm.llm_client.LLMClient.generate")
    def test_run_telegram_uses_the_telegram_system_prompt(
        self, mock_generate
    ):
        """
        The Telegram prompt (not the dashboard one) must be the system
        block of the generated prompt.
        """

        mock_generate.return_value = {
            "reply": "Theek hai, bataiye.",
            "objective": "Collect employee ID",
            "expected_outcome": "Scammer explains more.",
        }

        agent = ConversationAgent()

        agent.run_telegram(
            investigation=make_investigation(),
            investigation_state=make_state(),
            latest_message="Send OTP to unblock.",
        )

        prompt = mock_generate.call_args[0][0]

        self.assertIn("Telegram Conversation Assistant", prompt)
        self.assertIn("TELEGRAM CHANNEL CONTEXT", prompt)
        self.assertNotIn("Conversation Generator", prompt)

        # The shared context blocks are still present.
        self.assertIn("INVESTIGATION RESULT", prompt)
        self.assertIn("CURRENT OBJECTIVE", prompt)
        self.assertIn("CONVERSATION HISTORY", prompt)

    @patch("llm.llm_client.LLMClient.generate")
    def test_run_telegram_injects_channel_context(
        self, mock_generate
    ):
        """chat_id / sender handle reach the prompt; None is 'unknown'."""

        mock_generate.return_value = {
            "reply": "Ji, boliye.",
            "objective": "Collect employee ID",
            "expected_outcome": "Scammer replies.",
        }

        agent = ConversationAgent()

        agent.run_telegram(
            investigation=make_investigation(),
            investigation_state=make_state(),
            latest_message="Hello",
            chat_id=4242,
            sender_username="@fake_sbi_support",
        )

        prompt = mock_generate.call_args[0][0]

        self.assertIn("Telegram", prompt)
        self.assertIn("4242", prompt)
        self.assertIn("@fake_sbi_support", prompt)

        # Unknown channel metadata degrades gracefully.
        agent._telegram_prompt = None
        agent.run_telegram(
            investigation=make_investigation(),
            investigation_state=make_state(),
            latest_message="Hello again",
        )

        second_prompt = mock_generate.call_args[0][0]

        self.assertIn("unknown", second_prompt)

    @patch("llm.llm_client.LLMClient.generate")
    def test_run_telegram_rejects_incomplete_llm_output(
        self, mock_generate
    ):
        """A reply without objective/expected_outcome is an error."""

        mock_generate.return_value = {
            "reply": "Ji haan.",
            "objective": "Keep conversation on chat",
        }

        agent = ConversationAgent()

        with self.assertRaises(ValueError) as ctx:
            agent.run_telegram(
                investigation=make_investigation(),
                investigation_state=make_state(),
                latest_message="Hello",
            )

        self.assertIn("Missing 'expected_outcome'", str(ctx.exception))

    def test_run_telegram_requires_the_telegram_prompt_file(self):
        """
        A missing Telegram prompt is a loud FileNotFoundError - the
        agent must never silently fall back to the dashboard rules.
        """

        agent = ConversationAgent()

        with patch.object(
            PromptLoader,
            "load",
            side_effect=FileNotFoundError("prompt missing"),
        ):
            with self.assertRaises(FileNotFoundError):
                agent.run_telegram(
                    investigation=make_investigation(),
                    investigation_state=make_state(),
                    latest_message="Hello",
                )


# --------------------------------------------------
# 3. Conversation service (per-chat loop)
# --------------------------------------------------

class TestConversationService(unittest.TestCase):

    def test_first_message_bootstraps_persona_and_replies(self):
        """
        Turn 1 initializes the engine from the threat type, records
        the turn and archives the case facts.
        """

        service, _, _, memory = build_service()

        result = service.handle_message(
            42,
            "Your SBI account is blocked. Verify at http://sbi-fake.com",
            sender_username="fake_sbi",
        )

        self.assertEqual(result["chat_id"], 42)
        self.assertEqual(result["turn"], 1)
        self.assertEqual(result["threat_type"], "Banking Phishing")
        self.assertEqual(
            result["current_objective"],
            "Collect official verification website",
        )
        self.assertEqual(result["strategy"], "Curious")
        self.assertEqual(result["sender_username"], "fake_sbi")
        self.assertTrue(result["reply"])

        # Persona + transcript + archive were all created.
        state = service.get_state(42)
        self.assertIsNotNone(state)
        self.assertEqual(state.turn, 1)
        self.assertEqual(len(memory.saved), 1)

    def test_later_messages_accumulate_indicators(self):
        """
        IOCs from every message of a chat are merged into ONE case
        file, and the verdict stays the first one (no flip-flopping).
        """

        service, _, _, _ = build_service(
            investigation_results=[
                make_investigation(urls=["http://sbi-fake.com"]),
                make_investigation(
                    urls=["http://pay-fake.in"],
                    threat_type="Job Scam",
                    confidence=60,
                ),
            ]
        )

        service.handle_message(42, "Verify at http://sbi-fake.com")
        result = service.handle_message(
            42, "Pay the fee at http://pay-fake.in"
        )

        state = service.get_state(42)

        self.assertIn("http://sbi-fake.com", state.investigation.urls)
        self.assertIn("http://pay-fake.in", state.investigation.urls)

        # First verdict wins, later IOCs accumulate.
        self.assertEqual(
            state.investigation.threat_type, "Banking Phishing"
        )
        self.assertEqual(state.investigation.confidence, 95)
        self.assertGreaterEqual(state.investigation.risk_score, 80)
        self.assertEqual(result["turn"], 2)

    def test_objective_ladder_advances_between_turns(self):
        """Every answered turn advances the investigation objective."""

        service, _, _, _ = build_service()

        first = service.handle_message(42, "Your card is blocked")
        second = service.handle_message(42, "Send your OTP")
        third = service.handle_message(42, "Pay 500 rupees")

        self.assertEqual(
            first["current_objective"],
            "Collect official verification website",
        )
        self.assertEqual(
            second["current_objective"], "Collect employee ID"
        )
        self.assertEqual(
            third["current_objective"], "Collect payment method"
        )

        # Strategies track the objectives (Curious -> Confused ->
        # Cooperative for the banking ladder).
        self.assertEqual(first["strategy"], "Curious")
        self.assertEqual(second["strategy"], "Confused")
        self.assertEqual(third["strategy"], "Cooperative")

    def test_transcript_records_both_sides(self):
        """
        The transcript given to the LLM contains the scammer line and
        the persona reply, in order.
        """

        service, _, conversation_agent, _ = build_service(
            replies=["Kaise verify karu?"]
        )

        service.handle_message(42, "Your card is blocked")

        history = service.history(42)

        self.assertIn("SCAMMER:", history)
        self.assertIn("TRACEAI:", history)
        self.assertIn("Your card is blocked", history)
        self.assertIn("Kaise verify karu?", history)
        self.assertLess(
            history.index("SCAMMER:"), history.index("TRACEAI:")
        )

        # The agent received that history (with the latest line in it).
        self.assertIn(
            "Your card is blocked",
            conversation_agent.calls[0]["conversation_history"],
        )
        self.assertEqual(
            conversation_agent.calls[0]["chat_id"], 42
        )

    def test_invalid_input_is_rejected(self):
        """Blank text and non-integer chat ids never reach the agents."""

        service, investigation_agent, _, _ = build_service()

        for bad_text in ("", "   ", "\n\t"):
            with self.assertRaises(ValueError):
                service.handle_message(42, bad_text)

        for bad_chat_id in ("42", None, 42.0):
            with self.assertRaises(ValueError):
                service.handle_message(bad_chat_id, "Hello")

        self.assertEqual(investigation_agent.calls, [])
        self.assertEqual(service.active_chat_ids(), [])

    def test_each_chat_keeps_its_own_state(self):
        """Two chats must never share a persona, transcript or turn."""

        # Chat 42 is a banking scam; chat 99 is a job scam.
        service, _, _, _ = build_service(
            investigation_results=[
                make_investigation(),
                make_investigation(),
                make_investigation(threat_type="Job Scam"),
            ]
        )

        service.handle_message(42, "Your SBI card is blocked")
        service.handle_message(42, "Send OTP now")
        service.handle_message(99, "Congratulations, you got the job")

        self.assertEqual(sorted(service.active_chat_ids()), [42, 99])
        self.assertEqual(service.get_state(42).turn, 2)
        self.assertEqual(service.get_state(99).turn, 1)

        self.assertIn("Send OTP now", service.history(42))
        self.assertNotIn("Send OTP now", service.history(99))

        # Different threat families bootstrap different personas.
        self.assertEqual(
            service.get_state(99).engine.get_state().profile.language,
            "English",
        )

    def test_reset_drops_chat_state(self):
        """reset() forgets one chat and leaves the others untouched."""

        service, _, _, _ = build_service()

        service.handle_message(42, "Your card is blocked")
        service.handle_message(99, "You won a lottery")

        self.assertTrue(service.reset(42))

        self.assertIsNone(service.get_state(42))
        self.assertEqual(service.history(42), "No previous conversation.")
        self.assertEqual(service.active_chat_ids(), [99])

        # A second reset reports the chat was already unknown.
        self.assertFalse(service.reset(42))


# --------------------------------------------------
# 4. Background worker
# --------------------------------------------------

class TestConversationWorker(unittest.TestCase):

    def setUp(self):
        reset_worker()

    def tearDown(self):
        reset_worker()

    def test_run_once_answers_inbound_message(self):
        """
        One poll -> one turn -> one delivered reply, and the reply is
        sent back to the chat the message came from.
        """

        service, _, _, _ = build_service(
            replies=["Kaise verify karu?"]
        )
        integration = StubTelegramIntegration(
            updates=[make_update(chat_id=42, text="Your card is blocked")]
        )

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        handled = worker.run_once()

        self.assertEqual(handled, 1)
        self.assertEqual(worker.processed, 1)
        self.assertEqual(len(integration.sent), 1)
        self.assertEqual(integration.sent[0]["chat_id"], 42)
        self.assertEqual(
            integration.sent[0]["text"], "Kaise verify karu?"
        )

        # Long-poll parameters are forwarded to the client.
        self.assertEqual(
            integration.get_updates_calls[0],
            {"timeout": 0, "limit": 10, "ack": True},
        )

    def test_run_once_skips_blank_messages(self):
        """Blank inbound text is skipped - nothing is sent."""

        service, investigation_agent, _, _ = build_service()
        integration = StubTelegramIntegration(
            updates=[make_update(text="    ")]
        )

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.skipped, 1)
        self.assertEqual(worker.processed, 0)
        self.assertEqual(integration.sent, [])
        self.assertEqual(investigation_agent.calls, [])

    def test_send_failure_is_recorded_without_crashing(self):
        """A failed send is recorded; the worker stays usable."""

        service, _, _, _ = build_service()
        integration = StubTelegramIntegration(
            updates=[make_update()],
            send_error=RuntimeError("Telegram 429 rate limited"),
        )

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.failed, 1)
        self.assertEqual(worker.processed, 0)
        self.assertEqual(len(worker.errors), 1)
        self.assertEqual(worker.errors[0]["stage"], "send")
        self.assertIn("429", worker.last_error)

        # The loop survives: a later good message still gets answered.
        integration.send_error = None
        integration.enqueue(make_update(update_id=2))

        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(worker.processed, 1)

    def test_service_failure_is_recorded_without_crashing(self):
        """An exception inside the service never kills the poll."""

        class ExplodingService:
            def handle_message(self, chat_id, text, sender_username=None):
                raise RuntimeError("LLM provider exploded")

            def active_chat_ids(self):
                return []

        integration = StubTelegramIntegration(updates=[make_update()])

        worker = TelegramConversationWorker(
            integration, service=ExplodingService(), poll_timeout=0
        )

        self.assertEqual(worker.run_once(), 0)
        self.assertEqual(worker.failed, 1)
        self.assertEqual(worker.errors[0]["stage"], "handle")
        self.assertEqual(integration.sent, [])

    def test_start_requires_a_connected_integration(self):
        """Polling with an unverified client is refused up front."""

        service, _, _, _ = build_service()
        integration = StubTelegramIntegration(connected=False)

        worker = TelegramConversationWorker(
            integration, service=service, poll_timeout=0
        )

        with self.assertRaises(RuntimeError) as ctx:
            worker.start()

        self.assertIn("not connected", str(ctx.exception))
        self.assertFalse(worker.is_running())

    def test_start_and_stop_lifecycle(self):
        """start() spawns the loop; stop() shuts it down cooperatively."""

        service, _, _, _ = build_service()
        integration = StubTelegramIntegration()

        worker = TelegramConversationWorker(
            integration,
            service=service,
            poll_timeout=0,
            idle_sleep=0.01,
        )

        self.assertTrue(worker.start())
        self.assertTrue(worker.is_running())

        # Idempotent: a second start must not spawn a second loop.
        self.assertFalse(worker.start())

        self.assertTrue(worker.stop(timeout=5.0))
        self.assertFalse(worker.is_running())

        stats = worker.stats()
        self.assertFalse(stats["running"])
        self.assertGreater(stats["polls"], 0)


# --------------------------------------------------
# 5. HTTP endpoints
# --------------------------------------------------

class TestTelegramConversationEndpoints(unittest.TestCase):

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

    def test_conversation_message_endpoint_returns_reply(self):
        """POST /conversation/message runs one turn and returns it."""

        service = StubConversationService()

        with patch(
            "backend.telegram_routes.get_integration",
            return_value=StubTelegramIntegration(),
        ), patch(
            "backend.telegram_routes.get_conversation_service",
            return_value=service,
        ):
            response = self.client.post(
                "/api/telegram/conversation/message",
                json={
                    "chat_id": 42,
                    "text": "Your card is blocked",
                    "sender_username": "fake_sbi",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["status"], "replied")
        self.assertEqual(body["reply"], "Stub persona reply")
        self.assertEqual(body["chat_id"], 42)
        self.assertEqual(body["threat_type"], "Banking Phishing")

        # The service was called with exactly what the client sent.
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0]["chat_id"], 42)
        self.assertEqual(
            service.calls[0]["sender_username"], "fake_sbi"
        )

    def test_conversation_message_requires_connection(self):
        """Unconfigured / unverified Telegram is an honest 409."""

        unconfigured = StubTelegramIntegration()
        unconfigured.is_configured = lambda: False

        disconnected = StubTelegramIntegration(connected=False)

        for integration, expected in (
            (unconfigured, "not_configured"),
            (disconnected, "not_connected"),
        ):
            with patch(
                "backend.telegram_routes.get_integration",
                return_value=integration,
            ):
                response = self.client.post(
                    "/api/telegram/conversation/message",
                    json={"chat_id": 42, "text": "Hello"},
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["status"], expected
            )

    def test_conversation_message_validates_payload(self):
        """chat_id 0, blank text and over-long text are 422s."""

        with patch(
            "backend.telegram_routes.get_integration",
            return_value=StubTelegramIntegration(),
        ):
            for payload in (
                {"chat_id": 0, "text": "Hello"},
                {"chat_id": 42, "text": "   "},
                {"chat_id": 42, "text": ""},
                {"chat_id": 42, "text": "x" * 4097},
                {"chat_id": 42},
            ):
                response = self.client.post(
                    "/api/telegram/conversation/message",
                    json=payload,
                )

                self.assertEqual(
                    response.status_code,
                    422,
                    msg=f"payload {payload} should be rejected",
                )

    def test_start_and_stop_endpoints(self):
        """start/stop report the worker's real running state."""

        running_worker = FakeWorker(running=True)

        with patch(
            "backend.telegram_routes.get_integration",
            return_value=StubTelegramIntegration(),
        ), patch(
            "backend.telegram_routes.start_worker",
            return_value=running_worker,
        ) as start_mock:
            response = self.client.post(
                "/api/telegram/conversation/start"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")
        self.assertEqual(response.json()["worker"]["processed"], 3)
        start_mock.assert_called_once()

        stopped_worker = FakeWorker(running=False)

        with patch(
            "backend.telegram_routes.stop_worker",
            return_value=True,
        ), patch(
            "backend.telegram_routes.get_worker",
            return_value=stopped_worker,
        ):
            response = self.client.post(
                "/api/telegram/conversation/stop"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "stopped")
        self.assertFalse(response.json()["worker"]["running"])

    def test_status_endpoint_reports_honest_state(self):
        """
        Status never 409s: it reports configured/connected/running and
        the per-chat state held in memory.
        """

        service = StubConversationService(chat_ids=[42])

        with patch(
            "backend.telegram_routes.get_integration",
            return_value=StubTelegramIntegration(),
        ), patch(
            "backend.telegram_routes.get_conversation_service",
            return_value=service,
        ), patch(
            "backend.telegram_routes.get_worker",
            return_value=None,
        ):
            response = self.client.get(
                "/api/telegram/conversation/status"
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertTrue(body["telegram"]["configured"])
        self.assertTrue(body["telegram"]["connected"])
        self.assertFalse(body["running"])
        self.assertIsNone(body["worker"])
        self.assertEqual(body["chat_count"], 1)
        self.assertEqual(body["chats"][0]["chat_id"], 42)

    def test_reset_endpoint_clears_chat_state(self):
        """Known chats report 'reset'; unknown ones report honestly."""

        service = StubConversationService(chat_ids=[42])

        with patch(
            "backend.telegram_routes.get_conversation_service",
            return_value=service,
        ):
            known = self.client.post(
                "/api/telegram/conversation/reset",
                json={"chat_id": 42},
            )
            unknown = self.client.post(
                "/api/telegram/conversation/reset",
                json={"chat_id": 999},
            )

        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.json()["status"], "reset")
        self.assertEqual(known.json()["chat_count"], 0)

        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(unknown.json()["status"], "unknown_chat")

        self.assertEqual(service.reset_calls, [42, 999])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
