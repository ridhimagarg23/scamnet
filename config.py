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
