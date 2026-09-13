"""
api.py
======
TraceAI Production FastAPI Backend

This is the HTTP surface of the whole platform. The Next.js dashboard
talks to these endpoints:

    POST /analyze   - feed one scammer message into the pipeline
    POST /new       - reset a session (start a fresh case)
    GET  /health    - liveness probe for hosting platforms

    GET  /api/integrations                    - honest status of the
                                                external-app integration
                                                layer (SCAMNET)
    POST /api/integrations/{id}/connect       - attempt a real connect
                                                (Telegram: getMe verify;
                                                501 while a provider's
                                                auth flow is not built)

    GET  /api/telegram/messages               - fetch recent incoming
                                                Telegram messages (test)
    POST /api/telegram/send-test              - send one test message
                                                to a chat_id
                                                (see backend/telegram_routes.py)

One /analyze turn runs this pipeline:

    scammer message
        -> InvestigationAgent        (IOC regex + URL checks + LLM verdict
                                       + deterministic risk score)
        -> AdaptiveInvestigationEngine (persona profile + objective ladder)
        -> ConversationAgent         (persona's next reply)
        -> MemoryManager             (archive case facts to JSON)
        -> ReportAgent               (markdown incident report)
        -> JSON payload for the dashboard

Session state (per session_id) lives in the in-memory ``sessions``
dict, so a multi-turn undercover conversation is stateful across
requests but resets when the process restarts.
"""

import datetime
import logging
import time
import traceback
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import normalize_provider, settings
from llm.llm_client import LLMNotConfiguredError
from llm.model_catalog import (
    describe_model,
    fetch_live_models,
    get_models,
    is_known_model,
)

from agents.investigation_agent import InvestigationAgent
from agents.conversation_agent import ConversationAgent
from agents.report_agent import ReportAgent

from tools.adaptive_investigation_engine import AdaptiveInvestigationEngine
from tools.telegram_conversation_worker import (
    get_worker,
    start_worker,
    stop_worker,
)
from tools.conversation_session import ConversationSession
from tools.evidence_archive import EvidenceArchiver
from tools.memory_manager import MemoryManager
from tools.entity_extractor import EntityExtractor
from tools.url_checker import URLChecker
from tools.risk_engine import RiskEngine

from integrations import (
    get_all_integration_statuses,
    get_integration,
)
from integrations.base import (
    IntegrationConnectionError,
    IntegrationNotConfiguredError,
    IntegrationNotImplementedError,
)

from backend.telegram_routes import router as telegram_router


# --------------------------------------------------
# Setup Logging
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger("TraceAI-API")


# --------------------------------------------------
# Initialize FastAPI App
# --------------------------------------------------

app = FastAPI(
    title="TraceAI API",
    description="Backend API for TraceAI Undercover Scam Investigation Platform",
    version="1.0.0"
)


# --------------------------------------------------
# Enable CORS for direct (cross-origin) API consumers
# --------------------------------------------------
# The bundled Next.js dashboard proxies same-origin through
# /backend-api (see frontend/next.config.mjs), so it needs no CORS
# entry. These origins cover local development and deployments that
# call the API directly; extend with CORS_ALLOW_ORIGINS=<csv> instead
# of editing this file (the previous hard-coded allow-list meant a
# dashboard on any other host could not reach the API at all).

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# Register integration sub-routers
# --------------------------------------------------
# Telegram test endpoints (GET /api/telegram/messages,
# POST /api/telegram/send-test) live in backend/telegram_routes.py.

app.include_router(telegram_router)





# --------------------------------------------------
# Session Memory (In-Memory Dictionary)
# --------------------------------------------------

# Stores state for active investigations, keyed by session_id.
#
# Each value is a dict with this shape:
#   {
#     "session":        ConversationSession      (chat transcript)
#     "engine":         AdaptiveInvestigationEngine (objectives/profile)
#     "investigation":  InvestigationResult | None (accumulated case facts)
#     "report":         ReportResult | None       (latest markdown report)
#     "persona_profile": dict | None              (UI persona payload)
#     "timeline":       list[dict]                (activity feed for the UI)
#     "drive_file_id":  str | None               (archived report file id)
#   }
#
# NOTE: in-memory only - state is lost on process restart. Scale-out
# would require swapping this dict for Redis or similar. The dict is
# capped (FIFO eviction) so an unattended server cannot grow without
# bound as new session ids keep arriving.
sessions: Dict[str, dict] = {}

#: Maximum number of concurrently retained sessions. Each session holds
#: a transcript + IOC model, so this is a few hundred KB at most.
MAX_SESSIONS = 200


class InvestigationRequest(BaseModel):
    """Request body for POST /analyze."""

    message: str
    """The scammer message to investigate (non-empty)."""

    session_id: Optional[str] = "default"
    """Identifies the undercover conversation (state continuity)."""

    provider: Optional[str] = None
    """Optional per-request LLM provider (``openrouter`` / ``nvidia``)."""

    model: Optional[str] = None
    """Optional per-request LLM model id (any non-empty string works)."""


class LLMSelectRequest(BaseModel):
    """Request body for POST /api/llm/select (ops/debug override)."""

    provider: str
    """Provider to activate (``openrouter`` / ``nvidia``)."""

    model: Optional[str] = None
    """Model id to activate (defaults to the provider's default)."""


# --------------------------------------------------
# Helper Functions
# --------------------------------------------------

def get_current_time_str() -> str:
    return datetime.datetime.now().strftime("%I:%M %p")


