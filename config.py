"""
config.py
==========
Central configuration for TraceAI.

This module loads runtime settings from environment variables
(via a local ``.env`` file if one exists) and exposes them through
a single ``settings`` object that every other module imports.

Recommended environment variables
---------------------------------
* ``OPENROUTER_API_KEY``  - Your OpenRouter API key (https://openrouter.ai/keys).
* ``NVIDIA_NIM_API_KEY``  - Your NVIDIA NIM API key (https://build.nvidia.com).
                            At least ONE of the two LLM keys must be set -
                            without any key the server still BOOTS (the
                            dashboard, ``GET /health`` and the integration
                            status/connect endpoints keep working), but
                            ``POST /analyze`` answers HTTP 503
                            "llm_not_configured" and ``GET /health``
                            reports "degraded".

Optional environment variables
------------------------------
* ``LLM_PROVIDER``        - Active LLM provider: ``openrouter`` (default)
                            or ``nvidia`` (NVIDIA NIM - the fastest option).
                            When unset, the server auto-selects: OpenRouter
                            if its key exists, else NVIDIA NIM. The request
                            flow is fixed (OpenRouter first, NVIDIA as the
                            fallback stage), so this only matters when
                            OpenRouter is not configured at all.
* ``LLM_MODEL``           - OpenRouter model id used when the active
                            provider is OpenRouter. Defaults to
                            ``qwen/qwen3-32b``.
* ``NVIDIA_NIM_MODEL``    - NVIDIA NIM model id used when the active
                            provider is NVIDIA. Defaults to
                            ``nvidia/nemotron-3-ultra-550b-a55b``.
* ``OPENROUTER_DEADLINE`` - Seconds the OpenRouter call may take before it
                            is abandoned and the NVIDIA stage starts
                            (default ``15``). ``LLM_PRIMARY_DEADLINE`` is a
                            synonym; ``0`` disables the deadline.
* ``OPENROUTER_BASE_URL`` - OpenAI-compatible gateway root for OpenRouter.
                            Defaults to ``https://openrouter.ai/api/v1``.
* ``NVIDIA_NIM_BASE_URL`` - NVIDIA NIM gateway root. Defaults to
                            ``https://integrate.api.nvidia.com/v1``.
* ``OPENROUTER_FALLBACK_MODELS`` / ``NVIDIA_NIM_FALLBACK_MODELS``
                          - Comma-separated model ids tried IN ORDER when
                            the primary model fails (rate limit, timeout,
                            bad output, unknown model...). ``LLM_FALLBACK_MODELS``
                            is a shared shorthand applied to both providers
                            when the provider-specific variable is empty.
                            The NVIDIA stage always contains the two
                            nemotron models (ultra then lightning) whatever
                            these variables hold.
* ``LLM_CROSS_PROVIDER_FALLBACK``
                          - ``1`` (default) lets a failing provider fall
                            back to the OTHER provider's models (when its
                            key is configured); ``0`` disables that hop.
* ``OPENROUTER_TIMEOUT`` / ``NVIDIA_NIM_TIMEOUT`` / ``LLM_TIMEOUT``
                          - Per-request timeout in seconds (provider-specific
                            wins, else the shared ``LLM_TIMEOUT``). Defaults:
                            90 s for OpenRouter, 45 s for NVIDIA NIM so a
                            slow call fails over to the next model quickly.
* ``OPENROUTER_MODELS`` / ``NVIDIA_NIM_MODELS``
                          - Comma-separated EXTRA model ids appended to the
                            model catalog (for brand-new models that are
                            not in the curated catalog yet).
* ``CORS_ALLOW_ORIGINS``  - Comma-separated browser origins allowed to
                            call the API cross-origin. Defaults to the
                            local dev origins + the hosted dashboard.
                            (The bundled Next.js dashboard proxies
                            same-origin through ``/backend-api``, so it
                            needs no CORS entry at all.)
* ``GOOGLE_CREDENTIALS_FILE``
                          - Shared Google credentials JSON used by
                            Drive / Sheets / Gmail when their own
                            provider-specific setting is empty.

Optional SCAMNET integration variables (all default to unset; see
``integrations/`` - missing values simply keep the corresponding
external app in the honest "not_configured" state):

* ``TELEGRAM_BOT_TOKEN``            - Telegram Bot API token (@BotFather).
* ``TELEGRAM_API_BASE``             - Bot API root (default
                                      ``https://api.telegram.org``).
* ``GOOGLE_CREDENTIALS_FILE``       - Shared Google credentials JSON
                                      (service account or authorized
                                      user) used by every Google app.
* ``GOOGLE_SHEETS_CREDENTIALS_FILE``- Server-side path to the Sheets
                                      service-account / OAuth JSON key.
* ``GOOGLE_SHEETS_SPREADSHEET_ID``  - Optional target spreadsheet id.
* ``GOOGLE_SHEETS_WORKSHEET``       - Worksheet (tab) for evidence rows.
* ``GOOGLE_DRIVE_CREDENTIALS_FILE`` - Server-side path to the Drive
                                      service-account / OAuth JSON key.
* ``GOOGLE_DRIVE_FOLDER_ID``        - Optional report destination folder.
* ``GOOGLE_GMAIL_CREDENTIALS_FILE`` - Server-side path to the Gmail
                                      authorized-user JSON key.

Example
-------
.. code-block:: bash

    export OPENROUTER_API_KEY=sk-or-...
    export NVIDIA_NIM_API_KEY=nvapi-...
    export LLM_PROVIDER=nvidia
    export NVIDIA_NIM_MODEL=meta/llama-3.1-8b-instruct

Usage
-----
>>> from config import settings
>>> settings.ACTIVE_PROVIDER
'openrouter'
"""

