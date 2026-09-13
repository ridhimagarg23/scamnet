"""
conversation_service.py
=======================
Headless Telegram <-> ConversationAgent conversational loop.

This module is the Phase 2A bridge: it turns a raw Telegram message
into an undercover persona reply by running the SAME pipeline the
dashboard's ``POST /analyze`` uses, but with no HTTP request/response
involved and with one independent state machine per Telegram chat.

One turn (``TelegramConversationService.handle_message``) runs:

    inbound Telegram text
        -> InvestigationAgent          (IOC regex + URL checks + LLM
                                        verdict + deterministic risk)
        -> merge into the chat's accumulated case facts
        -> AdaptiveInvestigationEngine (persona profile + objective
                                        ladder, advanced every turn)
        -> ConversationAgent.run_telegram()  (persona's next reply,
                                        driven by the Telegram prompt)
        -> MemoryManager               (archive case facts to JSON)

Why a separate service instead of reusing /analyze?
* ``/analyze`` is request-scoped and UI-shaped (progress steps,
  timeline, persona card). The Telegram loop needs neither.
* Telegram needs ONE state machine PER CHAT (keyed by chat_id),
  not per analyst session.
* Keeping the loop here makes it callable from a background worker,
  from an HTTP endpoint or from a unit test without FastAPI.

State is in-memory only (like the rest of the project): a process
restart starts every chat fresh.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agents.conversation_agent import ConversationAgent
from agents.investigation_agent import InvestigationAgent

from tools.adaptive_investigation_engine import (
    AdaptiveInvestigationEngine,
)
from tools.conversation_session import ConversationSession
from tools.memory_manager import MemoryManager
from tools.risk_engine import RiskEngine
from tools.url_checker import URLChecker

from utils.schemas import InvestigationResult

logger = logging.getLogger("SCAMNET-ConversationService")


# ----------------------------------------------------------
# Bot commands (handled WITHOUT the LLM)
# ----------------------------------------------------------
# ``/start`` is what Telegram itself sends when a chat opens a bot for
# the first time, and it is the one message an operator can always send
# to check that the loop is alive. It must therefore never depend on
# the LLM provider: the worker answers it with a fixed, cover-safe
# greeting (see ``TelegramConversationWorker``).
START_COMMAND = "/start"

# Cover-safe liveness greeting. It deliberately does NOT mention
# investigations, SCAMNET, evidence or personas: a scammer who taps
# /start only sees a normal, friendly bot greeting.
START_GREETING = (
    "\U0001F44B Hi! You are connected. Send me a message and I will reply."
)

# Longest bot-command suffix Telegram may append ("/start@my_bot").
_COMMAND_SUFFIX = "@"


def normalize_command(text: str) -> str:
    """
    Normalize a bot command for comparison.

    ``"/START"``, ``"/start"`` and ``"/start@scamnet_intel_bot"`` all
    normalize to ``"/start"`` - Telegram sends the group-chat form
    (with the bot username) whenever the chat is a group.

    Returns an empty string for non-command text.
    """

    if not isinstance(text, str):
        return ""

    candidate = text.strip()

    if not candidate.startswith("/"):
        return ""

    # Only the first token is the command ("/start hello" -> "/start").
    candidate = candidate.split()[0].lower()

    if _COMMAND_SUFFIX in candidate:
        candidate = candidate.split(_COMMAND_SUFFIX, 1)[0]

    return candidate


def is_start_command(text: str) -> bool:
    """True when ``text`` is ``/start`` (however Telegram formatted it)."""

    return normalize_command(text) == START_COMMAND


# IOC list fields that are merged (union, de-duplicated, sorted) from
# every message of the same chat before risk is re-scored.
MERGED_IOC_FIELDS = (
    "phone_numbers",
    "emails",
    "urls",
    "upi_ids",
    "otp_keywords",
    "amounts",
    "bank_names",
)


def merge_investigations(
    existing: InvestigationResult,
    new: InvestigationResult,
) -> InvestigationResult:
    """
    Fold a freshly investigated message into a chat's accumulated case
    facts, then re-score risk on the FULL evidence set.

    This mirrors the accumulation ``POST /analyze`` performs for
    subsequent turns: the verdict (is_scam / confidence / threat_type /
    summary) stays the FIRST verdict of the chat so the persona never
    flip-flops mid-conversation, while every newly extracted IOC is
    added to the running total.

    Parameters
    ----------
    existing : InvestigationResult
        The chat's accumulated investigation (mutated in place).
    new : InvestigationResult
        Result of investigating the latest message.

    Returns
    -------
    InvestigationResult
        The same (mutated) ``existing`` object.
    """

    for attribute in MERGED_IOC_FIELDS:

        merged = sorted(
            set(
                getattr(existing, attribute)
                + getattr(new, attribute)
            )
        )

        setattr(existing, attribute, merged)

    # ----------------------------------------------------------
    # Recalculate risk with everything collected so far.
    # ----------------------------------------------------------

    entities = {
        "phone_numbers": existing.phone_numbers,
        "emails": existing.emails,
        "urls": existing.urls,
        "upi_ids": existing.upi_ids,
        "otp_keywords": existing.otp_keywords,
        "amounts": existing.amounts,
        "bank_names": existing.bank_names,
    }

    url_analysis = [
        URLChecker.analyze(url)
        for url in existing.urls
    ]

    risk = RiskEngine.calculate(
        {
            "is_scam": existing.is_scam,
            "confidence": existing.confidence,
        },
        entities,
        url_analysis,
    )

    existing.risk_score = risk["risk_score"]
    existing.risk_level = risk["risk_level"]

    existing.detected_indicators = sorted(
        set(existing.detected_indicators + risk["reasons"])
    )

    existing.recommendations = sorted(
        set(existing.recommendations + risk["reasons"])
    )

    return existing


@dataclass
class TelegramChatState:
    """
    Everything the loop remembers about ONE Telegram chat.

    Attributes
    ----------
    chat_id : int
        Telegram chat this state belongs to.
    session : ConversationSession
        Transcript of the chat (scammer turns + persona replies).
    engine : AdaptiveInvestigationEngine
        Persona profile / objective ladder for this chat.
    investigation : InvestigationResult | None
        Accumulated case facts; None until the first message arrives.
    turn : int
        Number of processed inbound messages (1-based after turn 1).
    last_threat_type / last_objective / last_strategy : str | None
        Latest engine output, handy for status endpoints and tests.
    """

    chat_id: int
    session: ConversationSession = field(
        default_factory=ConversationSession
    )
    engine: AdaptiveInvestigationEngine = field(
        default_factory=AdaptiveInvestigationEngine
    )
    investigation: Optional[InvestigationResult] = None
    turn: int = 0
    last_threat_type: Optional[str] = None
    last_objective: Optional[str] = None
    last_strategy: Optional[str] = None


class TelegramConversationService:
    """
    Runs the conversational loop for every active Telegram chat.

    Parameters
    ----------
    investigation_agent : InvestigationAgent | None
        Injected agent (tests pass a stub). Built lazily when omitted.
    conversation_agent : ConversationAgent | None
        Injected agent (tests pass a stub). Built lazily when omitted.
    memory_manager : MemoryManager | None
        Injected archive (tests pass a stub). Built lazily when
        omitted. Archiving is best-effort: a failure is logged and
        never breaks the conversation.
    archive : bool
        Set False to keep the loop fully side-effect free (no writes).
    """

    def __init__(
        self,
        investigation_agent=None,
        conversation_agent=None,
        memory_manager=None,
        archive: bool = True,
    ):

        self._investigation_agent = investigation_agent
        self._conversation_agent = conversation_agent
        self._memory_manager = memory_manager
        self.archive = archive

        # chat_id -> TelegramChatState
        self.chats: Dict[int, TelegramChatState] = {}

    # ----------------------------------------------------------
    # Lazily-built collaborators
    # ----------------------------------------------------------
    # Deferred so constructing the service never requires an API key
    # or touches the filesystem - only actually handling a message
    # does.

    @property
    def investigation_agent(self):
        """The InvestigationAgent (created on first use)."""

        if self._investigation_agent is None:
            self._investigation_agent = InvestigationAgent()
        return self._investigation_agent

    @property
    def conversation_agent(self):
        """The ConversationAgent (created on first use)."""

        if self._conversation_agent is None:
            self._conversation_agent = ConversationAgent()
        return self._conversation_agent

    @property
    def memory_manager(self):
        """The MemoryManager (created on first use)."""

        if self._memory_manager is None:
            self._memory_manager = MemoryManager()
        return self._memory_manager

    # ----------------------------------------------------------
    # Main loop
    # ----------------------------------------------------------

    def handle_message(
        self,
        chat_id: int,
        text: str,
        sender_username: Optional[str] = None,
    ) -> dict:
        """
        Process one inbound Telegram message and return the persona's
        reply plus the investigation context it was written under.

        Parameters
        ----------
        chat_id : int
            Telegram chat id (also the state key).
        text : str
            Raw inbound message text.
        sender_username : str | None
            Public @handle of the sender, when Telegram supplied one.

        Returns
        -------
            dict
            ``chat_id``, ``reply``, ``objective``, ``expected_outcome``,
            ``objective_achieved``, ``is_scam``, ``confidence``,
            ``threat_type``, ``risk_score``, ``risk_level``, ``strategy``,
            ``current_objective``, ``turn``, ``sender_username``,
            ``message``.

        Raises
        ------
        ValueError
            If ``chat_id`` is not an int or ``text`` is empty/blank.
        """

        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError(
                "chat_id must be an integer Telegram chat id."
            )

        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                "text must be a non-empty string."
            )

        state = self._get_or_create_state(chat_id)

        # ------------------------------------------------------
        # 1. Investigate the inbound message
        # ------------------------------------------------------

        fresh = self.investigation_agent.run(text)

        if state.investigation is None:

            # FIRST message of the chat: seed the case facts and
            # bootstrap the persona (profile + first objective).
            state.investigation = fresh
            engine_state = state.engine.initialize(
                fresh.threat_type
            )

        else:

            # LATER messages: accumulate IOCs and re-score risk.
            # The objective ladder is advanced only AFTER the
            # ConversationAgent verifies that the inbound message
            # supplied the evidence the current objective requested.
            state.investigation = merge_investigations(
                state.investigation, fresh
            )
            engine_state = state.engine.get_state()

        investigation = state.investigation

        # ------------------------------------------------------
        # 2. Record the inbound turn, then generate the reply
        # ------------------------------------------------------
        # The scammer line is appended BEFORE generating so the
        # transcript handed to the LLM already contains the message
        # being answered (same ordering as POST /analyze).

        state.session.add_scammer_message(text)

        result = self.conversation_agent.run_telegram(
            investigation=investigation,
            investigation_state=engine_state,
            latest_message=text,
            conversation_history=state.session.get_history(),
            chat_id=chat_id,
            sender_username=sender_username,
        )

        state.session.add_traceai_reply(
            result.reply
        )

        # The model is closest to the conversational semantics: the
        # turn counter advances after every inbound message, but the
        # evidence ladder advances ONLY when that message delivered the
        # requested artifact. Asking for an OTP, saying "wait", or
        # merely continuing the chat must not jump to a new objective.
        state.turn += 1
        engine_state = state.engine.update(
            objective_completed=result.objective_achieved
        )
        # Keep the engine's turn number aligned with the number of
        # inbound messages actually handled in THIS chat.
        engine_state.turn_number = state.turn

        state.last_threat_type = investigation.threat_type
        state.last_objective = engine_state.current_objective
        state.last_strategy = engine_state.current_strategy

        # ------------------------------------------------------
        # 3. Archive (best effort - never breaks the conversation)
        # ------------------------------------------------------

        if self.archive:

            try:
                self.memory_manager.save(
                    investigation.model_dump()
                )
            except Exception as exc:  # pragma: no cover - defensive

                logger.warning(
                    "Could not archive chat %s investigation: %s",
                    chat_id, exc,
                )

        logger.info(
            "Telegram chat %s turn %s handled (threat=%s, risk=%s, "
            "objective=%s).",
            chat_id, state.turn, investigation.threat_type,
            investigation.risk_score, engine_state.current_objective,
        )

        return {
            "chat_id": chat_id,
            "reply": result.reply,
            "objective": result.objective,
            "expected_outcome": result.expected_outcome,
            "objective_achieved": result.objective_achieved,
            "is_scam": investigation.is_scam,
            "confidence": investigation.confidence,
            "threat_type": investigation.threat_type,
            "risk_score": investigation.risk_score,
            "risk_level": investigation.risk_level,
            "strategy": engine_state.current_strategy,
            "current_objective": engine_state.current_objective,
            "turn": state.turn,
            "sender_username": sender_username,
            "message": text,
        }

    # ----------------------------------------------------------
    # State access
    # ----------------------------------------------------------

    def handle_start(
        self,
        chat_id: int,
        sender_username: Optional[str] = None,
    ) -> dict:
        """
        Answer the ``/start`` command without touching the LLM.

        Parameters
        ----------
        chat_id : int
            Telegram chat to greet.
        sender_username : str | None
            Public @handle of the sender (echoed back only).

        Returns
        -------
        dict
            ``{"chat_id", "reply", "kind": "start", "turn", ...}`` where
            ``turn`` is the chat's existing turn count (0 for a chat
            that has not started a case yet).

        Notes
        -----
        Deliberately side-effect free: greeting a chat must not create
        case state, because Telegram sends ``/start`` automatically the
        first time a user opens the bot. A case is only opened by the
        first real message.
        """

        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError(
                "chat_id must be an integer Telegram chat id."
            )

        if chat_id == 0:
            raise ValueError("chat_id cannot be 0.")

        state = self.chats.get(chat_id)

        return {
            "chat_id": chat_id,
            "reply": START_GREETING,
            "kind": "start",
            "turn": state.turn if state is not None else 0,
            "sender_username": sender_username,
        }

    def _get_or_create_state(
        self,
        chat_id: int,
    ) -> TelegramChatState:
        """Return (creating on demand) the state for ``chat_id``."""

        if chat_id not in self.chats:

            self.chats[chat_id] = TelegramChatState(
                chat_id=chat_id
            )

        return self.chats[chat_id]

    def get_state(
        self,
        chat_id: int,
    ) -> Optional[TelegramChatState]:
        """Return the chat state, or None when the chat is unknown."""

        return self.chats.get(chat_id)

    def active_chat_ids(self) -> List[int]:
        """Ids of every chat currently held in memory."""

        return list(self.chats.keys())

    def history(self, chat_id: int) -> str:
        """
        Plain-text transcript of a chat (``"No previous conversation."``
        when the chat is unknown or still empty).
        """

        state = self.chats.get(chat_id)

        if state is None:
            return "No previous conversation."

        return state.session.get_history()

    def summary(self, chat_id: int) -> Optional[dict]:
        """
        Compact status snapshot of one chat, or None when unknown.

        Shaped for ``GET /api/telegram/conversation/status``.
        """

        state = self.chats.get(chat_id)

        if state is None:
            return None

        investigation = state.investigation

        return {
            "chat_id": chat_id,
            "turn": state.turn,
            "threat_type": state.last_threat_type,
            "current_objective": state.last_objective,
            "strategy": state.last_strategy,
            "risk_score": (
                investigation.risk_score
                if investigation else None
            ),
            "risk_level": (
                investigation.risk_level
                if investigation else None
            ),
            "messages": len(state.session.history),
        }

    def reset(self, chat_id: int) -> bool:
        """
        Drop all state for one chat (fresh persona, fresh case facts).

        Returns
        -------
        bool
            True when a chat was actually removed, False when unknown.
        """

        return self.chats.pop(chat_id, None) is not None

    def reset_all(self) -> None:
        """Drop every chat (used by tests and by the reset endpoint)."""

        self.chats.clear()


# ----------------------------------------------------------
# Process-wide default service
# ----------------------------------------------------------
# The worker and the HTTP endpoints share ONE service instance so a
# chat's state survives across polls and requests.

_SERVICE: Optional[TelegramConversationService] = None


def get_conversation_service() -> TelegramConversationService:
    """Return (creating on first call) the shared conversation service."""

    global _SERVICE

    if _SERVICE is None:
        _SERVICE = TelegramConversationService()

    return _SERVICE


def reset_conversation_service() -> None:
    """Forget the shared service (tests, or a full loop restart)."""

    global _SERVICE

    _SERVICE = None