def _log_stage(session_id: str, name: str, started: float) -> float:
    """
    Log how long one ``/analyze`` pipeline stage took.

    A full turn runs three sequential LLM calls plus archiving, so a
    slow model shows up here first (``investigation`` / ``conversation`` /
    ``report`` each taking 10 s+). Returns the new timestamp so callers
    can chain stages without re-reading the clock.
    """

    now = time.perf_counter()

    logger.info(
        "Session '%s': stage '%s' took %.1fs.",
        session_id, name, now - started,
    )

    return now


def _llm_stage_info(agent) -> dict:
    """
    Secret-free snapshot of which provider/model answered one stage.

    The LLMClient falls back across spare models (and providers) on
    failure, so the model that ANSWERED can differ from the requested
    one - the dashboard shows this so "why so fast/slow?" is answerable.
    """

    llm = getattr(agent, "llm", None)

    return {
        "provider": getattr(llm, "last_provider", None),
        "model": getattr(llm, "last_model", None),
        "fallbacks_tried": len(getattr(llm, "fallbacks_used", None) or []),
    }


def get_persona_profile(threat_type: str, state) -> dict:
    """
    Maps abstract investigation profile characteristics to a concrete
    persona for the UI.

    The engine (``AdaptiveInvestigationEngine``) decides *how* the
    persona communicates (language / style / literacy); this helper
    decides *who* the persona is - a named cover identity with an
    occupation that fits the threat family (e.g. a bank scam gets
    "Rahul Sharma, Working Professional").

    Returns a payload shaped exactly like the dashboard's
    ``PersonaPanel`` expects (see frontend/lib/constants.js for the
    initial/empty counterpart).
    """

    threat = threat_type.lower()

    if "bank" in threat or "sbi" in threat:
        name = "Rahul Sharma"
        occupation = "Working Professional"
        initials = "RS"

    elif "job" in threat or "recruiter" in threat:
        name = "Priya Patel"
        occupation = "Recent Graduate"
        initials = "PP"

    elif "investment" in threat or "crypto" in threat or "stock" in threat:
        name = "Vikram Mehta"
        occupation = "Retired Bank Manager"
        initials = "VM"

    else:
        name = "Amit Kumar"
        occupation = "College Student"
        initials = "AK"

    return {
        "name": name,
        "occupation": occupation,
        "avatar": None,
        "initials": initials,
        "traits": [
            {
                "icon": "globe",
                "label": "Language",
                "value": state.profile.language
            },
            {
                "icon": "message",
                "label": "Communication Style",
                "value": state.profile.communication_style
            },
            {
                "icon": "alert",
                "label": "Risk Approach",
                "value": "Cautious"
            },
            {
                "icon": "user",
                "label": "Strategy",
                "value": state.current_strategy
            },
            {
                "icon": "bar",
                "label": "Digital Literacy",
                "value": state.profile.digital_literacy
            },
            {
                "icon": "shield",
                "label": "Current Objective",
                "value": state.current_objective
            }
        ],
        "aiTip": (
            f"Objective: {state.current_objective}. "
            f"Strategy: {state.current_strategy} response style."
        )
    }


def build_progress(investigation, state, has_report: bool = False) -> list:
    """
    Generates the 5-step investigation progress list for the UI.

    Each step is one of ``done`` / ``current`` / ``locked`` based on
    the accumulated investigation, the engine's turn counter and
    whether a report exists yet:

        1. Threat Detected       - LLM flagged the message as a scam
        2. IOC Extracted         - at least one IOC family captured
        3. Undercover Engagement - dialogue has started (turn > 1)
        4. Evidence Secured      - IOCs exist AND dialogue is ongoing
        5. Report Ready          - a report exists AND the case has
                                   enough evidence to hand over

    A sequential-cleanup pass guarantees a step can never be "locked"
    right after a "done" step (no gaps in the UI stepper).
    """

    has_iocs = (
        len(investigation.phone_numbers) > 0
        or len(investigation.emails) > 0
        or len(investigation.urls) > 0
        or len(investigation.upi_ids) > 0
    )

    steps = [
        {
            "label": "Threat\nDetected",
            "state": "done" if investigation.is_scam else "locked"
        },
        {
            "label": "IOC\nExtracted",
            "state": "done" if has_iocs else "current"
        },
        {
            "label": "Undercover\nEngagement",
            "state": (
                "done"
                if state.turn_number > 2
                else ("current" if state.turn_number > 1 else "locked")
            )
        },
        {
            "label": "Evidence\nSecured",
            "state": (
                "done"
                if (has_iocs and state.turn_number > 2)
                else "locked"
            )
        },
        {
            "label": "Report\nReady",
            "state": (
                "done"
                if (has_report and has_iocs and state.turn_number > 2)
                else ("current" if has_report else "locked")
            )
        }
    ]

    # Clean up sequential logic
    # (cannot have "locked" right after "done")
    for i in range(len(steps) - 1):
        if (
            steps[i]["state"] == "done"
            and steps[i + 1]["state"] == "locked"
        ):
            steps[i + 1]["state"] = "current"
            break

    return steps