import logging
import os

from dotenv import load_dotenv

# Load the .env file at the repository root (if present).
# Real credentials are never committed to git; see .env.example.
load_dotenv()


# ----------------------------------------------------------------------
# LLM provider identifiers
# ----------------------------------------------------------------------
# Canonical provider ids used everywhere (settings, LLMClient, API,
# dashboard). Aliases such as "nim" / "nvidia-nim" are normalized to
# "nvidia" so callers never have to guess the exact spelling.

PROVIDER_OPENROUTER = "openrouter"
PROVIDER_NVIDIA = "nvidia"

PROVIDER_ALIASES = {
    "openrouter": PROVIDER_OPENROUTER,
    "open_router": PROVIDER_OPENROUTER,
    "nvidia": PROVIDER_NVIDIA,
    "nim": PROVIDER_NVIDIA,
    "nvidia-nim": PROVIDER_NVIDIA,
    "nvidia_nim": PROVIDER_NVIDIA,
}

#: Default gateway roots (overridable per provider via env).
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

#: Default primary model for OpenRouter (the provider every turn starts
#: on - see ``PRIMARY_PROVIDER`` below).
DEFAULT_OPENROUTER_MODEL = "qwen/qwen3-32b"

# ----------------------------------------------------------------------
# NVIDIA NIM fallback stage
# ----------------------------------------------------------------------
# The NVIDIA stage is deliberately tiny: exactly TWO models, tried in
# this order. Nemotron 3 Ultra is the priority; Nemotron 3.5 Lightning
# only answers when Ultra fails / is too slow.
NVIDIA_PRIMARY_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
NVIDIA_SECONDARY_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

#: Ordered NVIDIA fallback stage (ultra first, lightning only as its
#: fallback). Kept as a tuple so nothing can mutate it by accident.
NVIDIA_STAGE_MODELS = (NVIDIA_PRIMARY_MODEL, NVIDIA_SECONDARY_MODEL)

DEFAULT_NVIDIA_MODEL = NVIDIA_PRIMARY_MODEL

#: Built-in spares for OpenRouter: NONE. The request flow is
#: "OpenRouter -> (no answer inside OPENROUTER_DEADLINE) -> NVIDIA
#: stage". Add ``OPENROUTER_FALLBACK_MODELS`` only if you really want
#: extra OpenRouter spares before the NVIDIA hop.
DEFAULT_OPENROUTER_FALLBACKS = ()

