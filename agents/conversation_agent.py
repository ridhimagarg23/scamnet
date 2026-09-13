"""
conversation_agent.py
=====================
Adaptive Conversation Agent (step 4 of the pipeline).

This agent writes the *persona's next chat reply* to the scammer.
It is deliberately narrow: the strategy brain (AdaptiveInvestigation
Engine) has already decided WHO the persona is, WHAT objective to
pursue and WHICH behavioural style to use - this agent only has to
sound like a believable human pursuing that objective.

Two prompt channels are supported:

* ``run()``          - the dashboard channel, driven by
                       ``prompts/conversation_prompt.txt`` (used by
                       POST /analyze).
* ``run_telegram()`` - the live Telegram channel, driven by
                       ``prompts/telegram_assistant_prompt.txt``.
                       Identical plumbing, but the system rules are
                       chat-specific (Telegram register, live-chat
                       safety override) and the prompt additionally
                       carries the Telegram channel context.

Safety rules live in the prompts: never reveal personal/financial
data, never admit being an AI or an investigation, keep replies short
(< 35 words) and reply in the scammer's language.
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


# Filename of the Telegram-specific system rules (loaded lazily so the
# dashboard path stays usable even if the Telegram prompt is absent).
TELEGRAM_PROMPT_FILE = "telegram_assistant_prompt.txt"


class ConversationAgent:

    def __init__(self):
        """Load the LLM client and the conversation prompt template.

        The Telegram prompt is NOT read here: it is loaded on first use
        by ``_get_telegram_prompt()`` so that importing/constructing the
        agent for the dashboard channel never depends on a file that
        only the Telegram worker needs.
        """

        self.llm = LLMClient()

        self.prompt = PromptLoader.load(
            "conversation_prompt.txt"
        )

        # Lazily-populated cache for the Telegram system rules.
        self._telegram_prompt = None

    # ----------------------------------------------------------
    # Prompt handling
    # ----------------------------------------------------------

    def _get_telegram_prompt(self) -> str:
        """
        Return the Telegram system rules (cached after the first read).

        Returns
        -------
        str
            Contents of ``prompts/telegram_assistant_prompt.txt``.

        Raises
        ------
        FileNotFoundError
            If the Telegram prompt file is missing.
        """

        if self._telegram_prompt is None:

            self._telegram_prompt = PromptLoader.load(
                TELEGRAM_PROMPT_FILE
            )

        return self._telegram_prompt

    def _build_prompt(
        self,
        prompt: str,
        investigation: InvestigationResult,
        investigation_state: InvestigationState,
        latest_message: str,
        conversation_history: str = "",
        channel_context: str = "",
    ) -> str:
        """
        Assemble the full LLM prompt: system rules + every piece of
        context the reply must stay consistent with.

        Parameters
        ----------
        prompt : str
            The channel's system rules (dashboard or Telegram).
        investigation : InvestigationResult
            Accumulated case facts.
        investigation_state : InvestigationState
            Active profile + current objective + current strategy.
        latest_message : str
            The scammer message this reply answers.
        conversation_history : str
            Plain-text transcript of all prior turns.
        channel_context : str
            Optional extra block (Telegram chat id / sender handle)
            inserted before the closing instruction.

        Returns
        -------
        str
            The finished prompt.
        """

        return f"""
{prompt}

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
{channel_context}
==================================================
IMPORTANT
==================================================

Return ONLY valid JSON.

"""

    def _run_prompt(self, final_prompt: str) -> ConversationResult:
        """
        Execute one LLM call and validate the JSON contract.

        Parameters
        ----------
        final_prompt : str
            Fully assembled prompt.

        Returns
        -------
        ConversationResult

        Raises
        ------
        ValueError
            If the LLM output is missing any required key.
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

    # ----------------------------------------------------------
    # Public entry points
    # ----------------------------------------------------------

    def run(
        self,
        investigation: InvestigationResult,
        investigation_state: InvestigationState,
        latest_message: str,
        conversation_history: str = "",
    ) -> ConversationResult:
        """
        Generate the persona's next reply for one scammer message
        (dashboard channel).

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

        final_prompt = self._build_prompt(
            prompt=self.prompt,
            investigation=investigation,
            investigation_state=investigation_state,
            latest_message=latest_message,
            conversation_history=conversation_history,
        )

        return self._run_prompt(
            final_prompt
        )

    def run_telegram(
        self,
        investigation: InvestigationResult,
        investigation_state: InvestigationState,
        latest_message: str,
        conversation_history: str = "",
        chat_id: int | None = None,
        sender_username: str | None = None,
    ) -> ConversationResult:
        """
        Generate the persona's next reply for a LIVE Telegram chat.

        Same pipeline as ``run()`` but driven by
        ``prompts/telegram_assistant_prompt.txt``, and the prompt
        carries the Telegram channel context so the model knows it is
        writing a chat message (not a dashboard artefact).

        Parameters
        ----------
        investigation : InvestigationResult
            Accumulated case facts for this chat.
        investigation_state : InvestigationState
            Active profile + current objective + current strategy.
        latest_message : str
            The inbound Telegram message text this reply answers.
        conversation_history : str
            Plain-text transcript of the chat so far.
        chat_id : int | None
            Telegram chat id (context only, echoed into the prompt).
        sender_username : str | None
            Public @handle of the sender when Telegram supplied one.

        Returns
        -------
        ConversationResult

        Raises
        ------
        FileNotFoundError
            If the Telegram prompt file is missing.
        ValueError
            If the LLM output is missing any required key.
        """

        channel_context = f"""

==================================================
TELEGRAM CHANNEL CONTEXT
==================================================

Channel:
Telegram

Chat ID:
{chat_id if chat_id is not None else "unknown"}

Sender Username:
{sender_username if sender_username else "unknown"}
"""

        final_prompt = self._build_prompt(
            prompt=self._get_telegram_prompt(),
            investigation=investigation,
            investigation_state=investigation_state,
            latest_message=latest_message,
            conversation_history=conversation_history,
            channel_context=channel_context,
        )

        return self._run_prompt(
            final_prompt
        )