def build_evidence(investigation) -> list:
    """
    Maps extracted indicators to the evidence-tracker format of the UI.

    Every IOC family yields one or more entries of the shape
    ``{"type", "name", "status"}`` where status is:

    * ``pending``   - nothing collected for this family yet
    * ``collected`` - at least one real indicator found
    * ``verified``  - treated as extra-confident (bank names)

    Unknown/empty families still render as "pending" rows so the
    analyst can see at a glance which evidence is still missing.
    """

    evidence = []

    # --------------------------------------------------
    # Website URLs
    # --------------------------------------------------

    if investigation.urls:
        for url in investigation.urls:
            evidence.append({
                "type": "website",
                "name": f"URL: {url}",
                "status": "collected"
            })
    else:
        evidence.append({
            "type": "website",
            "name": "Website URL",
            "status": "pending"
        })

    # --------------------------------------------------
    # Phone numbers
    # --------------------------------------------------

    if investigation.phone_numbers:
        for phone in investigation.phone_numbers:
            evidence.append({
                "type": "phone",
                "name": f"Phone: {phone}",
                "status": "collected"
            })
    else:
        evidence.append({
            "type": "phone",
            "name": "Phone Number",
            "status": "pending"
        })

    # --------------------------------------------------
    # Email Addresses
    # --------------------------------------------------

    if investigation.emails:
        for email in investigation.emails:
            evidence.append({
                "type": "email",
                "name": f"Email: {email}",
                "status": "collected"
            })
    else:
        evidence.append({
            "type": "email",
            "name": "Email Address",
            "status": "pending"
        })

    # --------------------------------------------------
    # UPI IDs
    # --------------------------------------------------

    if investigation.upi_ids:
        for upi in investigation.upi_ids:
            evidence.append({
                "type": "upi",
                "name": f"UPI: {upi}",
                "status": "collected"
            })
    else:
        evidence.append({
            "type": "upi",
            "name": "UPI ID",
            "status": "pending"
        })

    # --------------------------------------------------
    # Bank Names
    # --------------------------------------------------

    if investigation.bank_names:
        for bank in investigation.bank_names:
            evidence.append({
                "type": "bank",
                "name": f"Bank Name: {bank}",
                "status": "verified"
            })

    return evidence


# --------------------------------------------------
# ENDPOINTS
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "status": "running",
        "message": "TraceAI API is online 🚀",
        "timestamp": datetime.datetime.now().isoformat()
    }


@app.get("/health")
def health():
    """
    Liveness probe, plus an honest view of what the server can do.

    ``status`` is ``healthy`` when at least one LLM provider key is
    configured and ``degraded`` when none is - the server still serves
    the dashboard, ``/api/integrations`` and every integration connect
    attempt, so an operator can verify the external-app setup first.
    """

    llm_configured = bool(settings.llm_configured)

    return {
        "status": "healthy" if llm_configured else "degraded",
        "llm_configured": llm_configured,
        "llm_model": settings.ACTIVE_MODEL if llm_configured else None,
        "active_provider": settings.ACTIVE_PROVIDER,
        "active_model": settings.ACTIVE_MODEL,
        "providers": {
            "openrouter": {
                "configured": settings.openrouter_configured,
                "default_model": settings.get_default_model("openrouter"),
            },
            "nvidia": {
                "configured": settings.nvidia_configured,
                "default_model": settings.get_default_model("nvidia"),
            },
        },
    }


# --------------------------------------------------
# LLM provider / model selection endpoints
# --------------------------------------------------
# These endpoints describe/override the LLM selection for operators.
# The dashboard no longer renders a picker: the engine order is fixed
# (OpenRouter first, NVIDIA nemotron stage as the fallback), so these
# are diagnostics + an escape hatch for API consumers.
# Responses NEVER contain secret values - only key presence, model
# ids and human-readable hints.

def _llm_status_payload() -> dict:
    """Secret-free snapshot of the LLM provider selection state."""

    return {
        "active_provider": settings.ACTIVE_PROVIDER,
        "active_model": settings.ACTIVE_MODEL,
        "active_model_info": describe_model(
            settings.ACTIVE_PROVIDER, settings.ACTIVE_MODEL
        ),
        "llm_configured": settings.llm_configured,
        "cross_provider_fallback": settings.LLM_CROSS_PROVIDER_FALLBACK,
        "providers": {
            "openrouter": {
                "id": "openrouter",
                "label": "OpenRouter",
                "configured": settings.openrouter_configured,
                "key_name": "OPENROUTER_API_KEY",
                "base_url": settings.get_base_url("openrouter"),
                "default_model": settings.get_default_model("openrouter"),
                "fallback_models": settings.get_fallback_models("openrouter"),
                "timeout": settings.get_timeout("openrouter"),
            },
            "nvidia": {
                "id": "nvidia",
                "label": "NVIDIA NIM",
                "tagline": "Lightning-fast replies",
                "configured": settings.nvidia_configured,
                "key_name": "NVIDIA_NIM_API_KEY",
                "base_url": settings.get_base_url("nvidia"),
                "default_model": settings.get_default_model("nvidia"),
                "fallback_models": settings.get_fallback_models("nvidia"),
                "timeout": settings.get_timeout("nvidia"),
            },
        },
    }


@app.get("/api/llm/status")
def llm_status():
    """
    Returns the active LLM provider/model plus per-provider status.

    Diagnostic endpoint (the dashboard no longer shows a picker) -
    useful to confirm which keys the server sees. Never returns
    secrets, only key presence.
    """

    return _llm_status_payload()