#: Provider every turn STARTS on. A pasted scammer message always goes
#: to OpenRouter first; NVIDIA NIM is the fallback stage (never the
#: first stop) unless OpenRouter is not configured at all.
PRIMARY_PROVIDER = PROVIDER_OPENROUTER

#: Soft deadline (seconds) for the PRIMARY provider. When OpenRouter has
#: not answered within this window the call is abandoned and the NVIDIA
#: stage starts immediately - the analyst never waits on a slow gateway.
#: ``0`` disables the deadline (wait for the provider timeout instead).
DEFAULT_PRIMARY_DEADLINE = 15.0

#: Default per-request timeouts (seconds).
DEFAULT_OPENROUTER_TIMEOUT = 90.0
#: The NVIDIA stage runs very large models (550B), so it needs a little
#: more room than a small instruct model before it is declared too slow.
DEFAULT_NVIDIA_TIMEOUT = 45.0


# Truthy spellings accepted for boolean environment flags.
_TRUE_VALUES = ("1", "true", "yes", "on")


def _env_flag(name: str, default: bool = False) -> bool:
    """
    Read a boolean environment flag.

    ``TELEGRAM_AUTO_START_WORKER=0`` / ``false`` / ``no`` / ``off``
    (any case, surrounding spaces ignored) disable the flag; an unset
    variable keeps ``default``.
    """

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    return raw.strip().lower() in _TRUE_VALUES


def _env_list(*names: str) -> list:
    """
    Read the first non-empty comma-separated list variable.

    ``_env_list("OPENROUTER_FALLBACK_MODELS", "LLM_FALLBACK_MODELS")``
    lets a provider-specific variable win over the shared shorthand.
    Returns ``[]`` when none of the variables is set.
    """

    for name in names:
        raw = os.getenv(name)

        if raw and raw.strip():
            return [
                item.strip()
                for item in raw.split(",")
                if item.strip()
            ]

    return []


def _dedupe(models) -> list:
    """
    Drop empty + repeated model ids while preserving order.

    Used to build the fallback chains: a model id that already appears
    earlier (or is the primary model) is never tried twice.
    """

    seen: set = set()
    ordered: list = []

    for model in models:
        model_id = str(model or "").strip()

        if not model_id or model_id in seen:
            continue

        seen.add(model_id)
        ordered.append(model_id)

    return ordered


def _env_float(name: str, default: float) -> float:
    """Read a float variable, keeping ``default`` on garbage input."""

    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        value = float(raw.strip())
    except ValueError:
        return default

    return value if value > 0 else default


def normalize_provider(value: str | None) -> str | None:
    """
    Normalize a provider id (or alias) to its canonical form.

    Returns ``None`` for empty input and the lower-cased raw value for
    unknown ids (so callers can reject them with a clear message).
    """

    if value is None:
        return None

    text = str(value).strip().lower()

    if not text:
        return None

    return PROVIDER_ALIASES.get(text, text)


