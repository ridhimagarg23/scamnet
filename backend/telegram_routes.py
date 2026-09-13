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

Connection status itself is served by the existing integration
endpoints in backend/api.py:

    GET  /api/integrations                       (status of all apps)
    POST /api/integrations/telegram/connect      (real getMe verify)

Safety rules enforced here
--------------------------
* Both routes require the integration to be genuinely connected first
  (409 otherwise) - no anonymous drive-by usage.
* Inputs are strictly validated by pydantic (chat_id int, text 1-4096
  chars, limit/timeout/ack bounded) - no arbitrary payloads.
* Only normalized message data is returned; the bot token never
  appears in a response, and upstream failures surface as sanitised
  502s (see TelegramAPIError / _redact in integrations/telegram).
* Nothing is persisted: messages are fetched on demand and the update
  acknowledgement cursor lives in server memory only.
"""

import logging

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
