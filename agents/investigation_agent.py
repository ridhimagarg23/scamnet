"""
investigation_agent.py
======================
Main AI Investigation Agent (step 1 of the pipeline).

Pipeline executed inside ``run()``:
    1. Extract IOCs from the raw message (regex, deterministic).
    2. Run structural URL analysis on every detected URL.
    3. Ask the LLM for a verdict (is_scam / confidence / threat_type /
       summary), giving it the extracted entities as evidence.
    4. Validate the LLM output (required keys, sane confidence).
    5. Compute the composite risk score via the RiskEngine.
    6. Package everything into a validated InvestigationResult.

The InvestigationAgent runs on EVERY scammer message; the API then
merges new IOCs into the session's accumulated investigation.
"""

from llm.llm_client import LLMClient

from tools.prompt_loader import PromptLoader
from tools.entity_extractor import EntityExtractor
from tools.url_checker import URLChecker
from tools.risk_engine import RiskEngine

from utils.schemas import InvestigationResult


class InvestigationAgent:

    def __init__(self):
        """
        Prepare the LLM client and load the investigation prompt.

        The prompt (``prompts/investigation_prompt.txt``) encodes the
        agent's system behaviour: detect phishing/fraud/impersonation
        tactics and answer with strict JSON only.
        """

        self.llm = LLMClient()

        self.prompt = PromptLoader.load(
            "investigation_prompt.txt"
        )

    def run(
        self,
        message: str
    ) -> InvestigationResult:
        """
        Investigate one suspicious message.

        Parameters
        ----------
        message : str
            Raw scammer payload pasted by the analyst.

        Returns
        -------
        InvestigationResult
            Verdict + IOCs + risk score + recommendations.

        Raises
        ------
        ValueError
            If the LLM output violates the JSON contract or contains
            logically impossible confidence values.
        """

        print("\n[1/5] Extracting entities...")

        # ----------------------------------------------------------
        # 1. Deterministic IOC extraction (never hallucinated -
        #    regexes only, so the LLM cannot invent evidence).
        # ----------------------------------------------------------

        entities = EntityExtractor.extract(
            message
        )

        print("[2/5] Analyzing URLs...")

        # ----------------------------------------------------------
        # 2. Structural URL analysis (HTTPS? shortener? subdomains?)
        # ----------------------------------------------------------

        url_analysis = [

            URLChecker.analyze(url)

            for url in entities["urls"]

        ]

        print("[3/5] Investigating with AI...")

        # ----------------------------------------------------------
        # 3. Build the LLM prompt = system rules (from file) +
        #    the suspicious message + extracted entities as evidence.
        #    The model returns ONLY the JSON verdict.
        # ----------------------------------------------------------

        final_prompt = f"""
{self.prompt}

=========================
SUSPICIOUS MESSAGE
=========================

{message}

=========================
EXTRACTED ENTITIES
=========================

Phone Numbers:
{entities["phone_numbers"]}

Emails:
{entities["emails"]}

URLs:
{entities["urls"]}

UPI IDs:
{entities["upi_ids"]}

Banks:
{entities["bank_names"]}

OTP Keywords:
{entities["otp_keywords"]}

Amounts:
{entities["amounts"]}

=========================
Return ONLY valid JSON.
=========================
"""

        result = self.llm.generate(
            final_prompt,
            json_output=True
        )

        # ----------------------------
        # 4. Validate LLM Response
        # ----------------------------
        # Guard against malformed / lazy model output before any
        # downstream code touches the dict.

        required_keys = [

            "is_scam",

            "confidence",

            "threat_type",

            "summary"

        ]

        for key in required_keys:

            if key not in result:

                raise ValueError(
                    f"Missing '{key}' in LLM response."
                )

        confidence = result["confidence"]

        # The model must not contradict itself: calling something a
        # scam at <30% confidence (or "safe" at >90%) means the output
        # is unreliable -> treat it as an error rather than a verdict.
        if result["is_scam"] and confidence < 30:

            raise ValueError(
                "Invalid AI output. Scam detected with very low confidence."
            )

        if (not result["is_scam"]) and confidence > 90:

            raise ValueError(
                "Invalid AI output. Not scam with unusually high confidence."
            )

        print("[4/5] Calculating Risk...")

        # ----------------------------------------------------------
        # 5. Risk scoring: combine LLM verdict + extracted entities +
        #    URL analysis into one explainable 0-100 score.
        # ----------------------------------------------------------

        risk = RiskEngine.calculate(

            result,

            entities,

            url_analysis

        )

        # ----------------------------------------------------------
        # 6. Assemble the final result object:
        #    LLM verdict + extracted IOCs + risk metadata.
        # ----------------------------------------------------------

        result.update({

            "phone_numbers":
                entities["phone_numbers"],

            "emails":
                entities["emails"],

            "urls":
                entities["urls"],

            "upi_ids":
                entities["upi_ids"],

            "otp_keywords":
                entities["otp_keywords"],

            "amounts":
                entities["amounts"],

            "bank_names":
                entities["bank_names"],

            "risk_score":
                risk["risk_score"],

            "risk_level":
                risk["risk_level"],

            "recommendations":
                risk["reasons"],

            "detected_indicators":
                risk["reasons"]

        })

        print("[5/5] Investigation Complete.\n")

        # Pydantic validates types + ranges one last time here.
        return InvestigationResult(
            **result
        )
