"""
risk_engine.py
==============
Weighted, explainable risk-scoring engine.

The engine converts evidence about a message into a single 0-100
score and a risk bucket (LOW / MEDIUM / HIGH). Every point added is
recorded as a human-readable *reason*, which makes scores auditable:
an analyst can always see exactly why a case reached 82/100.

Scoring rubric
--------------
+-----------------------------------------------------+-------+
| Signal                                              | Pts   |
+-----------------------------------------------------+-------+
| LLM classified the message as a scam                | +40   |
| Confidence x 0.30 (rounded)                         | 0-30  |
| Suspicious URL present                              | +10   |
| UPI ID present                                      | +10   |
| Email address present                               | +5    |
| Phone number present                                | +5    |
| URL uses HTTP instead of HTTPS                      | +10   |
| URL is a known shortener (bit.ly, t.co ...)         | +10   |
| URL has >= 2 subdomains                             | +10   |
| >= 3 indicator families present (multi-IOC bonus)   | +5    |
+-----------------------------------------------------+-------+

Risk buckets: HIGH >= 80 | MEDIUM >= 50 | LOW < 50

Example: a banking-phishing SMS with a fake URL, an embedded phone
number and 92% confidence scores 40 + 27 + 10 + 5 = 82 -> HIGH.
"""

from typing import Dict


class RiskEngine:
    """
    Rule-based explainable risk scoring (no ML involved).
    """

    @classmethod
    def calculate(
        cls,
        investigation: Dict,
        entities: Dict,
        url_analysis: list[Dict]
    ) -> Dict:
        """
        Compute the composite risk score for an investigation.

        Parameters
        ----------
        investigation : dict
            LLM verdict fields: ``is_scam`` (bool) and
            ``confidence`` (int 0-100).
        entities : dict
            IOC lists from the EntityExtractor: ``urls``, ``upi_ids``,
            ``emails``, ``phone_numbers`` (each a list of strings).
        url_analysis : list[dict]
            One URLChecker.analyze() result per detected URL.

        Returns
        -------
        dict
            ``{"risk_score": int, "risk_level": "LOW|MEDIUM|HIGH",
            "reasons": [str, ...]}``

        Example
        -------
        .. code-block:: python

            # 40 (scam) + 24 (80% x 0.30) + 10 (URL) + 10 (plain HTTP) = 84
            RiskEngine.calculate(
                {"is_scam": True, "confidence": 80},
                {"urls": ["http://evil.co"], "upi_ids": [], "emails": [],
                 "phone_numbers": []},
                [{"https": False, "shortened": False, "subdomain_count": 0}])
            # -> {'risk_score': 84, 'risk_level': 'HIGH', 'reasons': [...]}
        """

        score = 0
        reasons = []

        # -------------------------
        # 1. Scam detection (LLM verdict)
        # -------------------------
        # The single heaviest signal: the model already read the
        # message and judged intent, not just surface artifacts.

        if investigation["is_scam"]:
            score += 40
            reasons.append("LLM detected a scam.")

        # -------------------------
        # 2. Confidence scaling
        # -------------------------
        # A confident verdict deserves a proportionally higher score
        # (up to +30 points at 100% confidence).

        confidence = investigation["confidence"]

        score += int(confidence * 0.30)

        # -------------------------
        # 3. Indicator families
        # -------------------------
        # Each concrete IOC family contributes fixed points. These are
        # deliberately smaller than the LLM verdict - presence of an
        # artifact is suggestive, not conclusive.

        if entities["urls"]:
            score += 10
            reasons.append("Suspicious URL detected.")

        if entities["upi_ids"]:
            score += 10
            reasons.append("UPI ID detected.")

        if entities["emails"]:
            score += 5
            reasons.append("Email detected.")

        if entities["phone_numbers"]:
            score += 5
            reasons.append("Phone number detected.")

        # -------------------------
        # 4. URL hygiene analysis
        # -------------------------
        # Structural red flags of phishing infrastructure:
        # cleartext HTTP, link shorteners, and deep subdomain nesting.

        for url in url_analysis:

            if not url["https"]:
                score += 10
                reasons.append("Uses HTTP instead of HTTPS.")

            if url["shortened"]:
                score += 10
                reasons.append("Shortened URL detected.")

            if url["subdomain_count"] >= 2:
                score += 10
                reasons.append("Suspicious subdomain.")

        # -------------------------
        # 5. Multi-IOC bonus
        # -------------------------
        # A message combining many artifact types (link + phone + UPI)
        # is far more likely to be a well-built lure.

        indicators = (
            len(entities["urls"])
            + len(entities["emails"])
            + len(entities["phone_numbers"])
            + len(entities["upi_ids"])
        )

        if indicators >= 3:
            score += 5
            reasons.append("Multiple indicators detected.")

        # -------------------------
        # 6. Clamp + bucket
        # -------------------------

        score = min(score, 100)

        if score >= 80:
            level = "HIGH"

        elif score >= 50:
            level = "MEDIUM"

        else:
            level = "LOW"

        return {
            "risk_score": score,
            "risk_level": level,
            "reasons": reasons
        }
