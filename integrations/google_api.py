"""
google_api.py
=============
Shared Google API plumbing for SCAMNET's Google integrations
(**Google Drive**, **Google Sheets** and **Gmail**).

Why this module exists
----------------------
Drive, Sheets and Gmail all speak the same authentication protocol:
an OAuth 2.0 access token is minted from a server-side credentials
JSON file (service account or authorized user) and then attached as a
``Authorization: Bearer ...`` header to plain REST calls. Implementing
that three times would mean three subtly different auth implementations
- so it lives here once, and the three clients in
``integrations/google_drive``, ``integrations/google_sheets`` and
``integrations/gmail`` only describe *what they do*, not *how they
authenticate*.

What the base class provides
----------------------------
* ``credentials_path()``     - resolve the credentials file (provider
                               specific setting, falling back to the
                               shared ``GOOGLE_CREDENTIALS_FILE``).
* ``configuration_issues()`` - honest validity checks (file exists,
                               readable, parseable JSON, supported
                               credential type) with secret-free and
                               path-free messages.
* ``_api_request()``         - authorized REST call with sanitised
                               error handling.
* ``connect()`` / ``disconnect()`` / ``check_health()`` - the real
                               lifecycle: ``connect()`` only succeeds
                               after the provider's ``verify_connection()``
                               made a genuine, authorized API call.
* ``get_connection_info()``  - secret-free facts (account e-mail,
                               spreadsheet id, ...) for the dashboard.

Credentials supported
---------------------
1. **Service account JSON** (``"type": "service_account"``) - the usual
   server-to-server setup. Share the Drive folder / spreadsheet with the
   service account's ``client_email``.
2. **Authorized user JSON** (``"type": "authorized_user"``) - the OAuth
   "refresh token" file produced by the OAuth consent flow. Required for
   Gmail, which service accounts can only use with Workspace
   domain-wide delegation.

Honest-status rule (see integrations/base.py)
---------------------------------------------
Nothing here fabricates a connection: a client is only marked connected
after ``verify_connection()`` received a real ``2xx`` from Google with
the expected payload. A failure is recorded as a sanitised
``GoogleAPIError`` (HTTP 401/403, network errors, ...) and reported as
``connection_failed`` (HTTP 502) by the API layer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from integrations.base import (
    BaseIntegration,
    IntegrationConnectionError,
    IntegrationNotConfiguredError,
)

logger = logging.getLogger("SCAMNET-Integrations-Google")


# --------------------------------------------------
# Tunables
# --------------------------------------------------

# check_health() reuses a recent successful verification for this long,
# so the dashboard's status polling does not hit the Google API on
# every request.
HEALTH_CACHE_TTL_SECONDS = 60.0

# Plain REST calls (metadata reads, small writes).
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Uploading a report file can take a moment longer.
UPLOAD_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=8.0)

# Credential "type" values google-auth understands.
SERVICE_ACCOUNT_TYPE = "service_account"
AUTHORIZED_USER_TYPE = "authorized_user"

# Shared, optional credentials file every Google client falls back to
# when its provider-specific setting is empty.
SHARED_CREDENTIALS_SETTING = "GOOGLE_CREDENTIALS_FILE"

# Patterns scrubbed from anything that could reach a log line, an
# exception message or an API response.
_REDACTION_PATTERNS = (
    # OAuth access tokens (short-lived, but still secret).
    re.compile(r"ya29\.[A-Za-z0-9._\-]+"),
    # OAuth refresh tokens ("1//0abc...").
    re.compile(r"1//[A-Za-z0-9._\-]+"),
    # OAuth client secrets ("GOCSPX-...").
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),
    # JSON / form encoded token material.
    re.compile(
        r"(?i)(access_token|refresh_token|client_secret|private_key)"
        r"\"?\s*[:=]\s*\"?[^\s\",}]+"
    ),
    # Service-account private keys that leaked into an error string.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class GoogleAPIError(IntegrationConnectionError):
    """
    A real Google API call failed.

    The message is always sanitised: it never contains the access
    token, the credentials-file path or raw credential material.
    ``status_code`` carries Google's HTTP status when there was one
    (e.g. 401 for a rejected credential, 404 for a missing file).
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class _Response:
    """
    Minimal ``google.auth.transport.Request`` response object.

    google-auth only requires the object returned by a transport call
    to expose ``status`` / ``headers`` / ``data``; defining our own tiny
    container keeps the adapter independent of google-auth internals.
    """

    __slots__ = ("status", "headers", "data")

    def __init__(self, status: int, headers: Dict[str, str], data: bytes):
        self.status = status
        self.headers = headers
        self.data = data


