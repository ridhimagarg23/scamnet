"""
Telegram integration client for SCAMNET.

Role in SCAMNET
---------------
Telegram is the communication channel with the suspicious actor:
receive their messages, send the undercover persona's replies, keep the
conversation alive and eventually trigger the investigation agent on
every turn.

Implemented MVP (real Bot API, no AI wiring yet)
------------------------------------------------
This client now talks to the REAL Telegram Bot API over HTTPS:

* ``verify_connection()`` - ``getMe``: proves the configured bot token
  is valid and returns the bot's public identity.
* ``connect()``           - verifies via ``getMe`` and marks the
  integration as genuinely connected (``connected=True`` only after
  Telegram itself accepted the credentials).
* ``check_health()``      - TTL-cached ``getMe`` ping so status
  endpoints stay honest without hammering the API.
* ``get_updates()``       - long-polling ``getUpdates`` with an
  in-memory acknowledgement cursor (``offset``), returning messages
  normalized into ``IncomingMessage`` (see models.py). No public
  webhook server is required.
* ``send_message()``      - ``sendMessage`` to a specific chat_id
  (with UTF-16-aware length checking, which is what Telegram actually
  enforces - a 4096-emoji message is far beyond its byte budget).
* ``send_chat_action()``  - ``sendChatAction`` (the "typing..." hint).
* ``set_my_commands()``   - ``setMyCommands`` (declares /start in the
  Telegram UI so an operator always has a way to ping the bot).
* ``delete_webhook()``    - ``deleteWebhook``: a webhook configured by
  anything else (BotFather, another deployment) makes ``getUpdates``
  return 409 forever, which looks exactly like "the bot never replies".
  ``connect()`` clears it, so polling always works.
* ``disconnect()``        - drops the verified-session state (no
  background loop or webhook is owned by this client).

Deliberately NOT implemented yet (next increments):
* no background polling loop / autonomous investigation orchestrator;
* incoming messages are NOT routed to InvestigationAgent /
  ConversationAgent / AdaptiveInvestigationEngine - this step only
  proves reliable Telegram communication;
* no webhook support; no media handling (text messages only).

Security
--------
The bot token lives ONLY in server-side configuration (``.env``) and
is embedded solely in the Bot API request URL. It is never:
* returned in any status payload or API response;
* included in exception messages or log lines (network errors are
  reduced to their exception TYPE name because httpx errors can carry
  the request URL; every upstream-provided string additionally passes
  through ``_redact()``);
* persisted to any database or the frontend.

Testing
-------
The HTTP layer is injectable (``http_client`` constructor argument),
so unit tests run fully offline against a stub - see
tests/test_telegram_integration.py.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from integrations.base import (
    BaseIntegration,
    IntegrationConnectionError,
    IntegrationNotConfiguredError,
)
from integrations.telegram.models import (
    IncomingMessage,
    normalize_update,
)

logger = logging.getLogger("SCAMNET-Telegram")


# --------------------------------------------------
# Provider-specific failure type
# --------------------------------------------------

class TelegramAPIError(IntegrationConnectionError):
    """
    A Telegram Bot API call failed (network problem, rejected token,
    upstream ``ok=false`` response).

    The message is always sanitised - it never contains the bot token
    or the raw request URL. ``error_code`` carries Telegram's numeric
    error code when one was returned (e.g. 401 Unauthorized) and
    ``retry_after`` mirrors ``parameters.retry_after`` - Telegram's own
    "wait this many seconds" hint for HTTP 429 responses.
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[int] = None,
        retry_after: Optional[int] = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


# --------------------------------------------------
# Client
# --------------------------------------------------

# Telegram allows 0-~50 s long-poll waits; 25 s is a safe default.
DEFAULT_LONG_POLL_TIMEOUT = 25

# Telegram rejects sendMessage texts longer than this MANY UTF-16 code
# units (NOT Python characters - one emoji counts as two).
MAX_TEXT_LENGTH = 4096

