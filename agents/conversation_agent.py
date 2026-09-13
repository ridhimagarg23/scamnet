"""
conversation_agent.py
=====================
Adaptive Conversation Agent (step 4 of the pipeline).

This agent writes the *persona's next chat reply* to the scammer.
It is deliberately narrow: the strategy brain (AdaptiveInvestigation
Engine) has already decided WHO the persona is, WHAT objective to
pursue and WHICH behavioural style to use - this agent only has to
sound like a believable human pursuing that objective.

Safety rules live in the prompt (``prompts/conversation_prompt.txt``):
never reveal personal/financial data, never admit being an AI or an
investigation, keep replies short (< 35 words) and reply in the
scammer's language.
"""

from llm.llm_client import LLMClient

from tools.prompt_loader import PromptLoader
from tools.adaptive_investigation_engine import (
    InvestigationState,
)

from utils.schemas import (
    InvestigationResult,
    ConversationResult,
)


class ConversationAgent:

    def __init__(self):
        """Load the LLM client and the conversation prompt template."""

        self.llm = LLMClient()

        self.prompt = PromptLoader.load(
            "conversation_prompt.txt"
        )

    def run(
        self,
        investigation: InvestigationResult,
        investigation_state: InvestigationState,
        latest_message: str,
        conversation_history: str = "",
    ) -> ConversationResult:
        """
        Generate the persona's next reply for one scammer message.

        Parameters
        ----------
        investigation : InvestigationResult
            Current (accumulated) case facts - threat type, risk,
            detected IOCs - so the persona reacts consistently to
            what the scammer has already sent.
        investigation_state : InvestigationState
            Active profile + current objective + current strategy.
        latest_message : str
            The scammer message this reply answers.
        conversation_history : str
            Plain-text transcript of all prior turns (or
            "No previous conversation." on the first turn).

        Returns
        -------
        ConversationResult
            ``reply`` (the persona text), ``objective`` (which goal it
            served) and ``expected_outcome`` (what the agent hopes the
            scammer reveals next).

        Raises
        ------
        ValueError
            If the LLM output is missing any required key.
        """

        # ----------------------------------------------------------
        # Build the prompt: system rules + every piece of context the
        # reply must stay consistent with.
        # ----------------------------------------------------------

        final_prompt = f"""
{self.prompt}

==================================================
INVESTIGATION RESULT
==================================================

Threat Type:
{investigation.threat_type}

Risk Score:
{investigation.risk_score}

Risk Level:
{investigation.risk_level}

Summary:
{investigation.summary}

Detected URLs:
{investigation.urls}

Detected Phone Numbers:
{investigation.phone_numbers}

Detected Emails:
{investigation.emails}

Detected UPI IDs:
{investigation.upi_ids}

Detected Indicators:
{investigation.detected_indicators}

Recommendations:
{investigation.recommendations}

==================================================
INVESTIGATION PROFILE
==================================================

Communication Style:
{investigation_state.profile.communication_style}

Language:
{investigation_state.profile.language}

Digital Literacy:
{investigation_state.profile.digital_literacy}

==================================================
CURRENT OBJECTIVE
==================================================

{investigation_state.current_objective}

==================================================
CURRENT STRATEGY
==================================================

{investigation_state.current_strategy}

==================================================
CONVERSATION HISTORY
==================================================

{conversation_history}

==================================================
LATEST SCAMMER MESSAGE
==================================================

{latest_message}

==================================================
IMPORTANT
==================================================

Return ONLY valid JSON.

"""

        result = self.llm.generate(
            final_prompt,
            json_output=True
        )

        # ----------------------------
        # Validate LLM Response
        # ----------------------------

        required_keys = [
            "reply",
            "objective",
            "expected_outcome",
        ]

        for key in required_keys:

            if key not in result:

                raise ValueError(
                    f"Missing '{key}' in LLM response."
                )

        return ConversationResult(
            **result
        )
