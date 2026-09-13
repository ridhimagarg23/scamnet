"""
report_agent.py
===============
AI Report Generator (step 5 of the pipeline).

Compiles the accumulated investigation facts plus the honeypot
dialogue into a polished, analyst-ready markdown report with the
standard sections (executive summary, threat type, risk assessment,
IOCs, findings, conversation summary, recommended actions).

The returned ReportResult is stored on the API session and rendered
in the dashboard's report modal (markdown -> HTML on the client).
"""

from llm.llm_client import LLMClient

from tools.prompt_loader import PromptLoader

from utils.schemas import (
    InvestigationResult,
    ConversationResult,
    ReportResult,
)


class ReportAgent:

    def __init__(self):
        """Load the LLM client and the report prompt template."""

        self.llm = LLMClient()

        self.prompt = PromptLoader.load(
            "report_prompt.txt"
        )

    def run(
        self,
        investigation: InvestigationResult,
        conversation: ConversationResult
    ) -> ReportResult:
        """
        Generate the investigation report.

        Parameters
        ----------
        investigation : InvestigationResult
            The accumulated case: verdict, IOCs, risk score/level,
            detected indicators and recommendations.
        conversation : ConversationResult
            The persona reply produced this turn plus its objective /
            expected outcome (quoted in the honeypot summary section).

        Returns
        -------
        ReportResult
            ``title`` + ``markdown`` body.

        Raises
        ------
        ValueError
            If the LLM output is missing any required key.
        """

        # ----------------------------------------------------------
        # Build the prompt: system rules + all facts the report is
        # allowed to mention. The prompt forbids inventing details,
        # so the report is always traceable to real evidence.
        # ----------------------------------------------------------

        final_prompt = f"""
{self.prompt}

=====================================
INVESTIGATION RESULT
=====================================

Threat Type:
{investigation.threat_type}

Risk Score:
{investigation.risk_score}

Risk Level:
{investigation.risk_level}

Summary:
{investigation.summary}

Phone Numbers:
{investigation.phone_numbers}

Emails:
{investigation.emails}

URLs:
{investigation.urls}

UPI IDs:
{investigation.upi_ids}

Detected Indicators:
{investigation.detected_indicators}

Recommendations:
{investigation.recommendations}

=====================================
HONEYPOT RESPONSE
=====================================

Reply:
{conversation.reply}

Objective:
{conversation.objective}

Expected Outcome:
{conversation.expected_outcome}

=====================================
Return ONLY valid JSON.
=====================================
"""

        result = self.llm.generate(
            final_prompt,
            json_output=True
        )

        # ----------------------------
        # Validate LLM Response
        # ----------------------------

        required_keys = [
            "title",
            "markdown"
        ]

        for key in required_keys:

            if key not in result:

                raise ValueError(
                    f"Missing '{key}' in LLM response."
                )

        return ReportResult(
            **result
        )