class Settings:
    """
    Container of all runtime configuration values.

    Attributes
    ----------
    OPENROUTER_API_KEY / NVIDIA_NIM_API_KEY : str | None
        Keys used to authenticate LLM calls through each provider.
    ACTIVE_PROVIDER : str
        Currently selected provider (``"openrouter"`` or ``"nvidia"``).
        Initialized from ``LLM_PROVIDER`` (or auto-selected from the
        configured keys) and switchable at runtime via
        ``POST /api/llm/select`` or per request via the ``provider``
        field of ``POST /analyze``.
    ACTIVE_MODEL : str
        Currently selected model id for the active provider.
    TELEGRAM_* / GOOGLE_SHEETS_* / GOOGLE_DRIVE_* : str | None
        Optional, server-side credentials for the SCAMNET external-app
        integration layer (``integrations/``). Never exposed to the
        frontend - only their presence/absence is reported by
        ``GET /api/integrations``.
    """

    #: Browser origins allowed to call the API cross-origin. The Next.js
    #: dashboard is same-origin (it proxies ``/backend-api``), so these
    #: only matter for direct/external API consumers.
    DEFAULT_CORS_ORIGINS = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://trace-ai-phi.vercel.app",
    )

    def __init__(self):

        # ------------------------------------------------------
        # LLM credentials (at least ONE provider key is required
        # for the AI agents; both may be set for fallback)
        # ------------------------------------------------------

        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or None
        self.NVIDIA_NIM_API_KEY = os.getenv("NVIDIA_NIM_API_KEY") or None

        # ------------------------------------------------------
        # Gateway roots (OpenAI-compatible on both providers)
        # ------------------------------------------------------

        self.OPENROUTER_BASE_URL = os.getenv(
            "OPENROUTER_BASE_URL",
            DEFAULT_OPENROUTER_BASE_URL,
        ).rstrip("/")

        self.NVIDIA_NIM_BASE_URL = os.getenv(
            "NVIDIA_NIM_BASE_URL",
            DEFAULT_NVIDIA_BASE_URL,
        ).rstrip("/")

        # ------------------------------------------------------
        # Default primary models per provider
        # ------------------------------------------------------
        # OpenRouter slugs look like "provider/model-name" while NIM
        # ids look like "org/model-name" - both are plain strings for
        # the OpenAI-compatible chat API.

        self.LLM_MODEL = os.getenv(
            "LLM_MODEL",
            DEFAULT_OPENROUTER_MODEL,
        ).strip() or DEFAULT_OPENROUTER_MODEL

        self.NVIDIA_NIM_MODEL = os.getenv(
            "NVIDIA_NIM_MODEL",
            DEFAULT_NVIDIA_MODEL,
        ).strip() or DEFAULT_NVIDIA_MODEL

        # ------------------------------------------------------
        # Fallback chains
        # ------------------------------------------------------
        # OpenRouter: none by default (it hands over to NVIDIA after
        # its deadline). NVIDIA: always ultra -> lightning.

        self.OPENROUTER_FALLBACK_MODELS = _env_list(
            "OPENROUTER_FALLBACK_MODELS", "LLM_FALLBACK_MODELS"
        ) or list(DEFAULT_OPENROUTER_FALLBACKS)

        # NVIDIA stage = ultra -> lightning, always, in that order.
        # Extra ids from the env are appended AFTER the two nemotron
        # models so a custom NIM deployment can still add local spares.
        self.NVIDIA_FALLBACK_MODELS = _dedupe(
            [
                *NVIDIA_STAGE_MODELS,
                *_env_list(
                    "NVIDIA_NIM_FALLBACK_MODELS",
                    "NVIDIA_FALLBACK_MODELS",
                    "LLM_FALLBACK_MODELS",
                ),
            ]
        )

        configured_nvidia_model = os.getenv("NVIDIA_NIM_MODEL", "").strip()

        if (
            configured_nvidia_model
            and configured_nvidia_model not in NVIDIA_STAGE_MODELS
        ):
            logging.getLogger("TraceAI-Config").warning(
                "NVIDIA_NIM_MODEL='%s' is set, but the NVIDIA fallback "
                "stage always runs %s (in that order). Leave "
                "NVIDIA_NIM_MODEL empty to use Nemotron 3 Ultra.",
                configured_nvidia_model,
                " -> ".join(NVIDIA_STAGE_MODELS),
            )

        # When True (default), a provider whose whole chain failed falls
        # back to the OTHER provider's chain (if its key is configured).
        self.LLM_CROSS_PROVIDER_FALLBACK = _env_flag(
            "LLM_CROSS_PROVIDER_FALLBACK", True
        )

        # ------------------------------------------------------
        # Per-request timeouts (fast fail-over keeps replies quick)
        # ------------------------------------------------------

        generic_timeout = os.getenv("LLM_TIMEOUT", "").strip()

        try:
            generic_value = float(generic_timeout) if generic_timeout else None
        except ValueError:
            generic_value = None

        if generic_value is None or generic_value <= 0:
            generic_value = None

        self.OPENROUTER_TIMEOUT = _env_float(
            "OPENROUTER_TIMEOUT",
            generic_value or DEFAULT_OPENROUTER_TIMEOUT,
        )
        self.NVIDIA_NIM_TIMEOUT = _env_float(
            "NVIDIA_NIM_TIMEOUT",
            generic_value or DEFAULT_NVIDIA_TIMEOUT,
        )

        # ------------------------------------------------------
        # Soft deadlines (how long the PRIMARY provider may take
        # before the NVIDIA stage takes over)
        # ------------------------------------------------------
        # The OpenRouter call is abandoned after this many seconds -
        # the analyst gets an answer from NVIDIA instead of staring
        # at a spinner. ``0`` disables the deadline for a provider.

        self.LLM_PRIMARY_DEADLINE = _env_float(
            "LLM_PRIMARY_DEADLINE", DEFAULT_PRIMARY_DEADLINE
        )

        self.OPENROUTER_DEADLINE = _env_float(
            "OPENROUTER_DEADLINE", self.LLM_PRIMARY_DEADLINE
        )
        # The NVIDIA stage is the last resort, so it is not cut short
        # by default (set NVIDIA_NIM_DEADLINE to bound it too).
        self.NVIDIA_NIM_DEADLINE = _env_float("NVIDIA_NIM_DEADLINE", 0.0)

        # ------------------------------------------------------
        # Active provider + model (runtime-switchable selection)
        # ------------------------------------------------------

        requested_provider = normalize_provider(os.getenv("LLM_PROVIDER", ""))

        if requested_provider in (PROVIDER_OPENROUTER, PROVIDER_NVIDIA):
            active_provider = requested_provider
        elif self.OPENROUTER_API_KEY:
            # Backwards compatible default: historical single-provider
            # setups keep behaving exactly as before.
            active_provider = PROVIDER_OPENROUTER
        elif self.NVIDIA_NIM_API_KEY:
            active_provider = PROVIDER_NVIDIA
        else:
            # No key at all: default to OpenRouter so the dashboard and
            # the 503 guidance name the historical provider first.
            active_provider = PROVIDER_OPENROUTER

        #: Provider every turn starts on (OpenRouter). Only used to keep
        #: the "OpenRouter first, NVIDIA as fallback" order explicit.
        self.PRIMARY_PROVIDER = PRIMARY_PROVIDER

        self.ACTIVE_PROVIDER = active_provider
        self.ACTIVE_MODEL = self.get_default_model(active_provider)

        # ------------------------------------------------------
        # SCAMNET external-app integrations (all OPTIONAL)
        # ------------------------------------------------------
        # Read by the integration layer (integrations/) and surfaced
        # as honest status - never as values - by GET /api/integrations.
        # A missing variable simply keeps that integration in the
        # "not_configured" state; nothing fails at startup because of
        # them. Real authentication flows are not implemented yet:
        # see the "To implement the real flow later" docstring section
        # in each integrations/<provider>/client.py.
        #
        # SECURITY: these credentials must only ever live in the
        # server-side .env (git-ignored). They must never be sent to
        # the frontend or committed to the repository.

        # --- Telegram (Bot API) ---
        # Bot token issued by @BotFather.
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        # Bot API root; override only for a self-hosted API server.
        self.TELEGRAM_API_BASE = os.getenv(
            "TELEGRAM_API_BASE",
            "https://api.telegram.org"
        )

        # Start the Telegram reply loop automatically once the bot is
        # connected (and at boot when the token already works). Set to
        # 0 to require an explicit POST /api/telegram/conversation/start
        # - useful when another service owns the getUpdates cursor.
        self.TELEGRAM_AUTO_START_WORKER = _env_flag(
            "TELEGRAM_AUTO_START_WORKER", True
        )

        # --- Shared Google credentials (optional convenience) ---
        # One credentials JSON (service account or OAuth authorized
        # user) that Drive / Sheets / Gmail fall back to when their own
        # provider-specific variable is empty.
        self.GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")

        # --- Google Sheets (live investigation evidence) ---
        # Server-side path to the service-account / OAuth JSON key.
        self.GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_SHEETS_CREDENTIALS_FILE"
        )
        # Optional target spreadsheet (created on first connect if unset).
        self.GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv(
            "GOOGLE_SHEETS_SPREADSHEET_ID"
        )
        # Optional worksheet (tab) that evidence rows are written to.
        self.GOOGLE_SHEETS_WORKSHEET = os.getenv("GOOGLE_SHEETS_WORKSHEET")

        # --- Google Drive (investigation reports) ---
        # Server-side path to the service-account / OAuth JSON key.
        self.GOOGLE_DRIVE_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_DRIVE_CREDENTIALS_FILE"
        )
        # Optional destination folder for generated report files.
        self.GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        # --- Gmail (evidence inbox + report delivery) ---
        # Gmail needs an OAuth *user* credentials JSON (refresh token);
        # service accounts require Workspace domain-wide delegation.
        self.GOOGLE_GMAIL_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_GMAIL_CREDENTIALS_FILE"
        )

        # ------------------------------------------------------
        # CORS (browser origins allowed to call the API directly)
        # ------------------------------------------------------
        # The bundled dashboard talks to the backend SAME-ORIGIN
        # through the Next.js /backend-api proxy, so no CORS entry is
        # needed for it. Direct API consumers (custom dashboards, the
        # e2e tests) can be allow-listed here without editing code.

        configured_origins = os.getenv("CORS_ALLOW_ORIGINS", "")

        self.CORS_ALLOW_ORIGINS = [
            origin.strip()
            for origin in configured_origins.split(",")
            if origin.strip()
        ] or list(self.DEFAULT_CORS_ORIGINS)

        # ------------------------------------------------------
        # Validation
        # ------------------------------------------------------
        # Missing LLM keys mean the AI agents cannot run, but that must
        # NOT take the whole server down: an operator verifying an
        # external-app setup (Telegram/Sheets/Drive/Gmail) needs /health
        # and /api/integrations to answer even before any LLM key
        # exists. LLM-backed endpoints refuse honestly (503) instead;
        # see LLMClient and backend/api.py.
        #
        # TRACEAI_STRICT_CONFIG=1 restores the old fail-fast behaviour
        # for deployments that prefer to crash on misconfiguration.
        #
        # NOTE: for offline unit tests simply export a dummy value:
        #   export OPENROUTER_API_KEY=test-key

        self.STRICT_CONFIG = os.getenv(
            "TRACEAI_STRICT_CONFIG", ""
        ).strip().lower() in ("1", "true", "yes", "on")

        if not self.llm_configured:

            message = (
                "Neither OPENROUTER_API_KEY nor NVIDIA_NIM_API_KEY was found "
                "in .env - LLM features (/analyze, Telegram conversation, "
                "reports) are disabled until at least one of them is set."
            )

            if self.STRICT_CONFIG:
                raise ValueError(message)

            logging.getLogger("TraceAI-Config").warning(message)

    # ----------------------------------------------------------
    # LLM provider helpers
    # ----------------------------------------------------------

    @staticmethod
    def is_known_provider(provider: str | None) -> bool:
        """True for ``openrouter`` / ``nvidia`` (aliases accepted)."""

        return normalize_provider(provider) in (
            PROVIDER_OPENROUTER,
            PROVIDER_NVIDIA,
        )

    def is_provider_configured(self, provider: str | None) -> bool:
        """True when the provider's API key is present."""

        normalized = normalize_provider(provider)

        if normalized == PROVIDER_NVIDIA:
            return bool(self.NVIDIA_NIM_API_KEY)

        if normalized == PROVIDER_OPENROUTER:
            return bool(self.OPENROUTER_API_KEY)

        return False

    @property
    def openrouter_configured(self) -> bool:
        """True when the OpenRouter key is present."""

        return bool(self.OPENROUTER_API_KEY)

    @property
    def nvidia_configured(self) -> bool:
        """True when the NVIDIA NIM key is present."""

        return bool(self.NVIDIA_NIM_API_KEY)

    @property
    def llm_configured(self) -> bool:
        """
        True when at least one LLM provider key is present.

        Surfaced by ``GET /health`` and checked by the agents so a
        missing key becomes one clear 503 instead of a confusing
        provider-side error mid-investigation.
        """

        return bool(self.OPENROUTER_API_KEY or self.NVIDIA_NIM_API_KEY)

    def get_api_key(self, provider: str | None) -> str | None:
        """Return the API key for a provider (None when unset)."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return self.NVIDIA_NIM_API_KEY

        return self.OPENROUTER_API_KEY

    def get_base_url(self, provider: str | None) -> str:
        """Return the OpenAI-compatible gateway root for a provider."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return self.NVIDIA_NIM_BASE_URL

        return self.OPENROUTER_BASE_URL

    def get_default_model(self, provider: str | None) -> str:
        """Return the default primary model id for a provider."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return self.NVIDIA_NIM_MODEL

        return self.LLM_MODEL

    def get_fallback_models(self, provider: str | None) -> list:
        """Return the automatic spare models for a provider (ordered)."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return list(self.NVIDIA_FALLBACK_MODELS)

        return list(self.OPENROUTER_FALLBACK_MODELS)

    def get_timeout(self, provider: str | None) -> float:
        """Return the per-request timeout (seconds) for a provider."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return self.NVIDIA_NIM_TIMEOUT

        return self.OPENROUTER_TIMEOUT

    def get_deadline(self, provider: str | None) -> float:
        """
        Return the soft deadline (seconds) for a provider's stage.

        ``LLMClient.generate`` abandons the provider once this many
        seconds have elapsed since its FIRST attempt in the current
        call and moves on to the next model in the chain. ``0`` means
        "no deadline" (the per-request timeout is the only limit).
        """

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return self.NVIDIA_NIM_DEADLINE

        return self.OPENROUTER_DEADLINE

    def provider_key_name(self, provider: str | None) -> str:
        """Return the env var name holding the provider's key."""

        if normalize_provider(provider) == PROVIDER_NVIDIA:
            return "NVIDIA_NIM_API_KEY"

        return "OPENROUTER_API_KEY"

    def resolve_provider_model(
        self,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple:
        """
        Resolve an effective ``(provider, model)`` pair.

        * An explicit provider wins, otherwise the active provider.
        * An explicit model wins, otherwise - when the provider was
          switched away from the active one - that provider's default
          model, else the active model.
        """

        if provider:
            normalized = normalize_provider(provider)
            active_provider = (
                normalized
                if normalized
                in (PROVIDER_OPENROUTER, PROVIDER_NVIDIA)
                else self.ACTIVE_PROVIDER
            )
            provider_switched = active_provider != self.ACTIVE_PROVIDER
        else:
            active_provider = self.ACTIVE_PROVIDER
            provider_switched = False

        requested_model = (model or "").strip()

        if requested_model:
            active_model = requested_model
        elif provider_switched:
            active_model = self.get_default_model(active_provider)
        else:
            active_model = self.ACTIVE_MODEL

        return active_provider, active_model

    def set_active(
        self,
        provider: str,
        model: str | None = None,
    ) -> tuple:
        """
        Switch the runtime-active provider/model (dashboard selection).

        Returns the effective ``(provider, model)`` pair. Raises
        ``ValueError`` for an unknown provider id.
        """

        normalized = normalize_provider(provider)

        if normalized not in (PROVIDER_OPENROUTER, PROVIDER_NVIDIA):
            raise ValueError(
                f"Unknown LLM provider '{provider}'. "
                "Use 'openrouter' or 'nvidia'."
            )

        requested_model = (model or "").strip()

        self.ACTIVE_PROVIDER = normalized
        self.ACTIVE_MODEL = (
            requested_model or self.get_default_model(normalized)
        )

        return self.ACTIVE_PROVIDER, self.ACTIVE_MODEL


# Single shared instance so all modules read identical settings.
settings = Settings()
