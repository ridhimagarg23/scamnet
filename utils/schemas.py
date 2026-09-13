"""
schemas.py
==========
Pydantic models (data contracts) shared across TraceAI.

These schemas are the typed backbone of every agent exchange and
give us three guarantees:

1. **Agent outputs are validated** the moment an LLM response is
   converted into a model (wrong types / missing fields fail fast).
2. **Field ranges are enforced** (e.g. ``confidence`` must be 0-100).
3. **Serialisation is free** - ``model_dump()`` turns an object into
   the plain dict that ``MemoryManager`` persists or the API returns.

The three agent result types mirror the three LLM agents:
``InvestigationResult``, ``ConversationResult`` and ``ReportResult``.
"""

from pydantic import BaseModel, Field


class InvestigationResult(BaseModel):
    """
    Structured output of the InvestigationAgent.

    Combines the LLM verdict with the deterministic, regex-extracted
    Indicators of Compromise (IOCs) and the computed risk score.
    Persisted to ``database/threat_memory.json`` after every turn.
    """

    # ---------------- LLM verdict ----------------

    is_scam: bool
    """Whether the LLM judges the message to be a scam."""

    confidence: int = Field(
        ge=0,
        le=100
    )
    """LLM confidence in the verdict, 0-100."""

    threat_type: str
    """Human-readable threat family, e.g. "Banking Phishing"."""

    summary: str
    """One-paragraph explanation of why it is (not) a scam."""

    # ---------- Extracted IOCs (regex-driven) ----------

    phone_numbers: list[str] = []
    """Indian phone numbers found in the message."""

    emails: list[str] = []
    """Email addresses found in the message."""

    urls: list[str] = []
    """URLs / domains found in the message."""

    upi_ids: list[str] = []
    """UPI payment handles (name@bank) found in the message."""

    otp_keywords: list[str] = []
    """Matches for OTP / one-time-password phrasing."""

    amounts: list[str] = []
    """Currency amounts (₹ / Rs / INR) found in the message."""

    bank_names: list[str] = []
    """Known bank / wallet names found in the message."""

    # ---------- Risk (RiskEngine output) ----------

    risk_score: int = Field(
        ge=0,
        le=100
    )
    """Weighted, explainable risk score."""

    risk_level: str
    """Bucket: LOW (< 50), MEDIUM (50-79) or HIGH (>= 80)."""

    # ---------- Explanation / advice ----------

    detected_indicators: list[str] = []
    """Human-readable reasons that raised the risk score."""

    recommendations: list[str] = []
    """Same reasons reused as actionable next-step guidance."""


class ConversationResult(BaseModel):
    """
    Output of the ConversationAgent: the next persona reply plus the
    LLM's own reasoning about the objective it was pursuing.
    """

    reply: str
    """The decoy persona's next chat message to the scammer."""

    objective: str
    """Investigation objective the reply was written to serve."""

    expected_outcome: str
    """What the agent hopes the scammer reveals next."""


class ReportResult(BaseModel):
    """
    Output of the ReportAgent: a full markdown investigation report
    rendered in the dashboard's report modal.
    """

    title: str
    """Report headline, e.g. 'TraceAI Investigation Report'."""

    markdown: str
    """The complete report body in GitHub-flavoured markdown."""
