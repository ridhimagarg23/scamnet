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
* **Never send an empty reply.** Blank/whitespace inbound messages,
  empty model output and over-long model output are handled without
  breaking the chat (over-long replies are truncated on a UTF-16
  boundary, which is the unit Telegram actually enforces).
* **Always answer a wake-up.** ``/start`` - the message Telegram sends
  when a chat first opens a bot, and the one ping an operator can
  always send - is answered with a fixed greeting that needs NO LLM.
  A backlog of pending updates is acknowledged the same way, so a bot
  started after downtime never replies with a stale persona line.
* **Give up loudly on dead credentials.** Repeated 401/403 answers
  stop the loop and record why, instead of hammering Telegram forever.
* **Stop cooperatively.** ``stop()`` signals an event; the loop checks
  it between polls and before every send, so shutdown is prompt even
  during a long poll.
* **No secrets.** The worker only ever handles normalized messages and
  reply text - the bot token stays inside the Telegram client.

Two ways to run it
------------------
1. **Background polling worker** (default, ``start()``/``stop()``):
   a daemon thread long-polls Telegram. Fastest replies, but some
   hosts suspend the process between HTTP requests.
2. **Fetch mode** (``run_once()``, exposed by
   ``POST /api/telegram/conversation/fetch``): the scheduler/cron calls
   the endpoint; each call polls once, answers everything that arrived
   and returns the outcome. Works everywhere, including hosts that
   cannot keep a thread alive.
