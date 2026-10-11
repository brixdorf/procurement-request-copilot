"""Central configuration: reference date, model settings, tunables."""
from __future__ import annotations

import os
import re
from datetime import date

from src import ROOT

POLICY_PATH = ROOT / "data" / "procurement_policy.md"


def _read_reference_date() -> date:
    """The policy file defines the data snapshot date; never use the machine clock."""
    try:
        text = POLICY_PATH.read_text(encoding="utf-8")
        match = re.search(r"reference date:\*\*\s*(\d{4})-(\d{2})-(\d{2})", text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except OSError:
        pass
    return date(2026, 9, 30)


REFERENCE_DATE: date = _read_reference_date()

# Policy section 5: a vendor security assessment is current for 365 days.
REVIEW_VALIDITY_DAYS = 365

# Policy section 7: new vendor and annual spend of $10,000 or more needs Legal.
LEGAL_NEW_VENDOR_THRESHOLD_USD = 10_000

# LLM settings (Groq's OpenAI-compatible endpoint).
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
DEFAULT_MODEL = "openai/gpt-oss-20b"


def model_name() -> str:
    return os.getenv("MODEL_NAME") or DEFAULT_MODEL


def groq_api_key() -> str | None:
    key = os.getenv("GROQ_API_KEY", "").strip()
    return key or None


# Agent loop bounds (stop conditions).
SINGLE_AGENT_MAX_STEPS = 8
LLM_TIMEOUT_SECONDS = 45.0
LLM_MAX_ATTEMPTS = 4
LLM_MAX_BACKOFF_SECONDS = 25.0
