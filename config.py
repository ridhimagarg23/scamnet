"""
config.py
==========
Central configuration for TraceAI.

This module loads runtime settings from environment variables
(via a local ``.env`` file if one exists) and exposes them through
a single ``settings`` object that every other module imports.

Required environment variables
------------------------------
* ``OPENROUTER_API_KEY``  - Your OpenRouter API key (no default).
                            TraceAI refuses to start without it so that
                            misconfiguration fails fast at import time
                            instead of mid-request.

Optional environment variables
------------------------------
* ``LLM_MODEL``           - OpenRouter model id used by every agent.
                            Defaults to ``qwen/qwen3-32b``.

Optional SCAMNET integration variables (all default to unset; see
``integrations/`` - missing values simply keep the corresponding
external app in the honest "not_configured" state):

* ``TELEGRAM_BOT_TOKEN``            - Telegram Bot API token (@BotFather).
* ``TELEGRAM_API_BASE``             - Bot API root (default
                                      ``https://api.telegram.org``).
* ``GOOGLE_SHEETS_CREDENTIALS_FILE``- Server-side path to the Sheets
                                      service-account / OAuth JSON key.
* ``GOOGLE_SHEETS_SPREADSHEET_ID``  - Optional target spreadsheet id.
* ``GOOGLE_DRIVE_CREDENTIALS_FILE`` - Server-side path to the Drive
                                      service-account / OAuth JSON key.
* ``GOOGLE_DRIVE_FOLDER_ID``        - Optional report destination folder.

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

import os

from dotenv import load_dotenv

# Load the .env file at the repository root (if present).
# Real credentials are never committed to git; see .env.example.
load_dotenv()


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

        # --- Google Sheets (live investigation evidence) ---
        # Server-side path to the service-account / OAuth JSON key.
        self.GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_SHEETS_CREDENTIALS_FILE"
        )
        # Optional target spreadsheet (created per investigation if unset).
        self.GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv(
            "GOOGLE_SHEETS_SPREADSHEET_ID"
        )

        # --- Google Drive (investigation reports) ---
        # Server-side path to the service-account / OAuth JSON key.
        self.GOOGLE_DRIVE_CREDENTIALS_FILE = os.getenv(
            "GOOGLE_DRIVE_CREDENTIALS_FILE"
        )
        # Optional destination folder for generated report files.
        self.GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

        # ------------------------------------------------------
        # Validation
        # ------------------------------------------------------
        # Fail at startup (not on the first /analyze request) when
        # the key is missing. This makes configuration problems
        # visible immediately in production logs.
        #
        # NOTE: for offline unit tests simply export a dummy value:
        #   export OPENROUTER_API_KEY=test-key

        if not self.OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY not found in .env"
            )


# Single shared instance so all modules read identical settings.
settings = Settings()