@app.get("/api/llm/models")
def llm_models(provider: Optional[str] = None, refresh: bool = False):
    """
    Returns the model catalog for one provider (diagnostics).

    Query parameters
    ----------------
    provider : str, optional
        ``openrouter`` or ``nvidia`` (default: the active provider).
    refresh : bool, default False
        When true, query the provider's live ``/models`` endpoint first
        (needs the provider key for NVIDIA NIM); on any failure the
        curated catalog is returned instead with ``source`` explaining
        what happened - this endpoint never answers 5xx for a provider
        hiccup.
    """

    normalized = normalize_provider(provider) or settings.ACTIVE_PROVIDER

    if not settings.is_known_provider(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "unknown_llm_provider",
                "message": (
                    f"Unknown LLM provider '{provider}'. "
                    "Use 'openrouter' or 'nvidia'."
                ),
            },
        )

    models = get_models(normalized)
    source = "catalog"

    if refresh:
        live_models, live_source = fetch_live_models(
            normalized, force_refresh=True
        )

        if live_models:
            models = live_models
            source = live_source
        else:
            source = f"catalog ({live_source})"

    return {
        "provider": normalized,
        "configured": settings.is_provider_configured(normalized),
        "key_name": settings.provider_key_name(normalized),
        "active_model": (
            settings.ACTIVE_MODEL
            if normalized == settings.ACTIVE_PROVIDER
            else settings.get_default_model(normalized)
        ),
        "default_model": settings.get_default_model(normalized),
        "fallback_models": settings.get_fallback_models(normalized),
        "models": models,
        "source": source,
    }


@app.post("/api/llm/select")
def llm_select(request: LLMSelectRequest):
    """
    Switches the runtime-active LLM provider/model (ops override).

    Any non-empty model id is accepted (new NIM releases work without
    a catalog update); unknown ids are flagged via ``model_known`` so
    the UI can show a gentle hint instead of blocking the user.
    """

    provider = normalize_provider(request.provider)

    if not settings.is_known_provider(provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "unknown_llm_provider",
                "message": (
                    f"Unknown LLM provider '{request.provider}'. "
                    "Use 'openrouter' or 'nvidia'."
                ),
            },
        )

    if not settings.is_provider_configured(provider):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "status": "llm_provider_not_configured",
                "message": (
                    f"{settings.provider_key_name(provider)} is not set "
                    "on the server, so the provider cannot be activated. "
                    "Add it to the server-side .env (see .env.example) "
                    "and restart the backend."
                ),
            },
        )

    requested_model = (request.model or "").strip() or None

    try:
        active_provider, active_model = settings.set_active(
            provider, requested_model
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "unknown_llm_provider",
                "message": str(exc),
            },
        )

    logger.info(
        "LLM selection switched to provider '%s', model '%s'.",
        active_provider,
        active_model,
    )

    payload = _llm_status_payload()
    payload["model_known"] = is_known_model(active_provider, active_model)

    if not payload["model_known"]:
        payload["model_hint"] = (
            f"Model '{active_model}' is not in the curated catalog - it "
            "will still be tried first, with automatic fallback to the "
            "configured spare models if the provider rejects it. Add it "
            "to llm_models.json to give it a friendly label."
        )

    return payload


# --------------------------------------------------
# SCAMNET Integration Endpoints
# --------------------------------------------------
# Honest status of the external-app integration layer
# (Telegram / Google Sheets / Google Drive / Gmail - see integrations/).
#
# Contract:
#   * responses NEVER contain secret values (only setting names and
#     human-readable state descriptions);
#   * ``connected`` is only ever true after a REAL authenticated
#     session was established and health-verified server-side;
#   * every provider implements a genuine connect() flow; a provider
#     whose auth flow is ever unavailable answers HTTP 501
#     ("setup_required") instead of faking success.

@app.get("/api/integrations")
def integrations_status():
    """
    Returns the real configuration/authentication status of every
    registered integration, keyed by integration id:

        {
          "telegram":      {"available": bool, "connected": bool, ...},
          "google_sheets": {"available": bool, "connected": bool, ...},
          "google_drive":  {"available": bool, "connected": bool, ...},
          "gmail":         {"available": bool, "connected": bool, ...}
        }

    Each value also carries name/purpose/configured/state/detail/
    setup_instructions for the dashboard's Connected Apps UI.
    """

    return get_all_integration_statuses()


# ----------------------------------------------------------
# Telegram auto-start (the reply loop must not need a manual click)
# ----------------------------------------------------------
# A connected bot that is not polling is indistinguishable from a broken
# bot: the operator sends a message and nothing ever comes back. So the
# loop is brought up automatically - at boot when the token works, and
# immediately after a successful Connect - unless the operator turned
# it off with TELEGRAM_AUTO_START_WORKER=0.

def _telegram_worker_stats() -> Optional[Dict]:
    """Secret-free worker snapshot (None when the loop never ran)."""

    worker = get_worker()

    return worker.stats() if worker else None


def _autostart_telegram_worker(integration) -> Optional[Dict]:
    """
    Start the reply loop for a connected Telegram integration.

    Never raises: a polling problem must not turn a successful Connect
    into an HTTP error - it is reported inside the response instead.
    """

    if not settings.TELEGRAM_AUTO_START_WORKER:
        return _telegram_worker_stats()

    try:
        start_worker(integration)

    except Exception as exc:  # noqa: BLE001 - connect already succeeded
        logger.warning(
            "Telegram connected, but the reply loop could not be "
            "started: %s",
            exc,
        )
        return {
            "running": False,
            "start_error": str(exc),
        }

    stats = _telegram_worker_stats()

    logger.info(
        "Telegram reply loop started automatically (polls=%s).",
        (stats or {}).get("polls"),
    )

    return stats