# Plain requests (getMe / sendMessage) use a short timeout.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# check_health() caches its getMe result for this long so frequent
# status polling does not hit Telegram on every request.
HEALTH_CACHE_TTL_SECONDS = 60.0


def utf16_length(text: str) -> int:
    """
    Number of UTF-16 code units in ``text`` - the unit Telegram counts.

    Python's ``len()`` counts code points, so a 3000-emoji reply passes
    ``len(text) <= 4096`` but is rejected by Telegram with HTTP 400
    "message is too long". Every outbound length check/truncation must
    therefore go through this function.
    """

    if not isinstance(text, str):
        raise ValueError("text must be a string.")

    return len(text.encode("utf-16-le")) // 2


def truncate_for_telegram(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    """
    Truncate ``text`` so it fits Telegram's UTF-16 budget.

    Cuts on a code-point boundary (never inside a surrogate pair) and
    appends an ellipsis when something was dropped.
    """

    if not isinstance(text, str):
        raise ValueError("text must be a string.")

    if utf16_length(text) <= limit:
        return text

    # Reserve one code unit for the ellipsis.
    budget = limit - 1
    out = []
    used = 0

    for character in text:
        width = utf16_length(character)
        if used + width > budget:
            break
        out.append(character)
        used += width

    return "".join(out) + "\u2026"


class TelegramIntegration(BaseIntegration):
    """
    Real Telegram Bot API client (getMe / getUpdates / sendMessage).

    Parameters
    ----------
    settings : config.Settings
        Server-side configuration singleton (holds TELEGRAM_BOT_TOKEN).
    http_client : object | None
        Optional HTTP client exposing ``post(url, json=..., timeout=...)``
        (an ``httpx.Client`` by default). Injectable for offline tests.
    """

    id = "telegram"
    name = "Telegram"
    purpose = "Communication & intelligence gathering"

    # Bot token issued by @BotFather - lives ONLY in the server .env.
    required_settings = ("TELEGRAM_BOT_TOKEN",)
    # API root; overridable for local Bot API servers.
    optional_settings = ("TELEGRAM_API_BASE",)

    # The real connect flow (getMe verification) is implemented.
    connect_implemented = True

    setup_instructions = (
        "Create a bot with @BotFather, set TELEGRAM_BOT_TOKEN in the "
        "server-side .env and restart the backend. The bot must be "
        "allowed to message the investigator's chat."
    )

    def __init__(self, settings, http_client: Optional[Any] = None):
        super().__init__(settings)

        # Injectable HTTP transport (tests pass a stub; production uses
        # a lazily-created shared httpx.Client with connection pooling).
        self._http_client = http_client

        # Public bot identity captured by a successful getMe.
        # Example: {"id": 123, "username": "scamnet_intel_bot",
        #           "first_name": "SCAMNET Intel"} - NOT secret.
        self._bot_info: Optional[Dict[str, Any]] = None

        # Long-polling acknowledgement cursor: the next getUpdates
        # call asks for updates with id >= this value. In-memory only
        # (MVP): a process restart re-reads unacknowledged updates
        # (Telegram keeps them for ~24 h).
        self._update_offset: Optional[int] = None

        # check_health() TTL cache (avoids a getMe per status poll).
        self._last_health_at: Optional[float] = None
        self._last_health_ok: Optional[bool] = None

        # True once connect() confirmed no webhook can swallow updates.
        # Surfaced (secret-free) by get_connection_info().
        self._webhook_cleared: Optional[bool] = None

        # Set by get_updates(): True when the batch Telegram returned
        # was PENDING mail (no offset cursor yet), False when it was a
        # long-poll wake-up. The worker uses it to answer with a
        # one-time "I am alive" greeting instead of a fresh case.
        self.last_poll_had_pending: bool = False

    # ----------------------------------------------
    # HTTP plumbing (token-safe)
    # ----------------------------------------------

    def _get_http_client(self):
        """Lazily create the shared httpx.Client (or use the stub)."""

        if self._http_client is None:
            self._http_client = httpx.Client()
        return self._http_client

    def _redact(self, text: str) -> str:
        """
        Defence in depth: strip the bot token out of ANY string before
        it can reach an exception message, an API response or a log.
        """

        token = getattr(self.settings, "TELEGRAM_BOT_TOKEN", None)
        if token and isinstance(text, str):
            text = text.replace(token, "[REDACTED]")
        return text

    def _call_api(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        http_timeout: Optional[httpx.Timeout] = None,
    ) -> Any:
        """
        Execute one Bot API method (always POST + JSON body so long
        texts never hit URL length limits) and return its ``result``.

        Raises
        ------
        IntegrationNotConfiguredError - no token configured.
        TelegramAPIError              - network failure, non-JSON body
                                        or ``ok=false`` (sanitised
                                        message, never contains the
                                        token or the request URL).
        """

        token = getattr(self.settings, "TELEGRAM_BOT_TOKEN", None)
        if not token:
            raise IntegrationNotConfiguredError(
                "Telegram is not configured: set TELEGRAM_BOT_TOKEN in "
                "the server-side .env (never in frontend code)."
            )

        api_base = (
            getattr(self.settings, "TELEGRAM_API_BASE", None)
            or "https://api.telegram.org"
        ).rstrip("/")

        # The token appears ONLY here, inside the server-side URL.
        url = f"{api_base}/bot{token}/{method}"

        try:
            response = self._get_http_client().post(
                url,
                json=payload or {},
                timeout=http_timeout or DEFAULT_HTTP_TIMEOUT,
            )
        except Exception as exc:
            # Never propagate the raw error text: httpx exceptions can
            # carry the request object/URL, which embeds the token.
            # Only the exception TYPE name is safe to surface.
            logger.warning(
                "Telegram '%s' request failed at transport level (%s).",
                method,
                type(exc).__name__,
            )
            raise TelegramAPIError(
                f"Network error while calling Telegram Bot API method "
                f"'{method}' ({type(exc).__name__}). Check connectivity "
                f"and TELEGRAM_API_BASE."
            ) from None

        status_code = getattr(response, "status_code", None)

        try:
            body = response.json()
        except Exception:
            raise TelegramAPIError(
                f"Telegram Bot API returned a non-JSON response for "
                f"'{method}' (HTTP {status_code})."
            ) from None

        if not isinstance(body, dict) or body.get("ok") is not True:
            error_code = (
                body.get("error_code")
                if isinstance(body, dict)
                else None
            )
            description = (
                body.get("description")
                if isinstance(body, dict)
                else None
            )

            # Telegram's structured hint for 429s: how long to wait.
            parameters = (
                body.get("parameters") if isinstance(body, dict) else None
            )
            retry_after = None
            if isinstance(parameters, dict):
                candidate = parameters.get("retry_after")
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    retry_after = candidate

            message = f"Telegram Bot API error on '{method}'"
            if isinstance(error_code, int):
                message += f" (code {error_code})"
            if isinstance(description, str) and description:
                # Redact before the description can reach any caller.
                message += f": {self._redact(description)}"
            else:
                message += f" (HTTP {status_code})"
            if retry_after is not None:
                message += f" [retry after {retry_after}s]"

            logger.warning(message)
            raise TelegramAPIError(
                message, error_code=error_code, retry_after=retry_after
            )

        return body.get("result")

    # ----------------------------------------------
    # Input validation helpers
    # ----------------------------------------------

    @staticmethod
    def _validate_chat_id(chat_id: Any) -> int:
        """
        Return ``chat_id`` when it is a usable Telegram chat id.

        Raises ValueError otherwise (before any network call, so a bad
        id cannot waste a round trip).
        """

        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("chat_id must be an integer Telegram chat id.")
        if chat_id == 0:
            raise ValueError("chat_id cannot be 0.")
        return chat_id

    # ----------------------------------------------
    # Bot API operations
    # ----------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """
        Call ``getMe`` to prove the configured bot token is valid.

        Returns the bot's public identity:
            {"id": int, "username": str | None, "first_name": str | None}

        Raises TelegramAPIError when Telegram rejects the token or the
        network call fails.
        """

        result = self._call_api("getMe")

        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramAPIError(
                "Unexpected getMe response from Telegram (missing bot id)."
            )

        return {
            "id": result.get("id"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
        }

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = DEFAULT_LONG_POLL_TIMEOUT,
        limit: int = 100,
        ack: bool = True,
    ) -> List[IncomingMessage]:
        """
        Retrieve incoming updates with Bot API LONG POLLING.

        Parameters
        ----------
        offset : int | None
            Explicit update offset. When None the internal cursor is
            used (and advanced, if ``ack``) so every update is returned
            exactly once per process.
        timeout : int
            Long-poll wait in seconds (0 = return immediately with
            whatever is queued; Telegram accepts roughly 0-50).
        limit : int
            Maximum updates per call (1-100, Telegram's bounds).
        ack : bool
            Advance the internal cursor past the fetched updates so
            Telegram stops returning them. ``ack=False`` peeks.

        Returns
        -------
        list[IncomingMessage]
            Normalized inbound text messages. Non-text / non-message
            updates are skipped (but still acknowledged).
        """

        if not isinstance(timeout, int) or isinstance(timeout, bool) or not (0 <= timeout <= 50):
            raise ValueError("timeout must be an int in [0, 50] seconds.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 100):
            raise ValueError("limit must be an int in [1, 100].")

        effective_offset = self._update_offset if offset is None else offset

        payload: Dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            # MVP consumes plain inbound messages only.
            "allowed_updates": ["message"],
        }
        if effective_offset is not None:
            payload["offset"] = effective_offset

        # The HTTP read timeout must exceed the long-poll wait, or the
        # client would give up while Telegram is still holding the
        # connection open waiting for updates.
        http_timeout = httpx.Timeout(timeout + 10.0, connect=5.0)

        result = self._call_api(
            "getUpdates", payload=payload, http_timeout=http_timeout
        )

        updates = result if isinstance(result, list) else []

        # With no cursor yet, everything Telegram returns is PENDING
        # mail (it queued those updates while nothing polled). Single
        # inboxes deliver in that order, so the oldest update is very
        # likely to be the /start Telegram itself sends when the user
        # first opens the bot - the worker uses this flag to answer it
        # with a one-time liveness greeting instead of opening a case.
        self.last_poll_had_pending = bool(updates) and effective_offset is None

        messages: List[IncomingMessage] = []
        last_update_id: Optional[int] = None

        for update in updates:
            if not isinstance(update, dict):
                continue

            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                last_update_id = (
                    update_id
                    if last_update_id is None
                    else max(last_update_id, update_id)
                )

            normalized = normalize_update(update)
            if normalized is not None:
                messages.append(normalized)

        if offset is None and ack and last_update_id is not None:
            # Acknowledge everything we saw so the next poll (or any
            # other consumer) only receives NEWER updates.
            self._update_offset = last_update_id + 1

        return messages

    def send_message(self, chat_id: int, text: str) -> Dict[str, Any]:
        """
        Send one text message through ``sendMessage``.

        Parameters
        ----------
        chat_id : int
            Target chat (private chats are positive, groups/super-
            groups negative). Must be an integer id - @usernames are
            not accepted in this MVP.
        text : str
            Non-empty message text, at most 4096 characters
            (Telegram's hard limit).

        Returns
        -------
        dict
            ``{"chat_id": int, "message_id": int}`` of the sent message.
        """

        # ---- strict input validation (before any network call) ----
        self._validate_chat_id(chat_id)

        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string.")

        # Telegram counts UTF-16 code units, not Python characters.
        if utf16_length(text) > MAX_TEXT_LENGTH:
            raise ValueError(
                f"text exceeds Telegram's {MAX_TEXT_LENGTH}-unit limit."
            )

        result = self._call_api(
            "sendMessage", payload={"chat_id": chat_id, "text": text}
        )

        if not isinstance(result, dict) or not isinstance(
            result.get("message_id"), int
        ):
            raise TelegramAPIError(
                "Unexpected sendMessage response from Telegram "
                "(missing message_id)."
            )

        chat = result.get("chat")
        return {
            "chat_id": chat.get("id") if isinstance(chat, dict) else chat_id,
            "message_id": result.get("message_id"),
        }

    def send_chat_action(
        self, chat_id: int, action: str = "typing"
    ) -> bool:
        """
        Send a chat action (``typing`` by default) to ``chat_id``.

        Used by the conversation worker to show the "typing..." hint
        while the LLM writes the persona's reply, so a long turn does
        not look like a dead bot. Best-effort by design - Telegram
        throttles this call, and a failure must never break a reply.

        Returns
        -------
        bool
            True when Telegram acknowledged the action.
        """

        self._validate_chat_id(chat_id)

        if not isinstance(action, str) or not action.strip():
            raise ValueError("action must be a non-empty string.")

        result = self._call_api(
            "sendChatAction",
            payload={"chat_id": chat_id, "action": action},
        )

        return bool(result)

    def set_my_commands(self, commands: List[Dict[str, str]]) -> bool:
        """
        Publish the bot's command list (``setMyCommands``).

        Declaring ``/start`` makes Telegram show a command menu next to
        the input box, which gives the operator a guaranteed way to
        check that the bot is alive.

        Parameters
        ----------
        commands : list[dict]
            ``[{"command": "start", "description": "..."}, ...]``
            (1-100 entries, command text without the leading slash).

        Returns
        -------
        bool
            True when Telegram accepted the list.
        """

        if not isinstance(commands, list) or not commands:
            raise ValueError("commands must be a non-empty list.")

        for entry in commands:
            if not isinstance(entry, dict):
                raise ValueError("every command must be a dict.")
            command = entry.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("every command needs a non-empty name.")
            if command.startswith("/"):
                raise ValueError(
                    "command names must not start with '/' (Telegram "
                    "adds it)."
                )

        result = self._call_api(
            "setMyCommands", payload={"commands": commands}
        )

        return bool(result)

    def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        """
        Remove any webhook so long polling receives updates.

        A bot can be subscribed to EITHER a webhook OR ``getUpdates``.
        If a webhook is (or was) configured - by BotFather, a previous
        deployment, or another service - every ``getUpdates`` call
        answers HTTP 409 ``Conflict: can't use getUpdates method while
        webhook is active``, and the bot silently never replies. This
        call is idempotent and cheap, so ``connect()`` runs it.

        Parameters
        ----------
        drop_pending_updates : bool
            Ask Telegram to discard the update backlog as well. Kept
            False by default so messages that arrived while the server
            was down are still delivered.

        Returns
        -------
        bool
            True when Telegram acknowledged the call.
        """

        result = self._call_api(
            "deleteWebhook",
            payload={"drop_pending_updates": bool(drop_pending_updates)},
        )

        return bool(result) if isinstance(result, bool) else True

    # ----------------------------------------------
    # Lifecycle (BaseIntegration contract)
    # ----------------------------------------------

    def connect(self) -> None:
        """
        Verify the bot token against Telegram (``getMe``) and mark the
        integration connected ONLY when Telegram accepted it.

        Raises
        ------
        IntegrationNotConfiguredError - no token in server config.
        TelegramAPIError              - token rejected / network down.
        """

        if not self.is_configured():
            raise IntegrationNotConfiguredError(
                "Telegram is not configured: set TELEGRAM_BOT_TOKEN in "
                "the server-side .env (never in frontend code)."
            )

        try:
            bot_info = self.verify_connection()
        except IntegrationConnectionError as exc:
            # Record the sanitised failure so get_status() reports the
            # honest ERROR state, then propagate for the API layer.
            self._connected = False
            self._last_error = str(exc)
            raise

        # ----------------------------------------------------------
        # Clear any webhook BEFORE declaring the connection usable.
        # ----------------------------------------------------------
        # A webhook and getUpdates are mutually exclusive: while one is
        # registered, Telegram answers every getUpdates call with HTTP
        # 409 and the bot never sees a message - the classic "the bot
        # is connected but never replies" failure. Failing the connect
        # is the honest outcome here: better an explicit error in the
        # dashboard than a silently mute bot.
        try:
            self.delete_webhook()
            self._webhook_cleared = True

        except IntegrationConnectionError as exc:
            self._connected = False
            self._webhook_cleared = False
            self._last_error = (
                "Telegram still has a webhook configured, which blocks "
                "long polling (getUpdates). Remove it in BotFather / "
                "with deleteWebhook, then connect again."
            )
            logger.warning("Telegram deleteWebhook failed: %s", exc)
            raise TelegramAPIError(self._last_error) from None

        # ----------------------------------------------------------
        # Publish the command menu (best effort).
        # ----------------------------------------------------------
        # "start" gives the operator a guaranteed liveness probe: send
        # /start to the bot and it answers even when the LLM is down.
        try:
            self.set_my_commands([
                {
                    "command": "start",
                    "description": "Check this bot is online",
                },
            ])
        except IntegrationConnectionError as exc:
            # Cosmetic only - never block a working connection.
            logger.warning("Could not publish bot commands: %s", exc)

        self._bot_info = bot_info
        self._connected = True
        self._last_error = None
        # Fresh getMe just proved the session - warm the health cache
        # so an immediate is_connected() does not call Telegram again.
        self._last_health_ok = True
        self._last_health_at = time.monotonic()

        logger.info(
            "Telegram connected as @%s (bot id %s).",
            bot_info.get("username"),
            bot_info.get("id"),
        )

    def disconnect(self) -> None:
        """
        Drop the verified-session state.

        The MVP has no background polling loop or webhook to tear
        down; the update acknowledgement cursor is intentionally
        preserved so reconnecting does not re-deliver consumed
        updates.
        """

        self._connected = False
        self._bot_info = None
        self._last_health_ok = None
        self._last_health_at = None
        self.last_poll_had_pending = False
        logger.info("Telegram session state cleared (disconnected).")

    def check_health(self) -> bool:
        """
        Verify the live session with a TTL-cached ``getMe`` ping.

        A failed health check flips the integration back to
        disconnected (honest status) and records the sanitised error.
        """

        if not self._connected:
            return False

        now = time.monotonic()
        if (
            self._last_health_ok is not None
            and self._last_health_at is not None
            and (now - self._last_health_at) < HEALTH_CACHE_TTL_SECONDS
        ):
            return self._last_health_ok

        try:
            self.verify_connection()
        except (IntegrationConnectionError, IntegrationNotConfiguredError) as exc:
            self._connected = False
            self._last_error = str(exc)
            self._last_health_ok = False
            self._last_health_at = now
            logger.warning("Telegram health check failed: %s", exc)
            return False

        self._last_error = None
        self._last_health_ok = True
        self._last_health_at = now
        return True

    # ----------------------------------------------
    # Status enrichment
    # ----------------------------------------------

    def get_connection_info(self) -> dict:
        """Public bot identity (never the token) for the status API."""

        if self._connected and self._bot_info:
            return {
                "bot_id": self._bot_info.get("id"),
                "bot_username": self._bot_info.get("username"),
                "bot_name": self._bot_info.get("first_name"),
                # True when connect() confirmed getUpdates is not
                # competing with a webhook.
                "webhook_cleared": self._webhook_cleared,
            }
        return {}
