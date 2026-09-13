"""
model_catalog.py
================
Curated LLM model catalogs for every supported provider.

The catalog feeds TWO consumers:

* the dashboard's model picker (``GET /api/llm/models``) - so the
  analyst can choose a provider + model without editing files;
* the fallback documentation - spare models tried automatically when
  the primary model fails (see ``LLMClient`` and the
  ``*_FALLBACK_MODELS`` variables in ``.env.example``).

Where models come from (merged in this order, first wins per id)
-----------------------------------------------------------------
1. **Built-in curated lists** below (fast, well-tested defaults).
2. **``llm_models.json``** at the repository root - the editable file
   where you paste extra NVIDIA NIM model ids fetched from the API
   (https://build.nvidia.com or ``GET /v1/models``). Accepts::

       {
         "nvidia": ["org/model-id", {"id": "...", "label": "..."}],
         "openrouter": [...]
       }

   A ``{"models": {...}}`` wrapper is accepted too. Unknown shapes are
   ignored (never crash the server over a hand-edited file).
3. **Env extras** - ``NVIDIA_NIM_MODELS`` / ``OPENROUTER_MODELS``
   (comma-separated ids) for models that only exist in one deployment.
4. **Live fetch** (``GET /api/llm/models?refresh=true``) - queries the
   provider's ``/models`` endpoint and lists what the account can
   actually call right now. Results are cached for a few minutes.

Any non-empty model string is ACCEPTED at runtime (select + analyze),
even when it is not in the catalog - new NIM releases keep working
without a code change. Unknown ids simply show without a friendly
label until they are added to ``llm_models.json``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from config import (
    PROVIDER_NVIDIA,
    PROVIDER_OPENROUTER,
    normalize_provider,
    settings,
)

logger = logging.getLogger("TraceAI-ModelCatalog")


# ----------------------------------------------------------------------
# Built-in curated catalogs
# ----------------------------------------------------------------------
# ``speed`` is advisory for the picker UI: "lightning" = smallest/fastest
# instruction models (best for snappy honeypot replies), "fast" = quick
# mid-size, "balanced" = quality/latency trade-off, "powerful" = largest
# reasoning models (slowest, most expensive).

NVIDIA_CATALOG: list = [
    {
        "id": "meta/llama-3.1-8b-instruct",
        "label": "Llama 3.1 8B Instruct",
        "description": "Lightning-fast default for scam chat. Best latency.",
        "speed": "lightning",
        "recommended": True,
    },
    {
        "id": "mistralai/mistral-7b-instruct-v0.3",
        "label": "Mistral 7B Instruct v0.3",
        "description": "Very fast small model, great fallback.",
        "speed": "lightning",
        "recommended": True,
    },
    {
        "id": "google/gemma-2-9b-it",
        "label": "Gemma 2 9B IT",
        "description": "Fast instruction-tuned model, strong JSON output.",
        "speed": "fast",
        "recommended": False,
    },
    {
        "id": "meta/llama-3.1-70b-instruct",
        "label": "Llama 3.1 70B Instruct",
        "description": "High-quality large model for hard verdicts.",
        "speed": "balanced",
        "recommended": False,
    },
    {
        "id": "nvidia/llama-3.1-nemotron-70b-instruct",
        "label": "Llama 3.1 Nemotron 70B",
        "description": "NVIDIA-tuned 70B, excellent instruction following.",
        "speed": "balanced",
        "recommended": False,
    },
    {
        "id": "mistralai/mixtral-8x7b-instruct-v0.1",
        "label": "Mixtral 8x7B Instruct",
        "description": "MoE powerhouse for complex investigations.",
        "speed": "powerful",
        "recommended": False,
    },
]

OPENROUTER_CATALOG: list = [
    {
        "id": "qwen/qwen3-32b",
        "label": "Qwen3 32B",
        "description": "Default OpenRouter model. Strong structured output.",
        "speed": "balanced",
        "recommended": True,
    },
    {
        "id": "meta-llama/llama-3.1-8b-instruct",
        "label": "Llama 3.1 8B (OpenRouter)",
        "description": "Fast + cheap fallback for quick turns.",
        "speed": "fast",
        "recommended": True,
    },
    {
        "id": "mistralai/mistral-7b-instruct",
        "label": "Mistral 7B (OpenRouter)",
        "description": "Fast small fallback.",
        "speed": "fast",
        "recommended": False,
    },
    {
        "id": "google/gemini-flash-1.5",
        "label": "Gemini Flash 1.5",
        "description": "Google's fast model via OpenRouter.",
        "speed": "fast",
        "recommended": False,
    },
    {
        "id": "anthropic/claude-3.5-sonnet",
        "label": "Claude 3.5 Sonnet",
        "description": "Top-tier reasoning for difficult cases (slower).",
        "speed": "powerful",
        "recommended": False,
    },
]

_BUILTIN_CATALOGS = {
    PROVIDER_NVIDIA: NVIDIA_CATALOG,
    PROVIDER_OPENROUTER: OPENROUTER_CATALOG,
}

#: Editable model file at the repository root (see module docstring).
CUSTOM_MODELS_FILENAME = "llm_models.json"

#: Per-provider env vars holding extra comma-separated model ids.
EXTRA_MODELS_ENV = {
    PROVIDER_NVIDIA: ("NVIDIA_NIM_MODELS", "NVIDIA_MODELS"),
    PROVIDER_OPENROUTER: ("OPENROUTER_MODELS",),
}

#: How long live ``/models`` responses are cached (seconds).
LIVE_CACHE_TTL = 300

_live_cache: dict = {}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _custom_models_path() -> Path:
    """Absolute path of the editable ``llm_models.json`` file."""

    return Path(__file__).resolve().parent.parent / CUSTOM_MODELS_FILENAME


def _normalize_entry(entry: Any) -> dict | None:
    """
    Normalize one catalog entry to ``{"id": ..., ...}``.

    Accepts a plain model-id string or a dict with an ``id`` key.
    Returns ``None`` for anything else (never raise on bad input).
    """

    if isinstance(entry, str):
        model_id = entry.strip()

        if not model_id:
            return None

        return {"id": model_id}

    if isinstance(entry, dict):
        model_id = str(entry.get("id", "") or "").strip()

        if not model_id:
            return None

        normalized = dict(entry)
        normalized["id"] = model_id
        return normalized

    return None


def _load_custom_models() -> dict:
    """
    Read the editable ``llm_models.json`` file (best effort).

    Returns ``{"nvidia": [...], "openrouter": [...]}`` with normalized
    entries; missing/unreadable/invalid files yield empty lists and a
    log line instead of an exception.
    """

    path = _custom_models_path()

    empty: dict = {
        PROVIDER_NVIDIA: [],
        PROVIDER_OPENROUTER: [],
    }

    if not path.exists():
        return empty

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - hand-edited file
        logger.warning(
            "Ignoring %s (%s). Fix the JSON to load custom models.",
            CUSTOM_MODELS_FILENAME,
            exc,
        )
        return empty

    if isinstance(raw, dict) and isinstance(raw.get("models"), dict):
        raw = raw["models"]

    if not isinstance(raw, dict):
        logger.warning(
            "Ignoring %s: top-level object must map provider ids to "
            "model lists.",
            CUSTOM_MODELS_FILENAME,
        )
        return empty

    for provider in (PROVIDER_NVIDIA, PROVIDER_OPENROUTER):
        entries = raw.get(provider, [])

        if isinstance(entries, str):
            entries = [entries]

        if not isinstance(entries, list):
            continue

        for item in entries:
            normalized = _normalize_entry(item)

            if normalized is not None:
                empty[provider].append(normalized)

    # Also accept the "nim" alias key for convenience.
    nim_entries = raw.get("nim", [])

    if isinstance(nim_entries, list):
        for item in nim_entries:
            normalized = _normalize_entry(item)

            if normalized is not None:
                empty[PROVIDER_NVIDIA].append(normalized)

    return empty


def _load_env_models(provider: str) -> list:
    """Read extra comma-separated model ids from provider env vars."""

    for env_name in EXTRA_MODELS_ENV.get(provider, ()):
        raw = os.getenv(env_name, "")

        if raw and raw.strip():
            return [
                {"id": item.strip(), "source": "env"}
                for item in raw.split(",")
                if item.strip()
            ]

    return []


def _merge_preserving_first(*lists: list) -> list:
    """
    Merge catalog lists, keeping the FIRST occurrence of each model id.

    Dict entries for the same id are shallow-merged so a custom file can
    override e.g. just the label of a built-in model.
    """

    merged: dict = {}

    for entries in lists:
        for entry in entries:
            model_id = entry.get("id")

            if not model_id:
                continue

            if model_id in merged:
                merged[model_id] = {**merged[model_id], **entry}
            else:
                merged[model_id] = dict(entry)

    return list(merged.values())


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def get_models(provider: str | None) -> list:
    """
    Return the merged model catalog for one provider.

    Order: built-in curated entries first (recommended models at the
    top), then ``llm_models.json`` additions, then env extras. Every
    entry is a dict with at least ``id`` and usually ``label``,
    ``description`` and ``speed``.
    """

    normalized = normalize_provider(provider) or PROVIDER_OPENROUTER

    if normalized not in _BUILTIN_CATALOGS:
        normalized = PROVIDER_OPENROUTER

    custom = _load_custom_models()
    env_models = _load_env_models(normalized)

    return _merge_preserving_first(
        _BUILTIN_CATALOGS[normalized],
        custom.get(normalized, []),
        env_models,
    )


def get_model_ids(provider: str | None) -> list:
    """Return just the model ids of ``get_models(provider)``."""

    return [entry["id"] for entry in get_models(provider)]


def is_known_model(provider: str | None, model_id: str | None) -> bool:
    """True when ``model_id`` is in the provider's catalog."""

    if not model_id:
        return False

    wanted = str(model_id).strip()

    return any(entry["id"] == wanted for entry in get_models(provider))