class _HttpxRequest:
    """
    A ``google.auth.transport.Request`` implemented with httpx.

    google-auth calls this object to exchange a refresh token for an
    access token (``https://oauth2.googleapis.com/token``). Routing it
    through the same injectable HTTP client as every other call means
    the whole integration can be unit-tested offline with a stub
    transport - no real Google account required.
    """

    def __init__(self, client, timeout: Optional[httpx.Timeout] = None):
        self._client = client
        self._timeout = timeout or DEFAULT_HTTP_TIMEOUT

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[Any] = None,
        **kwargs: Any,
    ) -> _Response:
        response = self._client.request(
            method,
            url,
            content=body,
            headers=headers or {},
            timeout=timeout or self._timeout,
        )

        raw = getattr(response, "content", None)
        if raw is None:
            # Stub transports in tests may only expose .json(); encode
            # it back so google-auth can read the token payload.
            try:
                raw = json.dumps(response.json()).encode("utf-8")
            except Exception:
                raw = b"{}"

        return _Response(
            status=int(getattr(response, "status_code", 0) or 0),
            headers=dict(getattr(response, "headers", {}) or {}),
            data=raw,
        )


def _safe_json(response) -> Any:
    """Return the parsed JSON body, or None when the body is not JSON."""

    try:
        return response.json()
    except Exception:
        return None


