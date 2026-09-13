"""
telegram_routes.py
==================
Minimal, test-oriented Telegram endpoints for SCAMNET.

These routes exist to PROVE that Telegram communication works
end-to-end before any AI agent is wired to the channel:

    GET  /api/telegram/messages   - fetch recent incoming messages
                                    (Bot API long polling, normalized)
    POST /api/telegram/send-test  - send one test text message to a
                                    specific chat_id

Phase 2A conversational loop (Telegram <-> ConversationAgent):

    POST /api/telegram/conversation/message - run ONE inbound message
                                    through the investigation +
                                    conversation pipeline and return
                                    the persona reply (no polling)
    POST /api/telegram/conversation/start   - start the background
                                    polling worker (it answers every
                                    inbound message automatically)
    POST /api/telegram/conversation/stop    - stop the worker
    GET  /api/telegram/conversation/status  - honest worker + chat
                                    state snapshot (never 409)
    POST /api/telegram/conversation/reset   - drop one chat's state

Connection status itself is served by the existing integration
endpoints in backend/api.py:

    GET  /api/integrations                       (status of all apps)
    POST /api/integrations/telegram/connect      (real getMe verify)

Safety rules enforced here
--------------------------
* The routes that would talk to the bot (messages, send-test,
  conversation/message, conversation/start) require the integration to
  be genuinely connected first (409 otherwise) - no anonymous drive-by
  usage. Status/stop/reset stay available so an operator can always
  observe and shut the loop down.
* Inputs are strictly validated by pydantic (chat_id int, text 1-4096
  chars, limit/timeout/ack bounded) - no arbitrary payloads.
* Only normalized message data is returned; the bot token never
  appears in a response, and upstream failures surface as sanitised
  502s (see TelegramAPIError / _redact in integrations/telegram).
* Nothing is persisted: messages are fetched on demand and the update
  acknowledgement cursor lives in server memory only.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from integrations import get_integration
from integrations.base import (
    IntegrationConnectionError,
    IntegrationNotConfiguredError,
)
from integrations.telegram.client import (
    MAX_TEXT_LENGTH,
    TelegramIntegration,
)

from tools.conversation_service import get_conversation_service
from tools.telegram_conversation_worker import (
    get_worker,
    start_worker,
    stop_worker,
)

logger = logging.getLogger("SCAMNET-API-Telegram")

router = APIRouter(
    prefix="/api/telegram",
    tags=["telegram"],
)


# --------------------------------------------------
# Request models
# --------------------------------------------------

class SendTestMessageRequest(BaseModel):
    """
    Body of POST /api/telegram/send-test.

    chat_id : int
        Numeric Telegram chat id (private chats positive, groups
        negative). Numeric strings are coerced; anything else 422s.
    text : str
        Non-empty plain text, at most Telegram's 4096-char limit.
    """

    chat_id: int
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)

    @field_validator("chat_id")
    @classmethod
    def chat_id_non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("chat_id cannot be 0.")
        return value

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be blank.")
        return value


class ConversationMessageRequest(BaseModel):
    """
    Body of POST /api/telegram/conversation/message.

    Drives ONE turn of the Telegram <-> ConversationAgent loop
    without starting the background worker - useful for manual
    testing and for the regression suite.

    chat_id : int
        Numeric Telegram chat id the message belongs to.
    text : str
        The inbound message text to answer (1-4096 chars).
    sender_username : str | None
        Optional @handle of the sender (prompt context only).
    """

    chat_id: int
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    sender_username: Optional[str] = None

    @field_validator("chat_id")
    @classmethod
    def conversation_chat_id_non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("chat_id cannot be 0.")
        return value

    @field_validator("text")
    @classmethod
    def conversation_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be blank.")
        return value


class ResetChatRequest(BaseModel):
    """Body of POST /api/telegram/conversation/reset."""

    chat_id: int

    @field_validator("chat_id")
    @classmethod
    def reset_chat_id_non_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("chat_id cannot be 0.")
        return value


# --------------------------------------------------
# Shared guards
# --------------------------------------------------

def _require_connected_telegram() -> TelegramIntegration:
    """
    Return the registered Telegram client, or raise an honest 409 when
    it is not configured / not genuinely connected yet.
    """

    integration = get_integration("telegram")

    if integration is None or not integration.is_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "status": "not_configured",
                "message": (
                    "Telegram is not configured on the server. Set "
                    "TELEGRAM_BOT_TOKEN in the server-side .env and "
                    "restart the backend."
                ),
            },
        )

    if not integration.is_connected():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "status": "not_connected",
                "message": (
                    "Telegram is not connected yet. Call POST "
                    "/api/integrations/telegram/connect first (it "
                    "verifies the bot token against Telegram)."
                ),
            },
        )

    return integration


# --------------------------------------------------
# Endpoints
# --------------------------------------------------

@router.get("/messages")
def telegram_recent_messages(
    limit: int = Query(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of updates to fetch (Telegram bound).",
    ),
    timeout: int = Query(
        default=0,
        ge=0,
        le=30,
        description=(
            "Long-poll wait in seconds. 0 returns immediately with "
            "queued updates; >0 waits for new messages."
        ),
    ),
    ack: bool = Query(
        default=True,
        description=(
            "Acknowledge fetched updates (cursor advances, Telegram "
            "stops re-delivering them). Use false to peek without "
            "consuming."
        ),
    ),
):
    """
    Fetch recent incoming Telegram messages for testing.

    Returns the normalized internal structure (see
    integrations/telegram/models.py::IncomingMessage):

        {"count": N, "messages": [{"update_id", "chat_id",
         "message_id", "sender_id", "sender_username", "text",
         "timestamp"}, ...]}

    With the default ``ack=true`` each update is returned once; the
    acknowledgement cursor is in-memory (server restart re-reads
    unacknowledged updates kept by Telegram for ~24 h).
    """

    integration = _require_connected_telegram()

    logger.info(
        "Telegram message fetch requested (limit=%s, timeout=%s, ack=%s).",
        limit, timeout, ack,
    )

    try:
        messages = integration.get_updates(
            timeout=timeout, limit=limit, ack=ack
        )
    except IntegrationConnectionError as exc:
        # Sanitised by the client (never contains the token/URL).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"status": "telegram_error", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"status": "invalid_request", "message": str(exc)},
        )

    return {
        "count": len(messages),
        "messages": [message.model_dump() for message in messages],
    }


@router.post("/send-test")
def telegram_send_test(request: SendTestMessageRequest):
    """
    Send a single test text message to ``chat_id`` through the bot.

    Returns ``{"chat_id": ..., "message_id": ...}`` of the delivered
    message. Only plain text is accepted; validation is enforced by
    ``SendTestMessageRequest`` (422 on bad input) and re-checked
    client-side before any network call.
    """

    integration = _require_connected_telegram()

    logger.info(
        "Telegram test message requested for chat_id=%s (%s chars).",
        request.chat_id, len(request.text),
    )

    try:
        result = integration.send_message(request.chat_id, request.text)
    except IntegrationConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"status": "telegram_error", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"status": "invalid_request", "message": str(exc)},
        )

    return {
        "status": "sent",
        "chat_id": result["chat_id"],
        "message_id": result["message_id"],
    }


# --------------------------------------------------
# Phase 2A: Telegram <-> ConversationAgent loop
# --------------------------------------------------
# These endpoints expose tools/conversation_service.py (one turn) and
# tools/telegram_conversation_worker.py (the background polling loop).
# Only the two routes that need a working bot are guarded by
# _require_connected_telegram(); status / stop / reset stay callable so
# an operator can always observe or shut the loop down.

@router.post("/conversation/message")
def telegram_conversation_message(
    request: ConversationMessageRequest,
):
    """
    Run ONE inbound Telegram message through the full pipeline and
    return the persona's reply.

    Pipeline: InvestigationAgent -> (accumulate IOCs + re-score risk)
    -> AdaptiveInvestigationEngine -> ConversationAgent (Telegram
    prompt). The chat's state (transcript, case facts, objective
    ladder) is kept in memory, so successive calls build a coherent
    multi-turn undercover conversation.

    Returns ``{"status": "replied", ...turn payload}``. A malformed
    payload is a 400; a failure inside the agents is a sanitised 502.
    """

    _require_connected_telegram()

    service = get_conversation_service()

    logger.info(
        "Telegram conversation turn requested for chat_id=%s (%s chars).",
        request.chat_id, len(request.text),
    )

    try:
        result = service.handle_message(
            request.chat_id,
            request.text,
            sender_username=request.sender_username,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "invalid_request",
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "Telegram conversation turn failed for chat_id=%s: %s",
            request.chat_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "status": "conversation_failed",
                "message": str(exc),
            },
        )

    return {
        "status": "replied",
        **result,
    }


@router.post("/conversation/start")
def telegram_conversation_start():
    """
    Start the background worker that answers every inbound Telegram
    message automatically.

    Idempotent: calling it while the worker is already polling returns
    the running worker instead of spawning a second loop.
    """

    integration = _require_connected_telegram()

    worker = start_worker(integration)

    logger.info(
        "Telegram conversation worker start requested (running=%s).",
        worker.is_running(),
    )

    return {
        "status": (
            "running" if worker.is_running() else "stopped"
        ),
        "worker": worker.stats(),
    }


@router.post("/conversation/stop")
def telegram_conversation_stop():
    """
    Stop the background worker.

    Deliberately NOT guarded by the connection check: an operator must
    always be able to shut the loop down, even if Telegram dropped.
    """

    worker = get_worker()

    stopped = stop_worker()

    return {
        "status": "stopped" if stopped else "stopping",
        "worker": worker.stats() if worker else None,
    }


@router.get("/conversation/status")
def telegram_conversation_status():
    """
    Honest snapshot of the conversational loop.

    Never 409s: it reports ``configured`` / ``connected`` / ``running``
    as booleans so the UI can reflect reality without special-casing
    error codes. Includes per-chat state held in memory.
    """

    integration = get_integration("telegram")

    service = get_conversation_service()
    worker = get_worker()

    chat_ids = service.active_chat_ids()

    return {
        "telegram": {
            "configured": bool(
                integration and integration.is_configured()
            ),
            "connected": bool(
                integration and integration.is_connected()
            ),
        },
        "running": bool(worker and worker.is_running()),
        "worker": worker.stats() if worker else None,
        "chat_count": len(chat_ids),
        "chats": [
            summary
            for summary in (
                service.summary(chat_id)
                for chat_id in chat_ids
            )
            if summary is not None
        ],
    }


@router.post("/conversation/reset")
def telegram_conversation_reset(request: ResetChatRequest):
    """
    Drop all in-memory state for one chat (transcript, accumulated
    case facts, objective ladder), so the next message starts a fresh
    undercover persona.

    Returns ``unknown_chat`` when the chat was not being tracked -
    resetting an unknown chat is a no-op, never an error.
    """

    service = get_conversation_service()

    removed = service.reset(request.chat_id)

    logger.info(
        "Telegram conversation reset for chat_id=%s (removed=%s).",
        request.chat_id, removed,
    )

    return {
        "status": "reset" if removed else "unknown_chat",
        "chat_id": request.chat_id,
        "chat_count": len(service.active_chat_ids()),
    }
