"""
app.py
======
TraceAI CLI Application

Runs a single-pass investigation from the terminal - handy for quick
manual testing of a message without starting the API server:

    export OPENROUTER_API_KEY=...
    python app.py

Flow: investigate the pasted message -> build the persona state ->
save to memory -> generate the honeypot reply -> print a report.
"""

from agents.investigation_agent import InvestigationAgent
from agents.conversation_agent import ConversationAgent
from agents.report_agent import ReportAgent

from tools.memory_manager import MemoryManager
from tools.adaptive_investigation_engine import (
    AdaptiveInvestigationEngine,
)
from tools.conversation_session import (
    ConversationSession,
)


def main():

    print("=" * 60)
    print(" TraceAI - AI Scam Investigation Platform ")
    print("=" * 60)

    # -------------------------------------------------
    # 1. Ask the analyst for the raw scammer payload
    # -------------------------------------------------

    message = input(
        "\nPaste suspicious message:\n\n"
    )

    # ---------------------------------
    # 2. Investigation: verdict + IOCs + risk score
    # ---------------------------------

    investigation = InvestigationAgent().run(
        message
    )

    # ---------------------------------
    # 3. Adaptive Investigation Engine:
    #    pick persona profile + first objective/strategy
    # ---------------------------------

    engine = AdaptiveInvestigationEngine()

    state = engine.initialize(
        investigation.threat_type
    )

    # ---------------------------------
    # 4. Conversation Session: record the scammer message
    # ---------------------------------

    session = ConversationSession()

    session.add_scammer_message(
        message
    )

    # ---------------------------------
    # 5. Persist the investigation into threat memory
    # ---------------------------------

    memory = MemoryManager()

    memory.save(
        investigation.model_dump()
    )

    # ---------------------------------
    # 6. Conversation: generate the persona's reply
    # ---------------------------------

    conversation = ConversationAgent().run(
        investigation=investigation,
        investigation_state=state,
        latest_message=message,
        conversation_history=session.get_history()
    )

    session.add_traceai_reply(
        conversation.reply
    )

    # ---------------------------------
    # 7. Report: compile the markdown investigation report
    # ---------------------------------

    report = ReportAgent().run(
        investigation=investigation,
        conversation=conversation
    )

    # ---------------------------------
    # 8. Print the complete output
    # ---------------------------------

    print("\n" + "=" * 60)
    print(" INVESTIGATION RESULT ")
    print("=" * 60)

    print(f"\nThreat Type : {investigation.threat_type}")
    print(f"Confidence  : {investigation.confidence}%")
    print(f"Risk Score  : {investigation.risk_score}")
    print(f"Risk Level  : {investigation.risk_level}")

    print("\nSummary:")
    print(investigation.summary)

    print("\n" + "=" * 60)
    print(" INVESTIGATION PROFILE ")
    print("=" * 60)

    print(f"Language             : {state.profile.language}")
    print(f"Communication Style  : {state.profile.communication_style}")
    print(f"Digital Literacy     : {state.profile.digital_literacy}")

    print("\nCurrent Objective:")
    print(state.current_objective)

    print("\nCurrent Strategy:")
    print(state.current_strategy)

    print("\nSuggested Safe Reply:")
    print(conversation.reply)

    print("\nExpected Outcome:")
    print(conversation.expected_outcome)

    print("\n" + "=" * 60)
    print(report.title)
    print("=" * 60)

    print(report.markdown)


if __name__ == "__main__":
    main()