def _bootstrap_telegram_on_boot() -> None:
    """
    Connect + start the bot in the background when a token is present.

    Runs in a daemon thread so an unreachable Telegram (or a slow DNS
    lookup) can never delay application start-up. Silent when the token
    is simply not configured - that is a normal, supported state.
    """

    integration = get_integration("telegram")

    if integration is None or not integration.is_configured():
        logger.info(
            "Telegram bot token not configured - reply loop not started."
        )
        return

    if not settings.TELEGRAM_AUTO_START_WORKER:
        logger.info(
            "TELEGRAM_AUTO_START_WORKER is disabled - the bot will not "
            "poll until /api/telegram/conversation/start is called."
        )
        return

    try:
        if not integration.is_connected():
            integration.connect()

    except Exception as exc:  # noqa: BLE001 - boot must never fail
        logger.warning(
            "Telegram boot connect failed (%s). The bot will not poll "
            "until POST /api/integrations/telegram/connect succeeds.",
            exc,
        )
        return

    _autostart_telegram_worker(integration)


@app.on_event("startup")
def _startup_bootstrap() -> None:
    """Kick off the Telegram reply loop without blocking start-up."""

    import threading

    threading.Thread(
        target=_bootstrap_telegram_on_boot,
        name="telegram-boot-bootstrap",
        daemon=True,
    ).start()


@app.post("/api/integrations/{integration_id}/connect")
def integrations_connect(integration_id: str):
    """
    Attempts to establish a REAL connection for one integration.

    Honest outcomes (no fake success states):
      404 - unknown integration id
      409 - server-side credentials missing/invalid ("not_configured")
      501 - a provider's real auth flow is unavailable
            ("setup_required")
      502 - a real connection attempt failed ("connection_failed" -
            e.g. Telegram rejected the token, Google rejected the
            credentials or the network is down; the message is
            sanitised and never contains the token)
      200 - a genuine authenticated connection was established; the
            fresh IntegrationStatus payload is returned. For Telegram
            that means getMe verified the bot; for the Google apps it
            means an authorized API call (Drive about.get, Sheets
            spreadsheet read/create, Gmail getProfile) succeeded.
    """

    integration = get_integration(integration_id)

    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown integration '{integration_id}'."
        )

    logger.info(
        f"Connect requested for integration '{integration_id}'."
    )

    try:
        integration.connect()

    except IntegrationNotConfiguredError as exc:
        # Credential problem: the operator must configure the server
        # .env first. The message names settings only - never values.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "status": "not_configured",
                "message": str(exc)
            }
        )

    except IntegrationNotImplementedError as exc:
        # The real auth flow does not exist yet. 501 makes "not built"
        # indistinguishable-from-success impossible for any client.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "status": "setup_required",
                "message": str(exc)
            }
        )

    except IntegrationConnectionError as exc:
        # A REAL attempt was made and failed (Telegram rejected the
        # token, network unreachable, upstream error). The message was
        # sanitised by the provider client - no token, no request URL.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "status": "connection_failed",
                "message": str(exc)
            }
        )

    # Reached only when connect() performed a real, verified handshake.
    payload = integration.get_status().model_dump(mode="json")

    if integration_id == "telegram":
        # Bring the reply loop up straight away: otherwise the operator
        # has to know about a second endpoint before the bot answers
        # anything, which is exactly the "bot never replies" trap.
        payload["worker"] = _autostart_telegram_worker(integration)

        if not (payload.get("worker") or {}).get("running"):
            payload["next_step"] = (
                "Reply loop is not running. Check the worker message in "
                "this response, then POST /api/telegram/conversation/"
                "start or fix the reported problem."
            )

    return payload


@app.post("/api/integrations/{integration_id}/disconnect")
def integrations_disconnect(integration_id: str):
    """
    Drop the live session of one integration (idempotent).

    Disconnecting never destroys server-side credentials - the next
    Connect attempt re-reads them - it only clears the verified session
    so the dashboard stops claiming a live connection.
    """

    integration = get_integration(integration_id)

    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown integration '{integration_id}'."
        )

    logger.info(
        f"Disconnect requested for integration '{integration_id}'."
    )

    try:
        integration.disconnect()

    except IntegrationNotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "status": "setup_required",
                "message": str(exc)
            }
        )

    except IntegrationConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "status": "disconnect_failed",
                "message": str(exc)
            }
        )

    payload = integration.get_status().model_dump(mode="json")

    if integration_id == "telegram":
        # A disconnected bot must not keep long-polling Telegram with a
        # session the dashboard no longer claims.
        try:
            stop_worker()
        except Exception as exc:  # noqa: BLE001 - disconnect succeeded
            logger.warning(
                "Telegram disconnected, but the reply loop could not be "
                "stopped: %s", exc,
            )

        payload["worker"] = _telegram_worker_stats()

    return payload


@app.options("/new")
def new_options():
    logger.info("Received OPTIONS request for /new preflight")
    return {"status": "ok"}

@app.post("/new")
def new_investigation(request: Dict[str, str]):
    """
    Clears the investigation session state to begin a new case.
    """

    session_id = request.get("session_id", "default")

    if session_id in sessions:
        del sessions[session_id]
        logger.info(f"Session '{session_id}' has been reset.")

    return {
        "status": "success",
        "message": f"Session '{session_id}' successfully reset."
    }

@app.options("/analyze")
def analyze_options():
    logger.info("Received OPTIONS request for /analyze preflight")
    return {"status": "ok"}

