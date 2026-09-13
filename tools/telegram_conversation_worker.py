"""
telegram_conversation_worker.py
===============================
Background worker that turns Telegram into a live conversation channel.

Phase 2A closes the loop end-to-end:

    Telegram update (long poll)
        -> TelegramConversationService.handle_message()
             (InvestigationAgent -> Adaptive engine -> ConversationAgent)
        -> TelegramIntegration.send_message(chat_id, persona reply)

The worker owns ONLY the plumbing (polling, threading, error isolation);
all conversation intelligence lives in ``tools/conversation_service.py``
so it can be unit-tested without threads or Telegram.

Design rules
------------
* **Never die on one bad message.** A failing investigation, a failing
  LLM call or a failing send is recorded and the loop continues -
  a stuck worker would silently end the undercover operation.
* **Never send an empty reply.** Blank/whitespace inbound messages and
  empty model output are skipped without touching the chat.
* **Stop cooperatively.** ``stop()`` signals an event; the loop checks
  it between polls and before every send, so shutdown is prompt even
  during a long poll.
* **No secrets.** The worker only ever handles normalized messages and
  reply text - the bot token stays inside the Telegram client.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

from integrations.telegram.client import MAX_TEXT_LENGTH

from tools.conversation_service import (
    TelegramConversationService,
    get_conversation_service,
)

logger = logging.getLogger("SCAMNET-TelegramWorker")


# Long-poll wait used by the background loop. Kept short so stop()
# stays responsive; Telegram re-delivers anything unacknowledged.
DEFAULT_POLL_TIMEOUT = 5

# Maximum number of updates consumed per poll (Telegram bound is 100).
DEFAULT_POLL_LIMIT = 10

# Pause between two polls when the previous one returned nothing.
DEFAULT_IDLE_SLEEP = 0.5

# Pause after an unexpected failure, so a broken upstream is not
# hammered in a tight loop.
DEFAULT_ERROR_BACKOFF = 2.0

# How long stop() waits for the polling thread to finish.
DEFAULT_STOP_TIMEOUT = 10.0


class TelegramConversationWorker:
    """
    Polls Telegram for inbound messages and answers each one through
    the conversation service.

    Parameters
    ----------
    integration : TelegramIntegration
        A *connected* Telegram client (``get_updates`` / ``send_message``).
        Tests inject a stub exposing the same two methods.
    service : TelegramConversationService | None
        The brain; defaults to the process-wide shared service.
    poll_timeout : int
        Long-poll wait per ``get_updates`` call (0-50 seconds).
    poll_limit : int
        Maximum updates fetched per poll (1-100).
    idle_sleep : float
        Seconds to wait between empty polls.
    error_backoff : float
        Seconds to wait after an unexpected failure.
    """

    def __init__(
        self,
        integration,
        service: Optional[TelegramConversationService] = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
        poll_limit: int = DEFAULT_POLL_LIMIT,
        idle_sleep: float = DEFAULT_IDLE_SLEEP,
        error_backoff: float = DEFAULT_ERROR_BACKOFF,
    ):

        self.integration = integration
        self.service = service or get_conversation_service()

        self.poll_timeout = poll_timeout
        self.poll_limit = poll_limit
        self.idle_sleep = idle_sleep
        self.error_backoff = error_backoff

        # --- runtime bookkeeping (also surfaced by stats()) ---
        self.processed: int = 0
        self.skipped: int = 0
        self.failed: int = 0
        self.errors: List[Dict[str, Any]] = []
        self.delivered: List[Dict[str, Any]] = []
        self.last_error: Optional[str] = None
        self.polls: int = 0

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ----------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------

    def is_running(self) -> bool:
        """True while the polling thread is alive."""

        return (
            self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> bool:
        """
        Start the polling thread.

        Returns
        -------
        bool
            True when a new thread was started, False when the worker
            was already running (idempotent - never starts a second
            loop for the same worker).

        Raises
        ------
        RuntimeError
            If the Telegram integration is not connected yet: polling
            an unverified client would only produce errors.
        """

        if self.is_running():
            logger.info(
                "Telegram conversation worker already running; "
                "start() ignored."
            )
            return False

        if not self.integration.is_connected():

            raise RuntimeError(
                "Telegram is not connected. Call POST "
                "/api/integrations/telegram/connect before starting "
                "the conversation worker."
            )

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._loop,
            name="telegram-conversation-worker",
            # Daemon: the worker must never keep the process alive.
            daemon=True,
        )

        self._thread.start()

        logger.info(
            "Telegram conversation worker started "
            "(poll_timeout=%ss, limit=%s).",
            self.poll_timeout, self.poll_limit,
        )

        return True

    def stop(
        self,
        timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> bool:
        """
        Signal the polling loop to finish and wait for it.

        Parameters
        ----------
        timeout : float
            Seconds to wait for the thread to join.

        Returns
        -------
        bool
            True when the thread finished (or was never running),
            False if it was still alive when the timeout expired.
        """

        self._stop_event.set()

        thread = self._thread

        if thread is None:
            return True

        thread.join(timeout=timeout)

        if thread.is_alive():

            logger.warning(
                "Telegram conversation worker did not stop within "
                "%ss (thread left running).", timeout,
            )
            return False

        logger.info(
            "Telegram conversation worker stopped "
            "(processed=%s, skipped=%s, failed=%s).",
            self.processed, self.skipped, self.failed,
        )

        return True

    def _loop(self) -> None:
        """Poll until stopped, isolating every failure."""

        while not self._stop_event.is_set():

            try:
                self.run_once()
            except Exception as exc:

                # A polling failure must never kill the loop.
                self.last_error = str(exc)
                self.errors.append(
                    {"stage": "poll", "error": str(exc)}
                )

                logger.error(
                    "Telegram poll failed: %s", exc,
                )

                if self._stop_event.wait(self.error_backoff):
                    break

                continue

            # Cooperative pause: returns immediately when stop() sets
            # the event, so shutdown never waits out a full sleep.
            if self._stop_event.wait(self.idle_sleep):
                break

    # ----------------------------------------------------------
    # Work
    # ----------------------------------------------------------

    def run_once(self) -> int:
        """
        Fetch one batch of updates and answer every usable message.

        Returns
        -------
        int
            Number of messages successfully answered AND delivered.

        Raises
        ------
        Exception
            Anything raised by ``get_updates`` (network/upstream error)
            is propagated so ``_loop`` can apply its backoff.
        """

        messages = self.integration.get_updates(
            timeout=self.poll_timeout,
            limit=self.poll_limit,
            ack=True,
        )

        self.polls += 1

        handled = 0

        for message in messages or []:

            if self._stop_event.is_set():
                break

            if self._handle_message(message):
                handled += 1

        return handled

    def _handle_message(self, message) -> bool:
        """
        Answer one normalized ``IncomingMessage``.

        Returns True only when a reply was actually sent to Telegram.
        Every failure is recorded and swallowed: one poison message
        must never take the worker down.
        """

        chat_id = getattr(message, "chat_id", None)
        update_id = getattr(message, "update_id", None)

        text = getattr(message, "text", "") or ""

        # --- skip blank / non-text payloads ---
        if not text.strip():

            self.skipped += 1
            logger.debug(
                "Skipping blank Telegram message (update_id=%s).",
                update_id,
            )
            return False

        # --- 1. generate the persona reply ---
        try:
            result = self.service.handle_message(
                chat_id,
                text,
                sender_username=getattr(
                    message, "sender_username", None
                ),
            )
        except Exception as exc:

            self.failed += 1
            self.last_error = str(exc)
            self.errors.append(
                {
                    "stage": "handle",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "error": str(exc),
                }
            )

            logger.error(
                "Conversation handling failed for chat %s: %s",
                chat_id, exc,
            )
            return False

        reply = (result.get("reply") or "").strip()

        if not reply:

            self.skipped += 1
            logger.warning(
                "Empty persona reply for chat %s - nothing sent.",
                chat_id,
            )
            return False

        # --- 2. deliver it back to the chat ---
        # Truncated defensively: Telegram hard-rejects >4096 chars,
        # and the prompt already asks the model for short replies.
        try:
            sent = self.integration.send_message(
                chat_id,
                reply[:MAX_TEXT_LENGTH],
            )
        except Exception as exc:

            self.failed += 1
            self.last_error = str(exc)
            self.errors.append(
                {
                    "stage": "send",
                    "chat_id": chat_id,
                    "update_id": update_id,
                    "error": str(exc),
                }
            )

            logger.error(
                "Sending Telegram reply to chat %s failed: %s",
                chat_id, exc,
            )
            return False

        self.processed += 1
        self.delivered.append(
            {
                "chat_id": chat_id,
                "message_id": (
                    sent.get("message_id")
                    if isinstance(sent, dict) else None
                ),
                "text": reply[:MAX_TEXT_LENGTH],
            }
        )

        logger.info(
            "Answered chat %s (update_id=%s, turn=%s).",
            chat_id, update_id, result.get("turn"),
        )

        return True

    # ----------------------------------------------------------
    # Introspection
    # ----------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """
        JSON-safe snapshot of the worker (used by the status endpoint).
        """

        return {
            "running": self.is_running(),
            "polls": self.polls,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "error_count": len(self.errors),
            "last_error": self.last_error,
            "active_chats": self.service.active_chat_ids(),
        }


# ----------------------------------------------------------
# Process-wide worker (shared by the HTTP endpoints)
# ----------------------------------------------------------

_WORKER: Optional[TelegramConversationWorker] = None


def get_worker() -> Optional[TelegramConversationWorker]:
    """The current worker, or None when one was never started."""

    return _WORKER


def start_worker(
    integration,
    service: Optional[TelegramConversationService] = None,
    **worker_kwargs,
) -> TelegramConversationWorker:
    """
    Create (if needed) and start the shared worker.

    Returns the running worker without restarting it when one is
    already polling, so repeated POSTs to the start endpoint are
    idempotent.
    """

    global _WORKER

    if _WORKER is not None and _WORKER.is_running():
        return _WORKER

    _WORKER = TelegramConversationWorker(
        integration,
        service=service,
        **worker_kwargs,
    )

    _WORKER.start()

    return _WORKER


def stop_worker(
    timeout: float = DEFAULT_STOP_TIMEOUT,
) -> bool:
    """
    Stop the shared worker.

    Returns
    -------
    bool
        True when it stopped cleanly (or was not running), False when
        the polling thread outlived ``timeout``.
    """

    global _WORKER

    if _WORKER is None or not _WORKER.is_running():
        return True

    return _WORKER.stop(timeout=timeout)


def reset_worker() -> None:
    """Stop and forget the shared worker (tests / full restart)."""

    global _WORKER

    if _WORKER is not None:

        try:
            _WORKER.stop()
        finally:
            _WORKER = None
