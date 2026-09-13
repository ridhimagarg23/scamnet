"""
api.py
======
TraceAI Production FastAPI Backend

This is the HTTP surface of the whole platform. The Next.js dashboard
talks to three endpoints:

    POST /analyze   - feed one scammer message into the pipeline
    POST /new       - reset a session (start a fresh case)
    GET  /health    - liveness probe for hosting platforms

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
import traceback
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, status, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.investigation_agent import InvestigationAgent
from agents.conversation_agent import ConversationAgent
from agents.report_agent import ReportAgent

from tools.adaptive_investigation_engine import AdaptiveInvestigationEngine
from tools.conversation_session import ConversationSession
from tools.memory_manager import MemoryManager
from tools.entity_extractor import EntityExtractor
from tools.url_checker import URLChecker
from tools.risk_engine import RiskEngine


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
# Enable CORS for frontend integration
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://trace-ai-phi.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)





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
#   }
#
# NOTE: in-memory only - state is lost on process restart. Scale-out
# would require swapping this dict for Redis or similar.
sessions: Dict[str, dict] = {}


class InvestigationRequest(BaseModel):
    """Request body for POST /analyze."""

    message: str
    """The scammer message to investigate (non-empty)."""

    session_id: Optional[str] = "default"
    """Identifies the undercover conversation (state continuity)."""


# --------------------------------------------------
# Helper Functions
# --------------------------------------------------

def get_current_time_str() -> str:
    return datetime.datetime.now().strftime("%I:%M %p")


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


def build_progress(investigation, state) -> list:
    """
    Generates the 5-step investigation progress list for the UI.

    Each step is one of ``done`` / ``current`` / ``locked`` based on
    the accumulated investigation and the engine's turn counter:

        1. Threat Detected       - LLM flagged the message as a scam
        2. IOC Extracted         - at least one IOC family captured
        3. Undercover Engagement - dialogue has started (turn > 1)
        4. Evidence Secured      - IOCs exist AND dialogue is ongoing
        5. Report Ready          - report has been generated

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
            "state": "current" if state.turn_number > 1 else "locked"
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
    return {
        "status": "healthy"
    }

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

    logger.info(
        f"Processing message in session '{session_id}': "
        f"{message[:50]}..."
    )

    try:

        # --------------------------------------------------
        # 1. Initialize or retrieve active session state
        # --------------------------------------------------
        # First message of a session creates the empty state bundle;
        # later messages reuse it so IOCs, persona and objectives
        # accumulate across turns.

        if session_id not in sessions:
            sessions[session_id] = {
                "session": ConversationSession(),
                "engine": AdaptiveInvestigationEngine(),
                "investigation": None,
                "report": None,
                "persona_profile": None,
                "timeline": []
            }

        state_data = sessions[session_id]

        session = state_data["session"]
        engine = state_data["engine"]
        timeline = state_data["timeline"]

        # --------------------------------------------------
        # 2. Run core investigation agent
        # --------------------------------------------------

        investigation_result = InvestigationAgent().run(message)

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

            # --------------------------------------------------
            # Advance engine objective/strategy state
            # --------------------------------------------------

            engine_state = engine.update(
                objective_completed=True
            )

            # --------------------------------------------------
            # Synchronize modified engine state inside persona
            # --------------------------------------------------

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

            timeline.append({
                "time": get_current_time_str(),
                "text": (
                    "Scammer response analyzed. Strategy advanced to: "
                    f"{engine_state.current_strategy}"
                )
            })

        # --------------------------------------------------
        # 3. Record the scammer message, then generate the
        #    persona's reply via the ConversationAgent
        # --------------------------------------------------

        session.add_scammer_message(message)

        conversation_result = ConversationAgent().run(
            investigation=investigation_result,
            investigation_state=engine_state,
            latest_message=message,
            conversation_history=session.get_history()
        )

        # Keep the transcript complete for the next turn's prompt.
        session.add_traceai_reply(
            conversation_result.reply
        )

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

        MemoryManager().save(
            investigation_result.model_dump()
        )

        # --------------------------------------------------
        # 5. Generate the latest investigation report
        # --------------------------------------------------
        # The report is re-generated each turn, so the stored report
        # always reflects the newest accumulated evidence.

        report_result = ReportAgent().run(
            investigation=investigation_result,
            conversation=conversation_result
        )

        state_data["report"] = report_result

        # --------------------------------------------------
        # 6. Construct final output JSON (UI-shaped payload)
        # --------------------------------------------------

        persona_profile = state_data["persona_profile"]

        progress_list = build_progress(
            investigation_result,
            engine_state
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

        return {
            "session_id": session_id,

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
                "messages": formatted_messages
            },

            "report": {
                "title": report_result.title,
                "markdown": report_result.markdown
            }
        }

    except Exception as e:

        logger.error(
            f"Error executing analysis: {str(e)}"
        )

        logger.error(
            traceback.format_exc()
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error executing investigation: {str(e)}"
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
