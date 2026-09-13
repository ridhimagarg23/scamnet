"""
config.py
==========
Central configuration for TraceAI.

This module loads runtime settings from environment variables
(via a local ``.env`` file if one exists) and exposes them through
a single ``settings`` object that every other module imports.

Recommended environment variables
---------------------------------
* ``OPENROUTER_API_KEY``  - Your OpenRouter API key. Without it the
                            LLM agents cannot run, so ``POST /analyze``
                            answers HTTP 503 with an explicit
                            configuration message. The server still
                            BOOTS (the dashboard, ``GET /health`` and
                            the integration status/connect endpoints
                            keep working), which is what lets an
                            operator verify an external-app setup
                            before the LLM key is in place. Set
                            ``TRACEAI_STRICT_CONFIG=1`` to restore the
                            old fail-fast-at-import behaviour.

Optional environment variables
------------------------------
* ``LLM_MODEL``           - OpenRouter model id used by every agent.
                            Defaults to ``qwen/qwen3-32b``.
* ``OPENROUTER_BASE_URL`` - OpenAI-compatible gateway root.
                            Defaults to ``https://openrouter.ai/api/v1``;
                            override it for a self-hosted gateway
                            (vLLM, Ollama, LiteLLM) or a test double.
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
    export LLM_MODEL=qwen/qwen3-32b

Usage
-----
>>> from config import settings
>>> settings.LLM_MODEL
'qwen/qwen3-32b'
"""

import logging
import os

from dotenv import load_dotenv

# Load the .env file at the repository root (if present).
# Real credentials are never committed to git; see .env.example.
load_dotenv()


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


class Settings:
    """
    Container of all runtime configuration values.

    Attributes
    ----------
    OPENROUTER_API_KEY : str | None
        Key used to authenticate every LLM call through OpenRouter.
    LLM_MODEL : str
        Model identifier (``vendor/model`` format) used by the
        LLMClient for all agent inference.
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
        # OpenRouter credentials
        # ------------------------------------------------------

        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

        # ------------------------------------------------------
        # Default LLM model
        # ------------------------------------------------------
        # OpenRouter model slugs look like "provider/model-name".
        # qwen/qwen3-32b is a strong, low-cost default for
        # structured JSON classification + chat generation.

        self.LLM_MODEL = os.getenv(
            "LLM_MODEL",
            "qwen/qwen3-32b"
        )

        # OpenAI-compatible gateway root. OpenRouter by default; a
        # self-hosted gateway (vLLM / Ollama / LiteLLM) or a local test
        # double only needs this one variable to be overridden.
        self.OPENROUTER_BASE_URL = os.getenv(
            "OPENROUTER_BASE_URL",
            "https://openrouter.ai/api/v1"
        ).rstrip("/")

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
        # A missing OPENROUTER_API_KEY means the LLM agents cannot run,
        # but it must NOT take the whole server down: an operator
        # verifying an external-app setup (Telegram/Sheets/Drive/Gmail)
        # needs /health and /api/integrations to answer even before the
        # LLM key exists. LLM-backed endpoints refuse honestly (503)
        # instead; see LLMClient and backend/api.py.
        #
        # TRACEAI_STRICT_CONFIG=1 restores the old fail-fast behaviour
        # for deployments that prefer to crash on misconfiguration.
        #
        # NOTE: for offline unit tests simply export a dummy value:
        #   export OPENROUTER_API_KEY=test-key

        self.STRICT_CONFIG = os.getenv(
            "TRACEAI_STRICT_CONFIG", ""
        ).strip().lower() in ("1", "true", "yes", "on")

        if not self.OPENROUTER_API_KEY:

            message = (
                "OPENROUTER_API_KEY not found in .env - LLM features "
                "(/analyze, Telegram conversation, reports) are disabled "
                "until it is set."
            )

            if self.STRICT_CONFIG:
                raise ValueError(message)

            logging.getLogger("TraceAI-Config").warning(message)

    @property
    def llm_configured(self) -> bool:
        """
        True when the LLM (OpenRouter) key is present.

        Surfaced by ``GET /health`` and checked by the agents so a
        missing key becomes one clear 503 instead of a confusing
        provider-side error mid-investigation.
        """

        return bool(self.OPENROUTER_API_KEY)


# Single shared instance so all modules read identical settings.
settings = Settings()
