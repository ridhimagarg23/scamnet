"""
entity_extractor.py

Deterministic extraction of Indicators of Compromise (IOCs)
using regular expressions (NOT the LLM).

Design rationale
----------------
* Regex extraction is fast, free and reproducible - it never costs a
  token and never hallucinates.
* The extracted candidates are handed to the LLM as supporting
  evidence, which keeps model verdicts anchored to real artifacts
  actually present in the message.

Regional focus
--------------
The extractor is tuned for the Indian threat landscape: it recognises
+91 / 10-digit mobile numbers, UPI payment handles, rupee amounts and
common Indian bank + wallet names (SBI, HDFC, Paytm, PhonePe ...).

NOTE: keep the patterns in sync with the browser-side highlighter in
frontend/lib/constants.js (highlightIOCs) used by the chat bubbles.
"""

import re

class EntityExtractor:

    # -----------------------------
    # Patterns
    # -----------------------------
    # Each pattern below targets the formats scammers embed in phishing
    # SMS / WhatsApp / email text. Compile-time safety: they are raw
    # strings, so every backslash reaches the regex engine untouched.

    # Indian mobile numbers: optional +91 prefix, then a 10-digit
    # number starting with 6-9 (valid Indian mobile prefixes).
    PHONE_PATTERN = r"(?:\+91[-\s]?)?[6-9]\d{9}"

    # Standard email addresses: local@domain.tld
    EMAIL_PATTERN = (
        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}"
    )

    # Full URLs: http(s):// or www. plus optional path / query string.
    # Phishing links almost always embed a tracking path or token.
    URL_PATTERN = (
        r"(?:https?://|www\.)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        r"(?:/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*)?"
    )

    # Bare domains with no scheme ("sbi-secure-login.co.in").
    # Restricted to common TLDs so ordinary words are not flagged.
    DOMAIN_PATTERN = (
        r"\b[A-Za-z0-9.-]+\.(?:com|in|org|net|co\.in)\b"
    )

    # UPI payment handles look like name@bank (payments@okaxis).
    # Emails are stripped from the text first to avoid overlap.
    UPI_PATTERN = (
        r"\b[a-zA-Z0-9._-]{2,}@[a-zA-Z]{2,}\b"
    )

    # OTP wording - the signature of OTP-theft scams.
    OTP_PATTERN = (
        r"\bOTP\b|\bone[- ]?time password\b"
    )

    # Currency amounts: rupee sign / Rs. / INR + grouped digits.
    AMOUNT_PATTERN = (
        r"(?:₹|Rs\.?|INR)\s?\d+(?:,\d+)*(?:\.\d+)?"
    )

    # Well-known banks + payment wallets targeted by impersonators.
    BANK_PATTERN = (
        r"\b("
        r"SBI|HDFC|ICICI|Axis|PNB|BOB|"
        r"Kotak|Canara|Union Bank|"
        r"Paytm|PhonePe|Google Pay"
        r")\b"
    )

    # -----------------------------
    # Extract
    # -----------------------------

    @classmethod
    def extract(cls, text: str) -> dict:

        """
        Scan raw message text and return every recognised IOC.

        Parameters
        ----------
        text : str
            The suspicious message (or one full conversation line).

        Returns
        -------
        dict
            Keys: phone_numbers, emails, urls, upi_ids, otp_keywords,
            amounts, bank_names - each a sorted, de-duplicated list.
        """

        # -----------------------------
        # URLs
        # -----------------------------

        urls = []

        for url in re.findall(
            cls.URL_PATTERN,
            text,
            re.IGNORECASE
        ):

            # Remove words accidentally attached
            url = re.split(
                r"(Reference|Regards|Call|Email|Phone|OTP)",
                url,
                flags=re.IGNORECASE
            )[0]

            url = url.rstrip(
                ".,!?:;)]>\"'"
            )

            urls.append(url)

        # -----------------------------
        # Standalone Domains
        # -----------------------------

        for domain in re.findall(
            cls.DOMAIN_PATTERN,
            text,
            re.IGNORECASE
        ):

            if not any(
                domain in url
                for url in urls
            ):
                urls.append(domain)

        # -----------------------------
        # Remove emails before UPI search
        # -----------------------------
        # Emails and UPI handles share the "x@y" shape, so matching on
        # the raw text would double-report addresses as UPI IDs. We
        # strip emails from a scratch copy and search UPI on that.

        clean_text = re.sub(
            cls.EMAIL_PATTERN,
            "",
            text,
            flags=re.IGNORECASE
        )

        # -----------------------------
        # Normalize (de-dupe + sort each IOC family)
        # -----------------------------

        phones = sorted(
            set(
                re.findall(
                    cls.PHONE_PATTERN,
                    text,
                    re.IGNORECASE
                )
            )
        )

        emails = sorted(
            set(
                re.findall(
                    cls.EMAIL_PATTERN,
                    text,
                    re.IGNORECASE
                )
            )
        )

        upis = sorted(
            set(
                re.findall(
                    cls.UPI_PATTERN,
                    clean_text,
                    re.IGNORECASE
                )
            )
        )

        otp_keywords = sorted(
            set(
                re.findall(
                    cls.OTP_PATTERN,
                    text,
                    re.IGNORECASE
                )
            )
        )

        amounts = sorted(
            set(
                re.findall(
                    cls.AMOUNT_PATTERN,
                    text,
                    re.IGNORECASE
                )
            )
        )

        banks = sorted(
            set(
                re.findall(
                    cls.BANK_PATTERN,
                    text,
                    re.IGNORECASE
                )
            )
        )

        urls = sorted(set(urls))

        # -----------------------------
        # Return
        # -----------------------------

        return {

            "phone_numbers": phones,

            "emails": emails,

            "urls": urls,

            "upi_ids": upis,

            "otp_keywords": otp_keywords,

            "amounts": amounts,

            "bank_names": banks,
        }