"""

import datetime
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from integrations.telegram.client import (
    MAX_TEXT_LENGTH,
    TelegramAPIError,
    truncate_for_telegram,
)

from tools.conversation_service import (
    TelegramConversationService,
    get_conversation_service,
    is_start_command,
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

# Consecutive authorization failures (401/403 from Telegram, i.e. the
# token was revoked or replaced) before the loop gives up and stops.
AUTH_FAILURE_LIMIT = 5

# Every this many polls the worker logs a heartbeat, so "is the bot
# actually listening?" is answerable from the server log alone.
HEARTBEAT_EVERY_POLLS = 60

# Chat action sent while the persona reply is generated (best effort).
TYPING_ACTION = "typing"

# Telegram answers HTTP 429 with parameters.retry_after when a chat is
# being messaged too fast. Waiting up to this many seconds and sending
# once more turns a lost reply into a delivered one; longer waits are
# left to the next inbound message instead of blocking the loop.
MAX_SEND_RETRY_WAIT = 5.0

# Serializes every getUpdates call in the process. Telegram hands each
# update to ONE getUpdates request, and answers the older request with
# HTTP 409 ("terminated by other getUpdates request") when two overlap -
# which loses messages. The background polling thread and the manual
# fetch endpoint therefore share this lock.
_POLL_LOCK = threading.Lock()


def _looks_like_auth_failure(exc: Exception) -> bool:
    """
    True when an exception means "these credentials are dead".

    Telegram answers HTTP 401 (``Unauthorized``) with
    ``error_code: 401`` when the bot token was revoked or replaced, and
    403 when the bot was blocked. Both are permanent until an operator
    acts, so the loop can stop instead of retrying forever.
    """

    error_code = getattr(exc, "error_code", None)

    if error_code in (401, 403):
        return True

    message = str(exc).lower()

    return (
        "unauthorized" in message
        or "forbidden" in message
        or "bot was blocked" in message
    )


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
        send_wake_message: bool = True,
    ):

        self.integration = integration
        self.service = service or get_conversation_service()

        self.poll_timeout = poll_timeout
        self.poll_limit = poll_limit
        self.idle_sleep = idle_sleep
        self.error_backoff = error_backoff

        #: Answer a pending backlog / ``/start`` with the liveness
        #: greeting (once per process). Disable to keep the loop purely
        #: reactive.
        self.send_wake_message = send_wake_message

        # --- runtime bookkeeping (also surfaced by stats()) ---
        self.processed: int = 0
        self.skipped: int = 0
        self.failed: int = 0
        self.greetings: int = 0
        self.errors: List[Dict[str, Any]] = []
        self.delivered: List[Dict[str, Any]] = []
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[str] = None
        self.polls: int = 0
        self.consecutive_failures: int = 0
        self.stopped_reason: Optional[str] = None

        # Wake-up bookkeeping: an explicit /start is always answered
        # (cheap, and it is the operator's liveness probe).
        self.wake_message_sent: bool = False

        #: True when the first poll of this process found updates that
        #: arrived while nothing was listening (diagnostics only).
        self.pending_backlog_seen: bool = False

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ----------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------

    @staticmethod
    def _fetch_in_progress() -> bool:
        """
        True while a manual ``fetch_once()`` call is polling.

        ``fetch_once`` holds ``_FETCH_LOCK``; a non-blocking acquire
        therefore tells us whether a fetch is in flight.
        """

        if _FETCH_LOCK.acquire(blocking=False):
            _FETCH_LOCK.release()
            return False

        return True

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

        if self._fetch_in_progress():
            raise RuntimeError(
                "A manual Telegram fetch (POST /api/telegram/"
                "conversation/fetch) is running right now. Telegram "
                "delivers each update to ONE getUpdates call, so polling "
                "and fetching at the same time would split messages "
                "between them. Retry in a moment."
            )

        self._stop_event.clear()
        self.stopped_reason = None

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
                self.run_once(should_abort=self._stop_event.is_set)

            except Exception as exc:

                # A polling failure must never kill the loop.
                self.consecutive_failures += 1
                self.last_error = str(exc)
                self.errors.append(
                    {"stage": "poll", "error": str(exc)}
                )

                logger.error(
                    "Telegram poll failed (%s in a row): %s",
                    self.consecutive_failures, exc,
                )

                # Dead credentials (token revoked / replaced) will never
                # recover on their own: stop loudly instead of hammering
                # Telegram with 401s forever.
                if (
                    _looks_like_auth_failure(exc)
                    and self.consecutive_failures >= AUTH_FAILURE_LIMIT
                ):
                    self.stopped_reason = "telegram_authorization_failed"
                    logger.error(
                        "Stopping the Telegram worker: %s consecutive "
                        "authorization failures. Reconnect the bot "
                        "(POST /api/integrations/telegram/connect) after "
                        "checking TELEGRAM_BOT_TOKEN.",
                        self.consecutive_failures,
                    )
                    self._stop_event.set()
                    break

                if self._stop_event.wait(self.error_backoff):
                    break

                continue

            self.consecutive_failures = 0

            # Heartbeat: "is the bot listening?" answers itself in the
            # log even when nothing is happening.
            if self.polls and self.polls % HEARTBEAT_EVERY_POLLS == 0:
                logger.info(
                    "Telegram worker heartbeat: %s polls, %s answered, "
                    "%s skipped, %s failed (last poll %s).",
                    self.polls, self.processed, self.skipped,
                    self.failed, self.last_poll_at,
                )

            # Cooperative pause: returns immediately when stop() sets
            # the event, so shutdown never waits out a full sleep.
            if self._stop_event.wait(self.idle_sleep):
                break

    # ----------------------------------------------------------
    # Work
    # ----------------------------------------------------------

    def run_once(self, should_abort: Optional[Callable[[], bool]] = None) -> int:
        """
        Fetch one batch of updates and answer every usable message.

        Parameters
        ----------
        should_abort : callable | None
            Optional predicate checked before each message. The
            background loop passes its stop event so a shutdown is
            prompt; the fetch endpoint passes None, because a *previous*
            stop must not silently disable fetch mode (it used to: the
            worker-wide stop event stays set after ``stop()``).

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

        # Only one getUpdates may be in flight per bot: two overlapping
        # polls make Telegram hand the same update to one call and 409
        # the other (which silently drops messages). The polling thread
        # and the fetch endpoint both go through this lock.
        with _POLL_LOCK:

            messages = self.integration.get_updates(
                timeout=self.poll_timeout,
                limit=self.poll_limit,
                ack=True,
            )

        self.polls += 1
        self.last_poll_at = _utc_now()

        # A first poll that finds mail means nothing was listening while
        # those messages arrived (fresh start, restart, or a long gap
        # between fetches). They are answered normally - and an
        # automatic Telegram ``/start`` inside that backlog is answered
        # with the greeting by the handler below, so the operator gets a
        # sign of life without the persona breaking character mid-case.
        if getattr(self.integration, "last_poll_had_pending", False):

            self.pending_backlog_seen = True

            logger.info(
                "First poll found %s pending update(s); answering them "
                "from scratch.", len(messages),
            )

        handled = 0

        for message in messages or []:

            if should_abort is not None and should_abort():
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

        # --- 0. /start is answered without the LLM ---
        # Telegram sends /start itself when a chat first opens the bot,
        # and it is the operator's liveness probe. It must work even
        # when the LLM provider is down, so it never reaches an agent.
        if is_start_command(text):
            self._handle_start_command(chat_id, message)
            return False

        # --- 1. show "typing..." then generate the persona reply ---
        self._send_typing(chat_id)

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
        # Truncated defensively on Telegram's own unit (UTF-16 code
        # units, not Python characters): the prompt asks for short
        # replies, but a model that ignores it (or an emoji-heavy
        # reply) must not make the send fail with HTTP 400.
        outbound = truncate_for_telegram(reply, MAX_TEXT_LENGTH)

        if outbound != reply:
            logger.warning(
                "Persona reply for chat %s was longer than Telegram's "
                "limit and has been truncated (%s chars).",
                chat_id, len(reply),
            )

        try:
            sent = self._send_with_retry(chat_id, outbound)
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
                "text": outbound,
                "at": _utc_now(),
            }
        )

        # Keep the delivery log bounded (long-running deployments).
        if len(self.delivered) > 100:
            del self.delivered[:-100]

        logger.info(
            "Answered chat %s (update_id=%s, turn=%s).",
            chat_id, update_id, result.get("turn"),
        )

        return True

    # ----------------------------------------------------------
    # Small helpers (each one fail-safe)
    # ----------------------------------------------------------

    def _send_with_retry(self, chat_id: int, text: str):
        """
        Deliver one message, honouring Telegram's 429 ``retry_after``.

        A rate-limited send is retried exactly ONCE after the (capped)
        delay Telegram asked for; anything else propagates so the caller
        records the failure. Losing a persona reply silently would end
        the undercover thread, so this narrow retry matters.
        """

        try:
            return self.integration.send_message(chat_id, text)

        except TelegramAPIError as exc:

            retry_after = getattr(exc, "retry_after", None)
            is_rate_limited = getattr(exc, "error_code", None) == 429

            if not is_rate_limited and retry_after is None:
                raise

            wait = min(float(retry_after or 1), MAX_SEND_RETRY_WAIT)

            logger.warning(
                "Telegram rate-limited the reply to chat %s; retrying "
                "once in %ss.", chat_id, wait,
            )

            if self._stop_event.wait(wait):
                raise

            return self.integration.send_message(chat_id, text)

    def _send_typing(self, chat_id: int) -> None:
        """Best-effort "typing..." hint while the LLM writes a reply."""

        sender = getattr(self.integration, "send_chat_action", None)

        if not callable(sender):
            return

        try:
            sender(chat_id, TYPING_ACTION)
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            logger.debug(
                "Could not send the typing action for chat %s: %s",
                chat_id, exc,
            )

    def _handle_start_command(self, chat_id: int, message) -> bool:
        """
        Answer an explicit ``/start`` with the liveness greeting.

        Never touches the case state: Telegram sends ``/start`` by
        itself the first time a chat opens the bot, so greeting must not
        open a case (and must not need the LLM).
        """

        logger.info(
            "Answering a /start command from chat %s (update_id=%s).",
            chat_id, getattr(message, "update_id", None),
        )

        return self._deliver_greeting(chat_id, reason="start_command")

    def _deliver_greeting(self, chat_id: int, reason: str) -> bool:
        """
        Send the fixed liveness greeting to ``chat_id``.

        Never touches the case state (Telegram sends ``/start`` by
        itself the first time a chat opens the bot) and never needs the
        LLM, so the bot always answers *something* - even while the
        model provider is down.
        """

        try:
            greeting = self.service.handle_start(chat_id)

        except Exception as exc:  # noqa: BLE001 - never break the loop
            logger.warning(
                "Could not build the greeting for chat %s: %s",
                chat_id, exc,
            )
            return False

        text = (greeting.get("reply") or "").strip()

        if not text:
            return False

        try:
            sent = self._send_with_retry(chat_id, text)

        except Exception as exc:  # noqa: BLE001 - never break the loop
            self.failed += 1
            self.last_error = str(exc)
            self.errors.append(
                {"stage": "greet", "chat_id": chat_id, "error": str(exc)}
            )
            logger.warning(
                "Could not send the greeting to chat %s: %s", chat_id, exc
            )
            return False

        self.greetings += 1
        self.wake_message_sent = True
        self.delivered.append(
            {
                "chat_id": chat_id,
                "message_id": (
                    sent.get("message_id") if isinstance(sent, dict) else None
                ),
                "text": text,
                "at": _utc_now(),
                "kind": "greeting",
            }
        )

        logger.info(
            "Sent the liveness greeting to chat %s (%s).", chat_id, reason
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
            "greetings": self.greetings,
            "error_count": len(self.errors),
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
            "last_delivery": self.delivered[-1] if self.delivered else None,
            "consecutive_failures": self.consecutive_failures,
            "stopped_reason": self.stopped_reason,
            "wake_message_sent": self.wake_message_sent,
            "pending_backlog_seen": self.pending_backlog_seen,
            "poll_timeout": self.poll_timeout,
            "poll_limit": self.poll_limit,
            "active_chats": self.service.active_chat_ids(),
        }

    def diagnostics(self) -> Dict[str, Any]:
        """
        Deeper, secret-free snapshot for the status endpoint.

        Lets an operator answer "why is the bot not replying?" without
        reading server logs: exact polling parameters, the last poll
        time, the last five errors and the last five deliveries.
        """

        return {
            "stats": self.stats(),
            "recent_errors": self.errors[-5:],
            "recent_deliveries": self.delivered[-5:],
        }


# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------

def _utc_now() -> str:
    """UTC timestamp (seconds resolution) for status/diagnostics."""

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
    )


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


# ----------------------------------------------------------
# Fetch mode (no long-lived thread required)
# ----------------------------------------------------------
# The polling thread above is the fastest way to answer messages, but
# some hosts (serverless platforms, sleeping containers) freeze a
# process between HTTP requests, so a background thread silently stops
# polling. In fetch mode the caller polls instead:
#
#     POST /api/telegram/conversation/fetch     (cron / uptime pinger)
#
# Exactly ONE process-wide worker is used for both modes, and the thread
# and the fetch endpoint can never poll at the same time:
#
#   * every getUpdates call is serialized by one process-wide poll lock,
#     so a fetch can never race the background thread (overlapping polls
#     make Telegram hand an update to one call and 409 the other);
#   * ``start()`` refuses to run while a fetch is in flight;
#   * ``run_once`` is idempotent for already-acknowledged updates (the
#     Telegram offset cursor is advanced on ack).

_FETCH_LOCK = threading.Lock()
_FETCH_THREAD: Optional[threading.Thread] = None
_FETCH_STOP_EVENT = threading.Event()
_FETCH_WORKER_ENABLED = False


def get_or_create_worker(integration) -> TelegramConversationWorker:
    """
    Return the shared worker, creating it (not started) when needed.

    Used by the fetch endpoint so a scheduler can drive the loop even
    when nobody ever called ``/conversation/start``.
    """

    global _WORKER

    if _WORKER is None:
        _WORKER = TelegramConversationWorker(
            integration, service=get_conversation_service()
        )

    return _WORKER