@app.post("/analyze")
def analyze(request: InvestigationRequest):
    """
    Processes scammer messages, runs undercover dialogue agent,
    updates evidence, re-scores risk metrics, and prepares
    investigation reports.
    """

    session_id = request.session_id or "default"
    message = request.message.strip()

    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty."
        )

    # Honest, explicit failure when the server has no LLM key: 503 with
    # an actionable message instead of a provider-side stack trace.
    # (The rest of the API - /health, /api/integrations, Telegram
    # status - keeps working, so an operator can verify integrations
    # before wiring the LLM.)
    if not settings.llm_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "llm_not_configured",
                "message": (
                    "Neither OPENROUTER_API_KEY nor NVIDIA_NIM_API_KEY is "
                    "set on the server, so the AI agents cannot run. Add "
                    "at least one of them to the server-side .env (see "
                    ".env.example) and restart the backend."
                )
            }
        )

    # Per-request LLM override: an explicit provider/model wins for
    # THIS turn only. The dashboard never sends one, so the fixed flow
    # (OpenRouter -> NVIDIA nemotron stage) applies.
    requested_provider = (request.provider or "").strip() or None
    requested_model = (request.model or "").strip() or None

    if requested_provider and not settings.is_known_provider(
        requested_provider
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "unknown_llm_provider",
                "message": (
                    f"Unknown LLM provider '{requested_provider}'. "
                    "Use 'openrouter' or 'nvidia'."
                ),
            },
        )

    llm_provider, llm_model = settings.resolve_provider_model(
        requested_provider, requested_model
    )

    # The agents only get an override when the CALLER really named one.
    # The dashboard never does: it lets the LLM layer run its fixed flow
    # (OpenRouter first -> NVIDIA nemotron stage as fallback), instead of
    # pinning whatever provider happens to be marked active on the
    # server right now.
    agent_provider = requested_provider
    agent_model = requested_model

    logger.info(
        f"Processing message in session '{session_id}': "
        f"{message[:50]}..."
    )

    pipeline_started = time.perf_counter()

    try:

        # --------------------------------------------------
        # 1. Initialize or retrieve active session state
        # --------------------------------------------------
        # First message of a session creates the empty state bundle;
        # later messages reuse it so IOCs, persona and objectives
        # accumulate across turns.

        if session_id not in sessions:

            # FIFO eviction: dicts preserve insertion order, so the
            # first key is the oldest session.
            if len(sessions) >= MAX_SESSIONS:
                oldest_id = next(iter(sessions))
                sessions.pop(oldest_id, None)
                logger.info(
                    "Evicted oldest session '%s' (cap %s).",
                    oldest_id, MAX_SESSIONS,
                )

            sessions[session_id] = {
                "session": ConversationSession(),
                "engine": AdaptiveInvestigationEngine(),
                "investigation": None,
                "report": None,
                "persona_profile": None,
                "timeline": [],
                "turn_count": 0
            }

        state_data = sessions[session_id]

        session = state_data["session"]
        engine = state_data["engine"]
        timeline = state_data["timeline"]

        # --------------------------------------------------
        # 2. Run core investigation agent
        # --------------------------------------------------

        stage_started = time.perf_counter()
        investigation_agent = InvestigationAgent(
            provider=agent_provider, model=agent_model
        )
        investigation_result = investigation_agent.run(message)
        _log_stage(session_id, "investigation", stage_started)

        if state_data["investigation"] is None:

            # -------------------------------------------
            # FIRST TURN of a session:
            # initialize engine, persona profile, timeline
            # -------------------------------------------

            state_data["investigation"] = investigation_result

            # Build the persona state from the threat family, then
            # map it to a concrete named cover identity for the UI.
            engine_state = engine.initialize(
                investigation_result.threat_type
            )

            state_data["persona_profile"] = get_persona_profile(
                investigation_result.threat_type,
                engine_state
            )

            timeline.append({
                "time": get_current_time_str(),
                "text": (
                    f"Scam threat detected: "
                    f"{investigation_result.threat_type}"
                )
            })

            timeline.append({
                "time": get_current_time_str(),
                "text": (
                    f"Created persona: "
                    f"{state_data['persona_profile']['name']}"
                )
            })

        else:

            # -------------------------------------------
            # SUBSEQUENT TURNS:
            # merge newly extracted IOCs into the
            # accumulated investigation, then re-score risk
            # on the full evidence set
            # -------------------------------------------

            existing_inv = state_data["investigation"]

            existing_inv.phone_numbers = sorted(
                list(
                    set(
                        existing_inv.phone_numbers
                        + investigation_result.phone_numbers
                    )
                )
            )

            existing_inv.emails = sorted(
                list(
                    set(
                        existing_inv.emails
                        + investigation_result.emails
                    )
                )
            )

            existing_inv.urls = sorted(
                list(
                    set(
                        existing_inv.urls
                        + investigation_result.urls
                    )
                )
            )

            existing_inv.upi_ids = sorted(
                list(
                    set(
                        existing_inv.upi_ids
                        + investigation_result.upi_ids
                    )
                )
            )

            existing_inv.otp_keywords = sorted(
                list(
                    set(
                        existing_inv.otp_keywords
                        + investigation_result.otp_keywords
                    )
                )
            )

            existing_inv.amounts = sorted(
                list(
                    set(
                        existing_inv.amounts
                        + investigation_result.amounts
                    )
                )
            )

            existing_inv.bank_names = sorted(
                list(
                    set(
                        existing_inv.bank_names
                        + investigation_result.bank_names
                    )
                )
            )

            # --------------------------------------------------
            # Recalculate risk scoring with all accumulated evidence
            # --------------------------------------------------
            # The risk score must reflect everything collected so far
            # (this message + all previous ones in the session).

            entities = {
                "phone_numbers": existing_inv.phone_numbers,
                "emails": existing_inv.emails,
                "urls": existing_inv.urls,
                "upi_ids": existing_inv.upi_ids,
                "otp_keywords": existing_inv.otp_keywords,
                "amounts": existing_inv.amounts,
                "bank_names": existing_inv.bank_names,
            }

            url_analysis = [
                URLChecker.analyze(url)
                for url in existing_inv.urls
            ]

            risk = RiskEngine.calculate(
                {
                    "is_scam": existing_inv.is_scam,
                    "confidence": existing_inv.confidence
                },
                entities,
                url_analysis
            )

            existing_inv.risk_score = risk["risk_score"]
            existing_inv.risk_level = risk["risk_level"]

            existing_inv.detected_indicators = sorted(
                list(
                    set(
                        existing_inv.detected_indicators
                        + risk["reasons"]
                    )
                )
            )

            existing_inv.recommendations = sorted(
                list(
                    set(
                        existing_inv.recommendations
                        + risk["reasons"]
                    )
                )
            )

            investigation_result = existing_inv

            # The objective ladder is advanced only after the
            # ConversationAgent confirms that this inbound message
            # delivered the evidence requested by the active objective.
            engine_state = engine.get_state()

        # --------------------------------------------------
        # 3. Record the scammer message, then generate the
        #    persona's reply via the ConversationAgent
        # --------------------------------------------------

        session.add_scammer_message(message)

        stage_started = time.perf_counter()
        conversation_agent = ConversationAgent(
            provider=agent_provider, model=agent_model
        )
        conversation_result = conversation_agent.run(
            investigation=investigation_result,
            investigation_state=engine_state,
            latest_message=message,
            conversation_history=session.get_history()
        )
        _log_stage(session_id, "conversation", stage_started)

        # Keep the transcript complete for the next turn's prompt.
        session.add_traceai_reply(
            conversation_result.reply
        )

        # Count every processed turn, but advance the evidence ladder
        # only when the model confirms the inbound message supplied the
        # artifact the active objective was waiting for.
        state_data["turn_count"] = (
            state_data.get("turn_count", 0) + 1
        )
        engine_state = engine.update(
            objective_completed=conversation_result.objective_achieved
        )
        engine_state.turn_number = state_data["turn_count"]

        # Keep the UI persona card synchronized with the strategy brain.
        state_data["persona_profile"]["traits"][3]["value"] = (
            engine_state.current_strategy
        )

        state_data["persona_profile"]["traits"][5]["value"] = (
            engine_state.current_objective
        )

        state_data["persona_profile"]["aiTip"] = (
            f"Objective: {engine_state.current_objective}. "
            f"Strategy: {engine_state.current_strategy} "
            f"response style."
        )

        if conversation_result.objective_achieved:

            timeline.append({
                "time": get_current_time_str(),
                "text": (
                    "Objective evidence secured; strategy advanced to: "
                    f"{engine_state.current_strategy}"
                )
            })

        timeline.append({
            "time": get_current_time_str(),
            "text": (
                "Generated reply using objective: "
                f"{engine_state.current_objective}"
            )
        })

        # --------------------------------------------------
        # 4. Archive the case facts into threat memory (JSON)
        # --------------------------------------------------

        stage_started = time.perf_counter()
        MemoryManager().save(
            investigation_result.model_dump()
        )
        _log_stage(session_id, "memory", stage_started)

        # --------------------------------------------------
        # 5. Generate the latest investigation report
        # --------------------------------------------------
        # The report is re-generated each turn, so the stored report
        # normally reflects the newest accumulated evidence. It is a
        # presentation layer, though: a report-provider failure must not
        # discard the investigation verdict and undercover reply that
        # already succeeded.

        report_error = None
        report_agent = None
        report_result = state_data.get("report")

        stage_started = time.perf_counter()

        try:

            report_agent = ReportAgent(
                provider=agent_provider, model=agent_model
            )
            report_result = report_agent.run(
                investigation=investigation_result,
                conversation=conversation_result
            )

            state_data["report"] = report_result

        except Exception as report_exc:

            report_error = str(report_exc)

            logger.warning(
                "Report generation failed for session %s; returning the "
                "investigation and reply without a report: %s",
                session_id,
                report_exc,
            )

        finally:
            _log_stage(session_id, "report", stage_started)

        # --------------------------------------------------
        # 5b. Best-effort export to the connected Google apps
        # --------------------------------------------------
        # Only runs when Drive / Sheets report a genuine, health-verified
        # session; otherwise each app is reported as "skipped". Failures
        # are logged and never break the investigation. The Drive export
        # also skips itself when report_result is None.

        stage_started = time.perf_counter()
        archive_result = EvidenceArchiver().export(
            case_id=session_id,
            investigation=investigation_result,
            report=report_result,
            drive_file_id=state_data.get("drive_file_id"),
        )
        _log_stage(session_id, "archive", stage_started)

        state_data["drive_file_id"] = (
            archive_result["google_drive"].get("file_id")
            or state_data.get("drive_file_id")
        )

        # --------------------------------------------------
        # 6. Construct final output JSON (UI-shaped payload)
        # --------------------------------------------------

        persona_profile = state_data["persona_profile"]

        progress_list = build_progress(
            investigation_result,
            engine_state,
            has_report=state_data.get("report") is not None
        )

        evidence_list = build_evidence(
            investigation_result
        )

        # --------------------------------------------------
        # Map complete session log to chat bubbles
        # --------------------------------------------------
        # Renders every stored turn (scammer + persona) into the
        # message shape the ChatPanel expects. "role" is what the UI
        # keys on: "scammer" = left bubble, "user" = right bubble
        # (the persona's replies are shown as the analyst's agent).
        # A detected URL in a scammer line is surfaced as a link chip.

        formatted_messages = []

        for item in session.history:

            role = item["role"]
            content = item["message"]

            is_scammer = role == "scammer"

            link = None

            if is_scammer:

                extracted = EntityExtractor.extract(
                    content
                )

                if extracted["urls"]:

                    link = {
                        "url": extracted["urls"][0],
                        "label": extracted["urls"][0]
                    }

            formatted_messages.append({
                "role": (
                    "scammer"
                    if is_scammer
                    else "user"
                ),
                "sender": (
                    "Scammer"
                    if is_scammer
                    else f"{persona_profile['name']} (You)"
                ),
                "time": get_current_time_str(),
                "content": content,
                "link": link,
                "status": (
                    "read"
                    if not is_scammer
                    else None
                )
            })

        # --------------------------------------------------
        # 7. Final Response
        # --------------------------------------------------
        # The dashboard consumes this contract directly:
        #
        #   session_id     - id of the undercover session
        #   investigation  - risk gauge, progress steps, evidence
        #                    tracker and reversed activity timeline
        #   persona        - cover identity card payload
        #   conversation   - persona reply + full formatted chat log
        #   report         - markdown incident report
        #
        # See frontend/lib/constants.js (INITIAL_DASHBOARD_DATA) for
        # the empty-state counterpart of these shapes.
        # --------------------------------------------------

        logger.info(
            "Session '%s': /analyze completed in %.1fs.",
            session_id, time.perf_counter() - pipeline_started,
        )

        # Which provider/model actually answered each stage (after any
        # automatic fallbacks) - surfaced so the dashboard can show it.
        conversation_llm = _llm_stage_info(conversation_agent)

        llm_info = {
            "requested_provider": llm_provider,
            "requested_model": llm_model,
            "provider": conversation_llm.get("provider") or llm_provider,
            "model": conversation_llm.get("model") or llm_model,
            "stages": {
                "investigation": _llm_stage_info(investigation_agent),
                "conversation": conversation_llm,
                "report": _llm_stage_info(report_agent),
            },
        }

        return {
            "session_id": session_id,

            "llm": llm_info,

            "investigation": {
                "riskScore": investigation_result.risk_score,
                "riskLevel": (
                    f"{investigation_result.risk_level} RISK"
                ),
                "threatType": investigation_result.threat_type,
                # Friendly severity label derived from the risk score.
                "threatSeverity": (
                    "Critical"
                    if investigation_result.risk_score >= 80
                    else (
                        "High"
                        if investigation_result.risk_score >= 50
                        else "Medium"
                    )
                ),
                "confidenceScore": investigation_result.confidence,
                "progress": progress_list,
                "evidence": evidence_list,
                # Activity feed newest-first for the UI.
                "activity": list(
                    reversed(timeline)
                )
            },

            "persona": persona_profile,

            "conversation": {
                "reply": conversation_result.reply,
                "expected_outcome": (
                    conversation_result.expected_outcome
                ),
                "objective_achieved": (
                    conversation_result.objective_achieved
                ),
                "messages": formatted_messages
            },

            "report": (
                {
                    "title": report_result.title,
                    "markdown": report_result.markdown
                }
                if report_result is not None
                else None
            ),

            # Present but non-fatal when only the optional report call failed.
            "warnings": (
                [
                    {
                        "status": "report_unavailable",
                        "message": (
                            "Investigation and reply succeeded, but the AI "
                            "report could not be generated this turn."
                        )
                    }
                ]
                if report_error
                else []
            ),

            # Honest per-app export outcome for this turn (Drive/Sheets).
            "archive": archive_result
        }

    except HTTPException:
        raise

    except LLMNotConfiguredError as e:

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "llm_not_configured",
                "message": str(e),
            }
        )

    except (RuntimeError, TimeoutError, ConnectionError) as e:

        logger.error("AI provider failure during analysis: %s", e)
        logger.error(traceback.format_exc())

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "status": "ai_provider_failed",
                "message": (
                    "The AI provider did not complete the investigation. "
                    "Check OPENROUTER_API_KEY / NVIDIA_NIM_API_KEY, the "
                    "selected model (GET /api/llm/models), provider "
                    f"status, and server network access. Details: {str(e)}"
                ),
            }
        )

    except ValueError as e:

        logger.error("Invalid AI response during analysis: %s", e)
        logger.error(traceback.format_exc())

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "status": "invalid_ai_response",
                "message": f"The AI provider returned an unusable response: {str(e)}",
            }
        )

    except Exception as e:

        logger.error(
            f"Error executing analysis: {str(e)}"
        )

        logger.error(
            traceback.format_exc()
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "analysis_failed",
                "message": f"Error executing investigation: {str(e)}",
            }
        )


# --------------------------------------------------
# Local Development
# --------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8001
    )
