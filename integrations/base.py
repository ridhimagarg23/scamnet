"""
base.py
=======
Integration abstractions for SCAMNET's external applications.

SCAMNET is an autonomous scam-investigation agent that will (in later
increments) talk to a suspicious actor over **Telegram**, keep live
investigation evidence in **Google Sheets** and archive evidence-backed
reports in **Google Drive**.

This module defines the provider-agnostic contract those external apps
must fulfil, so the core investigation code never depends on a specific
vendor SDK:

    BaseIntegration                    (abstract client contract)
    IntegrationState                   (lifecycle enum)
    IntegrationStatus                  (serialisable status payload)
    IntegrationNotConfiguredError      (missing server-side credentials)
    IntegrationNotImplementedError     (auth flow not built yet)
    IntegrationConnectionError         (real upstream/API failure)

Honest-status rule
------------------
An integration may only report ``connected=True`` after ``connect()``
has completed a REAL authentication handshake with the external
service AND ``check_health()`` verified the live session. Providers
whose real auth flow is not built yet must raise
``IntegrationNotImplementedError`` from ``connect()`` (surfaced by the
API as HTTP 501); providers whose real attempt failed must raise
``IntegrationConnectionError`` (HTTP 502). No client may fake success.

Security rule
-------------
Credentials live ONLY in server-side configuration (``config.settings``
populated from the git-ignored ``.env``). ``IntegrationStatus`` payloads
never contain secret *values* - only setting NAMES and human-readable
state descriptions. Filesystem paths of credential files are also
withheld from API responses.

Adding a new integration
------------------------
1. Create ``integrations/<provider>/client.py`` with a concrete
   subclass of ``BaseIntegration``.
2. Declare ``id`` / ``name`` / ``purpose`` / ``required_settings`` /
   ``setup_instructions``.
3. Implement ``connect`` / ``disconnect`` / ``check_health`` against
   the real service API (set ``self._connected = True`` only after a
   verified handshake).
4. Flip ``connect_implemented = True`` once the real flow works.
5. Register the instance in ``integrations/__init__.py``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger("SCAMNET-Integrations")


# --------------------------------------------------
# Lifecycle states
# --------------------------------------------------

class IntegrationState(str, Enum):
    """
    Lifecycle states an integration can be in.

    NOT_CONFIGURED - required server-side settings are absent/invalid.
    DISCONNECTED   - configured, but no live authenticated session.
    CONNECTED      - real authentication succeeded and health verified.
    ERROR          - a connection attempt failed (see status detail).
    """

    NOT_CONFIGURED = "not_configured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"


# --------------------------------------------------
# Honest failure types
# --------------------------------------------------

class IntegrationNotConfiguredError(RuntimeError):
    """
    Raised when an operation needs credentials that are not present in
    the server-side configuration. The message names the missing
    settings (names only - never values).
    """


class IntegrationNotImplementedError(RuntimeError):
    """
    Raised when the real authentication / API flow for a provider has
    not been implemented yet. The API layer maps this to HTTP 501 so
    clients can never mistake "not built" for "connected".
    """


class IntegrationConnectionError(RuntimeError):
    """
    Raised when a REAL connection or API attempt against the external
    service failed (network problem, credentials rejected, upstream
    API error). The API layer maps this to HTTP 502.

    Messages MUST be sanitised by the provider: they may describe the
    failure but never contain secret values (tokens, keys) or the raw
    request URL when it embeds credentials.
    """



# --------------------------------------------------
# Status payload (what GET /api/integrations returns)
# --------------------------------------------------

class IntegrationStatus(BaseModel):
    """
    Secret-free, serialisable snapshot of one integration.

    Attributes
    ----------
    id : str
        Stable snake_case identifier (e.g. ``telegram``).
    name : str
        Human-facing app name (e.g. ``Telegram``).
    purpose : str
        One-line role of the app inside SCAMNET.
    configured : bool
        All required server-side settings are present and valid.
    available : bool
        The app is actually usable: configured AND the real connection
        flow is implemented. False until both are true - we never claim
        availability we cannot honour.
    connected : bool
        A real authenticated session is live (verified via health
        check). Always False until a genuine connect() succeeded.
    state : IntegrationState
        Machine-readable lifecycle state.
    detail : str
        Human-readable explanation of the state (no secrets, no paths).
    setup_instructions : str
        What the operator must do to enable this integration.
    connection_info : dict
        Optional provider-defined, secret-free facts about a live
        connection (e.g. the Telegram bot's public username). Empty
        unless genuinely connected.
    """

    id: str
    name: str
    purpose: str
    configured: bool
    available: bool
    connected: bool
    state: IntegrationState
    detail: str = ""
    setup_instructions: str = ""
    connection_info: dict = Field(default_factory=dict)


# --------------------------------------------------
# Abstract client contract
# --------------------------------------------------

class BaseIntegration(ABC):
    """
    Contract every external-app client must fulfil.

    Subclasses declare which ``config.Settings`` attributes hold their
    credentials (``required_settings``) and implement the lifecycle
    methods against the real service API. The base class owns the
    honest-status bookkeeping so no provider can accidentally (or
    deliberately) report a fake connection.

    Attributes
    ----------
    id : str
        Unique snake_case id used in API routes and frontend cards.
    name : str
        Human-facing app name.
    purpose : str
        One-line role inside SCAMNET.
    required_settings : tuple[str, ...]
        Names of ``settings`` attributes that must be non-empty.
    optional_settings : tuple[str, ...]
        Names of ``settings`` attributes that tune behaviour but are
        not required for the integration to be "configured".
    connect_implemented : bool
        Class-level flag: True only once the real authentication flow
        exists in the subclass. Drives ``available`` in the status.
    setup_instructions : str
        Operator-facing guidance shown by the API/dashboard.
    """

    id: str = ""
    name: str = ""
    purpose: str = ""
    required_settings: Tuple[str, ...] = ()
    optional_settings: Tuple[str, ...] = ()
    connect_implemented: bool = False
    setup_instructions: str = ""

    def __init__(self, settings):
        """
        Parameters
        ----------
        settings : config.Settings
            The shared server-side settings singleton (injected so
            tests can pass a stub without touching the environment).
        """

        self.settings = settings

        # Internal, server-side connection bookkeeping. ``_connected``
        # is flipped ONLY by a real, verified connect() implementation.
        self._connected: bool = False
        self._last_error: Optional[str] = None

    # ----------------------------------------------
    # Configuration checks
    # ----------------------------------------------

    def missing_settings(self) -> List[str]:
        """Names of required settings that are absent or empty."""

        return [
            key
            for key in self.required_settings
            if not getattr(self.settings, key, None)
        ]

    def configuration_issues(self) -> List[str]:
        """
        Extra validity problems beyond "env var present" (e.g. a
        credentials file that does not exist on the server). Messages
        must be secret-free and must NOT contain filesystem paths.

        Providers override this hook; the default has no extra checks.
        """

        return []

    def is_configured(self) -> bool:
        """True when every required setting is present and valid."""

        return not self.missing_settings() and not self.configuration_issues()

    # ----------------------------------------------
    # Lifecycle (providers implement real behaviour)
    # ----------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """
        Establish a REAL authenticated connection to the external app.

        Implementations must:
        * raise ``IntegrationNotConfiguredError`` when credentials are
          missing from the server-side configuration;
        * raise ``IntegrationNotImplementedError`` while the real auth
          flow does not exist yet (never return fake success);
        * set ``self._connected = True`` only after the remote service
          confirmed the credentials.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """
        Tear down a live session. Idempotent: a no-op when nothing is
        connected. Implementations must clear ``self._connected``.
        """

    @abstractmethod
    def check_health(self) -> bool:
        """
        Verify the live session against the remote service (a cheap,
        read-only call). Must return False when there is no session or
        the check cannot actually be performed yet.
        """

    def is_connected(self) -> bool:
        """
        True ONLY when a real authenticated session exists AND the
        health check still passes. Any health-check failure is recorded
        (server-side log) and reported as not connected.
        """

        if not self._connected:
            return False

        try:
            return bool(self.check_health())
        except Exception as exc:  # pragma: no cover - defensive
            # Log the full error server-side; keep only a generic note
            # in the API-visible state (never risk leaking secrets).
            logger.error(
                "Health check failed for integration '%s': %s",
                self.id, exc
            )
            self._last_error = "Health check failed (see server logs)."
            return False

    # ----------------------------------------------
    # Status reporting
    # ----------------------------------------------

    def get_connection_info(self) -> dict:
        """
        Optional, secret-free facts about the live connection (e.g. a
        bot's public username). Included in ``IntegrationStatus.
        connection_info``. Providers override; default is empty.
        """

        return {}

    def get_status(self) -> IntegrationStatus:
        """
        Build the honest, secret-free status payload consumed by
        ``GET /api/integrations`` and the dashboard's Connected Apps UI.
        """

        configured = self.is_configured()
        connected = self.is_connected()

        if connected:
            state = IntegrationState.CONNECTED
            detail = "Connected - authenticated session is live."

        elif not configured:
            state = IntegrationState.NOT_CONFIGURED
            problems: List[str] = []
            missing = self.missing_settings()
            if missing:
                problems.append(
                    "Missing server-side setting(s): " + ", ".join(missing)
                )
            problems.extend(self.configuration_issues())
            detail = ". ".join(problems) + "."

        elif not self.connect_implemented:
            state = IntegrationState.DISCONNECTED
            detail = (
                "Credentials are configured on the server, but the real "
                "authentication flow is not implemented yet."
            )

        elif self._last_error:
            state = IntegrationState.ERROR
            detail = self._last_error

        else:
            state = IntegrationState.DISCONNECTED
            detail = "Configured, but no active connection."

        return IntegrationStatus(
            id=self.id,
            name=self.name,
            purpose=self.purpose,
            configured=configured,
            # "available" means genuinely usable: credentials present
            # AND a real connect flow exists. Never true by accident.
            available=configured and self.connect_implemented,
            connected=connected,
            state=state,
            detail=detail,
            setup_instructions=self.setup_instructions,
            connection_info=self.get_connection_info() if connected else {},
        )