def describe_model(provider: str | None, model_id: str) -> dict:
    """
    Return the catalog entry for a model id.

    Unknown ids yield a synthesized entry (label = id) so brand-new
    NIM releases still display and run without a catalog update.
    """

    wanted = str(model_id or "").strip()

    for entry in get_models(provider):
        if entry["id"] == wanted:
            return dict(entry)

    return {
        "id": wanted,
        "label": wanted,
        "description": "Custom model id (not in the curated catalog).",
        "speed": "unknown",
        "recommended": False,
        "custom": True,
    }


def fetch_live_models(
    provider: str | None,
    *,
    timeout: float = 15.0,
    force_refresh: bool = False,
) -> tuple:
    """
    Query the provider's live ``/models`` endpoint (best effort).

    Parameters
    ----------
    provider : str | None
        ``"nvidia"`` or ``"openrouter"`` (aliases accepted).
    timeout : float
        HTTP timeout in seconds (kept short - this backs a UI button).
    force_refresh : bool
        Ignore the in-memory cache and re-query the provider.

    Returns
    -------
    (models, source) : (list[dict] | None, str)
        ``models`` is None when the live query failed (caller should
        fall back to :func:`get_models`); ``source`` is ``"live"``,
        ``"live-cache"`` or ``"unavailable: <reason>"``.
    """

    normalized = normalize_provider(provider) or settings.ACTIVE_PROVIDER

    if normalized not in _BUILTIN_CATALOGS:
        return None, f"unavailable: unknown provider '{provider}'"

    now = time.monotonic()
    cached = _live_cache.get(normalized)

    if (
        not force_refresh
        and cached is not None
        and now - cached["fetched_at"] < LIVE_CACHE_TTL
    ):
        return list(cached["models"]), "live-cache"

    api_key = settings.get_api_key(normalized)
    base_url = settings.get_base_url(normalized)

    if normalized == PROVIDER_NVIDIA and not api_key:
        return None, "unavailable: NVIDIA_NIM_API_KEY is not configured"

    try:
        import httpx
    except ImportError:  # pragma: no cover - dependency is required
        return None, "unavailable: httpx is not installed"

    headers = {"Accept": "application/json"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base_url}/models"

    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - network trouble
        logger.warning("Live /models fetch for '%s' failed: %s", normalized, exc)
        return None, f"unavailable: {exc}"

    items = payload.get("data", payload if isinstance(payload, list) else [])

    if not isinstance(items, list):
        return None, "unavailable: unexpected /models response shape"

    known_ids = {entry["id"] for entry in get_models(normalized)}
    catalog_order = {entry["id"]: index for index, entry in enumerate(get_models(normalized))}

    live: list = []

    for item in items:
        if not isinstance(item, dict):
            continue

        model_id = str(item.get("id", "") or "").strip()

        if not model_id:
            continue

        live.append({
            "id": model_id,
            "label": item.get("name") or model_id,
            "description": (
                "Available on your account right now (live)."
                if model_id in known_ids
                else "Live model (not in the curated catalog)."
            ),
            "speed": "unknown",
            "recommended": False,
            "live": True,
        })

    # Catalog-known models first (in catalog order), then the rest A-Z.
    live.sort(key=lambda e: (0, catalog_order[e["id"]]) if e["id"] in catalog_order else (1, e["id"]))

    _live_cache[normalized] = {"models": live, "fetched_at": now}

    return list(live), "live"