def fetch_once(integration) -> Dict[str, Any]:
    """
    Run ONE poll-and-reply cycle with no long-lived thread.

    Safe to call from a web request or a cron job: polls are serialized
    process-wide (so the background thread can never fetch the same
    update batch in parallel), and a previous ``stop()`` does not
    suppress the fetch - the manual call always answers what it finds.

    Returns
    -------
    dict
        ``{"handled": int, "stats": {...}}`` where ``handled`` is the
        number of inbound messages answered and delivered in this call.
    """

    worker = get_or_create_worker(integration)

    # _FETCH_LOCK keeps two simultaneous HTTP fetches from double
    # polling; _POLL_LOCK (inside run_once) keeps this fetch from
    # racing the background thread.
    with _FETCH_LOCK:
        handled = worker.run_once()

    return {"handled": handled, "stats": worker.stats()}


def is_fetch_worker_enabled() -> bool:
    """True when the in-process fetch scheduler thread is running."""

    return (
        _FETCH_WORKER_ENABLED
        and _FETCH_THREAD is not None
        and _FETCH_THREAD.is_alive()
    )


def enable_fetch_worker(
    integration,
    interval: float = 1.0,
) -> bool:
    """
    Start the in-process fetch scheduler thread (opt-in).

    Use this only on a host that cannot call
    ``POST /api/telegram/conversation/fetch`` from outside (for example
    a PaaS that blocks both threads and inbound cron). The thread calls
    ``fetch_once`` every ``interval`` seconds.

    Returns True when the scheduler is running (idempotent).
    """

    global _FETCH_THREAD, _FETCH_WORKER_ENABLED

    if is_fetch_worker_enabled():
        return True

    _FETCH_STOP_EVENT.clear()
    _FETCH_WORKER_ENABLED = True

    def _tick() -> None:

        while not _FETCH_STOP_EVENT.wait(interval):

            # Never race the manual polling loop: it owns the updates
            # while it is alive.
            worker = _WORKER

            if worker is not None and worker.is_running():
                continue

            try:
                fetch_once(integration)
            except Exception as exc:  # noqa: BLE001 - keep scheduling
                logger.error("Fetch-worker poll failed: %s", exc)

    _FETCH_THREAD = threading.Thread(
        target=_tick,
        name="telegram-fetch-worker",
        daemon=True,
    )
    _FETCH_THREAD.start()

    logger.info(
        "Telegram fetch worker enabled (interval=%ss).", interval
    )

    return True


def disable_fetch_worker(timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
    """Stop the in-process fetch scheduler thread (idempotent)."""

    global _FETCH_THREAD, _FETCH_WORKER_ENABLED

    _FETCH_STOP_EVENT.set()
    _FETCH_WORKER_ENABLED = False

    thread = _FETCH_THREAD
    _FETCH_THREAD = None

    if thread is None:
        return True

    thread.join(timeout=timeout)

    return not thread.is_alive()


def reset_fetch_worker() -> None:
    """Stop and forget the fetch scheduler (tests / full restart)."""

    disable_fetch_worker()
    _FETCH_STOP_EVENT.clear()
