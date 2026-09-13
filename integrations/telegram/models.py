"""
models.py
=========
Normalized message models for the Telegram channel.

``IncomingMessage`` is the simple internal structure every inbound
Telegram message is converted into:

    {
        "update_id":       int,
        "chat_id":         int,
        "message_id":      int,
        "sender_id":       int | None,
        "sender_username": str | None,
        "text":            str,
        "timestamp":       int | None   (Unix epoch seconds)
    }

It is deliberately independent from Telegram's raw Bot API update
structure so the (future) SCAMNET investigation engine consumes one
stable schema no matter which channel a message arrived on.

``normalize_update()`` performs the conversion and safely skips
anything that is not a plain inbound text message (channel posts,
edits, photos, stickers, malformed payloads...) by returning None -
normalization must never crash the polling loop.
"""

from typing import Any, Optional

from pydantic import BaseModel


class IncomingMessage(BaseModel):
    """
    One normalized inbound message from the suspicious actor.

    Attributes
    ----------
    update_id : int
        Telegram's monotonically increasing update identifier (used for
        acknowledgement/offset tracking).
    chat_id : int
        Chat the message belongs to - the reply target for
        ``send_message``.
    message_id : int
        Identifier of the message inside the chat.
    sender_id : int | None
        Numeric user id of the sender (``from.id``) when present.
    sender_username : str | None
        Public @username of the sender when present.
    text : str
        Message text. MVP normalizes plain text messages only.
    timestamp : int | None
        Unix epoch seconds (``message.date``) when present.
    """

    update_id: int
    chat_id: int
    message_id: int
    sender_id: Optional[int] = None
    sender_username: Optional[str] = None
    text: str
    timestamp: Optional[int] = None


def normalize_update(update: Any) -> Optional[IncomingMessage]:
    """
    Convert one raw Bot API update dict into an ``IncomingMessage``.

    Returns None (never raises) when the update is not a plain inbound
    text message or is malformed - callers skip those but still advance
    the update offset past them.
    """

    if not isinstance(update, dict):
        return None

    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return None

    # MVP: only fresh, plain "message" updates (no edits / channel
    # posts / callbacks / inline queries).
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    if not isinstance(text, str):
        # Non-text message (photo, sticker, voice, ...): skipped for
        # the text-based investigation MVP.
        return None

    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None

    message_id = message.get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return None

    sender = message.get("from")
    sender = sender if isinstance(sender, dict) else {}

    sender_id = sender.get("id")
    if not isinstance(sender_id, int) or isinstance(sender_id, bool):
        sender_id = None

    sender_username = sender.get("username")
    if not isinstance(sender_username, str):
        sender_username = None

    timestamp = message.get("date")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        timestamp = None

    return IncomingMessage(
        update_id=update_id,
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_username=sender_username,
        text=text,
        timestamp=timestamp,
    )
