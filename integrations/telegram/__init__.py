"""Telegram integration subpackage (see client.py for the contract)."""

from .client import TelegramAPIError, TelegramIntegration
from .models import IncomingMessage, normalize_update

__all__ = [
    "TelegramIntegration",
    "TelegramAPIError",
    "IncomingMessage",
    "normalize_update",
]
