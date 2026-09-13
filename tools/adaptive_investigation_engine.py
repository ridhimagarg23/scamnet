"""
adaptive_investigation_engine.py
================================
Adaptive Investigation Engine - the "strategy brain" of TraceAI.

The engine owns everything that makes the undercover persona feel
*alive* across a multi-turn dialogue:

1. **Profile selection** - picks a believable victim archetype
   (communication style, language, digital literacy) matching the
   detected threat context (banking scam vs job scam vs investment).
2. **Objective ladder** - plans a progression of safe investigation
   goals (collect the verification URL -> collect employee ID ->
   collect payment method -> ... waste the scammer's time).
3. **Strategy mapping** - translates each objective into a persona
   behaviour ("Curious", "Confused", "Cooperative", ...) that the
   ConversationAgent must adopt while replying.

The engine is deliberately rule-based and deterministic (no LLM),
so an entire investigation can be simulated in unit tests.
"""

from dataclasses import dataclass, field


@dataclass
class InvestigationProfile:
    """
    The persona's communication fingerprint.

    Attributes
    ----------
    communication_style : str
        How the persona talks (e.g. Polite, Curious, Formal).
    language : str
        Language / register used (English or Hinglish for bank scams).
    digital_literacy : str
        Technical confidence of the persona (Low / Medium / High) -
        Low-literacy personas are more believable OTP-fraud victims.
    """

    communication_style: str
    language: str
    digital_literacy: str


@dataclass
class InvestigationState:
    """
    Mutable state of one active investigation session.

    The API keeps this object alive inside the session dictionary and
    advances it with ``engine.update()`` after every scammer message.

    Attributes
    ----------
    profile : InvestigationProfile
        The active victim persona fingerprint.
    current_objective : str
        What the persona is trying to extract right now.
    current_strategy : str
        Behavioural style the persona must use this turn.
    completed_objectives : list[str]
        History of achieved objectives (drives the ladder forward).
    turn_number : int
        1-based count of processed scammer messages.
    """

    profile: InvestigationProfile
    current_objective: str
    current_strategy: str
    completed_objectives: list[str] = field(default_factory=list)
    turn_number: int = 1


class AdaptiveInvestigationEngine:

    def __init__(self):

        self.state = None

    # ------------------------------------
    # Public lifecycle API
    # ------------------------------------

    def initialize(
        self,
        threat_type: str
    ) -> InvestigationState:
        """
        Bootstrap a fresh investigation from the first threat verdict.

        Called on turn 1 of every session. Chooses the persona profile
        and the first objective appropriate for the threat family.

        Parameters
        ----------
        threat_type : str
            LLM threat classification, e.g. "Banking Phishing".

        Returns
        -------
        InvestigationState
            The freshly created state (also stored on the engine).
        """

        profile = self._select_profile(threat_type)

        objective = self._first_objective(threat_type)

        strategy = self._choose_strategy(objective)

        self.state = InvestigationState(
            profile=profile,
            current_objective=objective,
            current_strategy=strategy
        )

        return self.state

    def update(
        self,
        objective_completed: bool
    ) -> InvestigationState:
        """
        Advance the investigation after one scammer turn.

        When the current objective is deemed complete (the analyst
        confirms the info was extracted), the next objective on the
        ladder becomes active and the persona's strategy is re-picked
        to match it.

        Parameters
        ----------
        objective_completed : bool
            Whether evidence for the current objective was gathered.

        Returns
        -------
        InvestigationState
            The updated state (same object as ``self.state``).
        """

        if objective_completed:

            # Archive the finished objective so the ladder never
            # proposes the same goal twice in one session.
            self.state.completed_objectives.append(
                self.state.current_objective
            )

            self.state.current_objective = self._next_objective()

        # Re-derive the strategy every turn - the persona's behaviour
        # should track whatever objective is active right now.
        self.state.current_strategy = self._choose_strategy(
            self.state.current_objective
        )

        self.state.turn_number += 1

        return self.state

    def get_state(self):
        """Return the current InvestigationState (None before init)."""

        return self.state

    # ====================================
    # PRIVATE METHODS (selection rules)
    # ====================================

    def _select_profile(
        self,
        threat_type: str
    ) -> InvestigationProfile:
        """
        Match a persona archetype to the threat family.

        Mapping rationale:
        * Banking scams target ordinary account holders -> a polite,
          Hinglish-speaking user with medium digital literacy.
        * Job scams target job seekers -> a curious recent graduate.
        * Investment scams need a wealthy mark -> a formal, financially
          literate profile (retired bank manager type).
        * Unknown threats -> a neutral, low-literacy default that is
          easy for scammers to underestimate.
        """

        threat = threat_type.lower()

        if "bank" in threat:

            return InvestigationProfile(
                communication_style="Polite",
                language="Hinglish",
                digital_literacy="Medium"
            )

        if "job" in threat:

            return InvestigationProfile(
                communication_style="Curious",
                language="English",
                digital_literacy="Medium"
            )

        if "investment" in threat:

            return InvestigationProfile(
                communication_style="Formal",
                language="English",
                digital_literacy="High"
            )

        return InvestigationProfile(
            communication_style="Neutral",
            language="English",
            digital_literacy="Low"
        )

    # ------------------------------------

    def _first_objective(
        self,
        threat_type: str
    ) -> str:
        """
        Choose a sensible *first* goal per threat family.

        The persona should start by collecting the least risky,
        most diagnostic artefact for that scam category.
        """

        threat = threat_type.lower()

        if "bank" in threat:
            # Banking scams revolve around a fake verification portal.
            return "Collect official verification website"

        if "job" in threat:
            # Job scams revolve around a fake recruiter / agency.
            return "Collect recruiter information"

        if "investment" in threat:
            # Investment scams push a fake brokerage/company.
            return "Collect company information"

        # Generic fallback: keep the scammer talking.
        return "Collect more information"

    # ------------------------------------

    def _next_objective(self) -> str:
        """
        Return the next uncompleted objective on the standard ladder.

        Objectives are ordered to maximise intel yield while keeping
        the persona safe:
        websites -> employee ID -> payment method -> keep them chatting
        -> waste their time -> terminate the operation.
        """

        objectives = [

            "Collect official verification website",

            "Collect employee ID",

            "Collect payment method",

            "Keep conversation on chat",

            "Waste scammer time",

            "End investigation"

        ]

        # First objective in the ladder that has not been completed yet.
        for objective in objectives:

            if objective not in self.state.completed_objectives:

                return objective

        # Ladder exhausted - never loop, always end.
        return "End investigation"

    # ------------------------------------

    def _choose_strategy(
        self,
        objective: str
    ) -> str:
        """
        Map an objective to the persona behaviour most likely to
        advance it:

        * Need the verification site?  Act *Curious*.
        * Need an employee ID?         Act *Confused* (scammers explain
                                       more when they feel in control).
        * Need payment details?        Act *Cooperative*.
        * Just stalling?               Act *Busy* / *Skeptical*.
        * Wrapping up?                 Act *Cautious*.
        """

        mapping = {

            "Collect official verification website":
                "Curious",

            "Collect employee ID":
                "Confused",

            "Collect payment method":
                "Cooperative",

            "Keep conversation on chat":
                "Busy",

            "Waste scammer time":
                "Skeptical",

            "End investigation":
                "Cautious"

        }

        return mapping.get(
            objective,
            "Curious"
        )
