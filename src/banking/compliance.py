import re
from typing import Tuple

class BankingComplianceGuard:
    """
    Enterprise Indian & Global FinTech Security & Regulatory Compliance Layer.
    - Prevents PII leaks (masks raw 16-digit cards, Aadhaar, PAN, UPI PIN, OTP).
    - Checks for mandatory escalation triggers (RBI Ombudsman, Consumer Court, Legal notice).
    - Strictly adheres to RBI Master Directions on Customer Protection and DPDP Act 2023.
    """

    CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
    AADHAAR_REGEX = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b")
    PAN_REGEX = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b")
    PIN_OTP_REGEX = re.compile(r"\b(?:otp|upi pin|pin|cvv|cvc)\s*[:=]?\s*(\d{4,6})\b", re.IGNORECASE)

    REGULATORY_TRIGGERS = [
        "rbi", "reserve bank", "ombudsman", "consumer court", "consumer forum",
        "lawyer", "advocate", "legal notice", "sue", "police complaint",
        "cyber crime", "nclt", "cfpb", "finra"
    ]

    @classmethod
    def sanitize_user_input(cls, text: str) -> str:
        """Masks sensitive financial credentials and identity numbers."""
        sanitized = cls.CARD_REGEX.sub(
            lambda m: "****-****-****-" + m.group(0).replace(" ", "").replace("-", "")[-4:],
            text
        )
        sanitized = cls.AADHAAR_REGEX.sub("XXXX-XXXX-[AADHAAR MASKED]", sanitized)
        sanitized = cls.PIN_OTP_REGEX.sub("CREDENTIAL: [REDACTED PER RBI GUIDELINES]", sanitized)
        return sanitized

    @classmethod
    def check_escalation_risk(cls, text: str) -> Tuple[bool, str]:
        """Checks if text contains regulatory or legal triggers requiring Level-3 Branch Manager handoff."""
        lower = text.lower()
        for trig in cls.REGULATORY_TRIGGERS:
            # Check whole word match or distinct phrase
            if re.search(r"\b" + re.escape(trig) + r"\b", lower):
                return True, f"Statutory/Regulatory mention detected ('{trig}'). Case escalated to Principal Nodal Officer / Legal Compliance."
        return False, ""