class GoogleApiIntegration(BaseIntegration):
    """
    Base class for every Google-backed SCAMNET integration.

    Subclasses declare:

    * ``credentials_setting`` - the ``config.Settings`` attribute that
      holds the credentials file path, e.g.
      ``GOOGLE_DRIVE_CREDENTIALS_FILE``;
    * ``scopes``              - the OAuth scopes the API calls need;
    * ``verify_connection()`` - one cheap, authorized read that proves
      the credentials really work and returns secret-free facts about
      the account (e.g. its e-mail address).

    Everything else (configuration validation, token refresh, REST
    plumbing, connect/disconnect/health, honest status bookkeeping) is
    inherited.
    """

    #: Human-facing API label used in error messages ("Google Drive").
    api_label = "Google"

    #: OAuth scopes required by this integration.
    scopes: Tuple[str, ...] = ()

    #: settings attribute holding this provider's credentials file.
    credentials_setting = ""

    #: Google's REST root, kept as a class attribute so tests can point
    #: it at a stub without touching the production URLs.
    api_base = "https://www.googleapis.com"

    # The real authentication flow is implemented here (service account
    # or authorized-user OAuth), so the dashboard may report
    # ``available=True`` once the server holds valid credentials.
    connect_implemented = True

    def __init__(self, settings, http_client: Optional[Any] = None):
        """
        Parameters
        ----------
        settings : config.Settings
            Shared server-side settings (holds the credentials path).
        http_client : object | None
            Optional HTTP transport exposing
            ``request(method, url, params=..., json=..., content=...,
            headers=..., timeout=...)`` (an ``httpx.Client`` by default).
            Injectable so every test runs fully offline.
        """

        super().__init__(settings)

        self._http_client = http_client

        # Cached google-auth credentials object (rebuilt on connect()).
        self._credentials = None

        # Actual secret VALUES read from the credentials file, so the
        # redactor can scrub them even when they do not match a known
        # token shape (a truncated/copy-pasted secret, for instance).
        self._secret_values: set = set()

        # Secret-free facts captured by a successful verify_connection().
        self._connection_info: Dict[str, Any] = {}

        # check_health() TTL cache.
        self._last_health_at: Optional[float] = None
        self._last_health_ok: Optional[bool] = None

    # ----------------------------------------------
    # HTTP plumbing
    # ----------------------------------------------

    def _get_http_client(self):
        """Lazily create the shared httpx.Client (or return the stub)."""

        if self._http_client is None:
            self._http_client = httpx.Client()
        return self._http_client

    # ----------------------------------------------
    # Configuration
    # ----------------------------------------------

    def credentials_path(self) -> Optional[str]:
        """
        Server-side path to the credentials JSON.

        Resolution order: this provider's own setting, then the shared
        ``GOOGLE_CREDENTIALS_FILE``. One Google credentials file can
        therefore serve Drive, Sheets and Gmail at once.
        """

        for key in (self.credentials_setting, SHARED_CREDENTIALS_SETTING):
            value = getattr(self.settings, key, None)
            if value:
                return str(value)
        return None

    def missing_settings(self) -> List[str]:
        """
        Names of required settings that are absent.

        The provider-specific credentials variable counts as present
        when the shared ``GOOGLE_CREDENTIALS_FILE`` is set instead.
        """

        if self.credentials_path():
            return []
        return [self.credentials_setting]

    def _credential_file_problem(self) -> Optional[str]:
        """
        Return a secret-free validity problem with the credentials file,
        or None when the file looks usable.
        """

        path = self.credentials_path()
        if not path:
            return None

        file_path = Path(path)

        if not file_path.is_file():
            return (
                f"{self.api_label} credentials file not found on the "
                f"server (path withheld)"
            )

        try:
            info = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return (
                f"{self.api_label} credentials file is not readable JSON "
                f"(path withheld)"
            )

        if not isinstance(info, dict):
            return (
                f"{self.api_label} credentials file must contain a JSON "
                f"object (path withheld)"
            )

        credential_type = info.get("type")

        if credential_type not in (SERVICE_ACCOUNT_TYPE, AUTHORIZED_USER_TYPE):
            return (
                f"{self.api_label} credentials file must be a service "
                f"account or authorized-user JSON (found "
                f"type={credential_type!r}, path withheld)"
            )

        if credential_type == SERVICE_ACCOUNT_TYPE and not (
            info.get("client_email") and info.get("private_key")
        ):
            return (
                f"{self.api_label} service-account JSON is missing "
                f"client_email/private_key (path withheld)"
            )

        if credential_type == AUTHORIZED_USER_TYPE and not (
            info.get("client_id")
            and info.get("client_secret")
            and info.get("refresh_token")
        ):
            return (
                f"{self.api_label} authorized-user JSON is missing "
                f"client_id/client_secret/refresh_token - run the OAuth "
                f"consent flow again (path withheld)"
            )

        return None

    def configuration_issues(self) -> List[str]:
        """
        Extra validity problems beyond "the env var is set": the file
        must exist, be readable JSON and hold usable credentials.
        Messages never contain the path or any secret value.
        """

        problem = self._credential_file_problem()
        return [problem] if problem else []

    # ----------------------------------------------
    # Credentials / authorization
    # ----------------------------------------------

    def _load_credentials_info(self) -> Dict[str, Any]:
        """Read + parse the credentials file, or raise honestly."""

        path = self.credentials_path()

        if not path:
            raise IntegrationNotConfiguredError(
                f"{self.api_label} is not configured: set "
                f"{self.credentials_setting} (or {SHARED_CREDENTIALS_SETTING}) "
                f"to a server-side credentials JSON file in the .env."
            )

        try:
            info = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise IntegrationNotConfiguredError(
                f"{self.api_label} credentials file could not be read or "
                f"parsed on the server (path withheld)."
            ) from None

        if not isinstance(info, dict):
            raise IntegrationNotConfiguredError(
                f"{self.api_label} credentials file must contain a JSON "
                f"object (path withheld)."
            )

        # Remember the real secret values (never logged) so _redact()
        # can remove them from any message, whatever their shape.
        for key in (
            "client_secret",
            "refresh_token",
            "access_token",
            "private_key",
            "private_key_id",
        ):
            value = info.get(key)
            if isinstance(value, str) and len(value) >= 8:
                self._secret_values.add(value)

        return info

    def _load_credentials(self):
        """
        Build (and cache) google-auth credentials for this client.

        Raises
        ------
        IntegrationNotConfiguredError
            Missing/unparseable file, unsupported credential type or a
            missing google-auth install - all without leaking the path.
        """

        if self._credentials is not None:
            return self._credentials

        info = self._load_credentials_info()
        credential_type = info.get("type")

        try:
            if credential_type == SERVICE_ACCOUNT_TYPE:
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=list(self.scopes)
                )

            elif credential_type == AUTHORIZED_USER_TYPE:
                from google.oauth2.credentials import Credentials

                credentials = Credentials.from_authorized_user_info(
                    info, scopes=list(self.scopes)
                )

            else:
                raise IntegrationNotConfiguredError(
                    f"{self.api_label} credentials file must be a service "
                    f"account or authorized-user JSON (found "
                    f"type={credential_type!r}, path withheld)."
                )

        except IntegrationNotConfiguredError:
            raise

        except ImportError:
            raise IntegrationNotConfiguredError(
                "The google-auth package is not installed on the server "
                "(run: pip install -r requirements.txt)."
            ) from None

        except Exception as exc:  # pragma: no cover - google-auth internals
            raise IntegrationNotConfiguredError(
                f"{self.api_label} credentials could not be loaded "
                f"({type(exc).__name__}, details withheld)."
            ) from None

        self._credentials = credentials
        return credentials

    def _redact(self, text: Any) -> Any:
        """
        Defence in depth: scrub tokens, secrets and the credentials-file
        path out of any string before it can reach a log, an exception
        message or an API response.
        """

        if not isinstance(text, str):
            return text

        for pattern in _REDACTION_PATTERNS:
            text = pattern.sub("[REDACTED]", text)

        # Values read from the credentials file (defence in depth).
        for secret in self._secret_values:
            text = text.replace(secret, "[REDACTED]")

        path = self.credentials_path()
        if path:
            text = text.replace(path, "[REDACTED-PATH]")

        return text

    def _access_token(self) -> str:
        """
        Return a valid OAuth access token, refreshing it when needed.

        Raises
        ------
        IntegrationConnectionError
            The token endpoint rejected the credentials or was
            unreachable (message sanitised).
        """

        credentials = self._load_credentials()

        try:
            if not credentials.valid:
                credentials.refresh(
                    _HttpxRequest(self._get_http_client())
                )

            token = credentials.token

        except IntegrationNotConfiguredError:
            raise

        except Exception as exc:
            self._connected = False
            raise IntegrationConnectionError(
                self._redact(
                    f"{self.api_label} authorization failed "
                    f"({type(exc).__name__}); check that the credentials "
                    f"are valid and not revoked."
                )
            ) from None

        if not token:
            raise IntegrationConnectionError(
                f"{self.api_label} authorization returned no access "
                f"token (details withheld)."
            )

        return token

    def _authorized_headers(
        self,
        method: str,
        url: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Build the request headers, including the bearer token."""

        headers = {"Accept": "application/json"}
        if extra:
            headers.update(extra)

        headers["Authorization"] = f"Bearer {self._access_token()}"
        return headers

    # ----------------------------------------------
    # REST helper
    # ----------------------------------------------

    def _api_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        content: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        acceptable: Tuple[int, ...] = (200,),
        timeout: Optional[httpx.Timeout] = None,
    ) -> Any:
        """
        Execute one authorized Google REST call and return its JSON body.

        Parameters
        ----------
        method : str
            HTTP verb (``GET``, ``POST``, ``PATCH``, ``DELETE`` ...).
        url : str
            Absolute API URL (never contains credentials).
        params, json_body, content, headers : optional
            Standard request options.
        acceptable : tuple[int, ...]
            Status codes that count as success (default: 200 only).
        timeout : httpx.Timeout | None
            Per-call timeout override.

        Raises
        ------
        GoogleAPIError
            Network failure, non-2xx response or a non-JSON body. The
            message is sanitised - no token, no URL query secrets.
        """

        request_headers = self._authorized_headers(method, url, headers)

        try:
            response = self._get_http_client().request(
                method,
                url,
                params=params,
                json=json_body,
                content=content,
                headers=request_headers,
                timeout=timeout or DEFAULT_HTTP_TIMEOUT,
            )

        except Exception as exc:
            logger.warning(
                "%s request to %s failed at transport level (%s).",
                self.api_label,
                url.split("?")[0],
                type(exc).__name__,
            )
            raise GoogleAPIError(
                f"Network error while calling the {self.api_label} API "
                f"({type(exc).__name__}). Check server connectivity."
            ) from None

        status_code = int(getattr(response, "status_code", 0) or 0)
        body = _safe_json(response)

        if acceptable and status_code not in acceptable:
            raise GoogleAPIError(
                self._error_message(status_code, body),
                status_code=status_code,
            )

        return body

    def _error_message(self, status_code: int, body: Any) -> str:
        """
        Turn a failed Google response into a sanitised, human-readable
        message (Google's own ``error.message`` when it is safe).
        """

        detail = None

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = error.get("message")
            elif isinstance(error, str):
                detail = error

        message = f"{self.api_label} API error (HTTP {status_code})"

        if isinstance(detail, str) and detail.strip():
            message += f": {self._redact(detail.strip()[:200])}"

        return message

    # ----------------------------------------------
    # Lifecycle (BaseIntegration contract)
    # ----------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """
        Perform ONE cheap, authorized read that proves the credentials
        work, and return secret-free facts about the account.

        Implemented by every concrete Google client.
        """

        raise NotImplementedError

    def connect(self) -> None:
        """
        Authenticate against Google for real.

        * re-reads the credentials file (so a rotated key is picked up),
        * calls ``verify_connection()`` (an actual authorized API call),
        * marks the integration connected ONLY when Google answered.

        Raises
        ------
        IntegrationNotConfiguredError - no usable credentials file.
        GoogleAPIError                - Google rejected the credentials
                                        or the API was unreachable.
        """

        if not self.is_configured():
            reason = self._credential_file_problem() or (
                f"missing setting {self.credentials_setting}"
            )
            raise IntegrationNotConfiguredError(
                f"{self.api_label} is not configured: {reason} "
                f"(path withheld)."
            )

        # Force a fresh read of the credentials file.
        self._credentials = None

        try:
            info = self.verify_connection()

        except IntegrationConnectionError as exc:
            self._connected = False
            self._last_error = str(exc)
            raise

        self._connection_info = info if isinstance(info, dict) else {}
        self._connected = True
        self._last_error = None

        # The successful verification just proved the session, so warm
        # the health cache and avoid an immediate second API call.
        self._last_health_ok = True
        self._last_health_at = time.monotonic()

        logger.info(
            "%s connected (%s).",
            self.name,
            ", ".join(
                f"{key}={value}"
                for key, value in self._connection_info.items()
                if value is not None
            ) or "verified",
        )

    def disconnect(self) -> None:
        """
        Drop the authorized session and forget the cached credentials.

        Idempotent: safe to call when nothing is connected. The next
        ``connect()`` re-reads the credentials file from disk.
        """

        self._connected = False
        self._credentials = None
        self._connection_info = {}
        self._last_health_ok = None
        self._last_health_at = None

        logger.info("%s session state cleared (disconnected).", self.name)

    def check_health(self) -> bool:
        """
        Verify the live session with a TTL-cached authorized API call.

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
            info = self.verify_connection()

        except (IntegrationConnectionError, IntegrationNotConfiguredError) as exc:
            self._connected = False
            self._last_error = str(exc)
            self._last_health_ok = False
            self._last_health_at = now
            logger.warning("%s health check failed: %s", self.name, exc)
            return False

        if isinstance(info, dict) and info:
            self._connection_info = info
        self._last_error = None
        self._last_health_ok = True
        self._last_health_at = now
        return True

    # ----------------------------------------------
    # Status enrichment
    # ----------------------------------------------

    def get_connection_info(self) -> dict:
        """Secret-free facts about the live Google session."""

        if self._connected and self._connection_info:
            return {
                key: self._redact(value)
                for key, value in self._connection_info.items()
                if value is not None
            }
        return {}


def markdown_multipart_body(
    metadata: Dict[str, Any],
    filename: str,
    content: bytes,
    content_type: str = "text/markdown",
) -> Tuple[bytes, str]:
    """
    Build a ``multipart/related`` body for Google's upload endpoints.

    Google Drive accepts a JSON metadata part followed by the media
    part for ``uploadType=multipart`` uploads. httpx can do this with
    ``files=``, but building the body here keeps every Google call on
    the single ``content=`` transport path - which is what makes the
    whole integration stubbable in tests.

    Returns
    -------
    (body, content_type)
        The encoded body and the matching ``Content-Type`` header
        (including the generated boundary).
    """

    boundary = "scamnet-boundary-7MA4YWxkTrZu0gW"

    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    return body, f"multipart/related; boundary={boundary}"
