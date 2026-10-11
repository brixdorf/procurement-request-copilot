from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.pipeline import analyze_request


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the public and hidden evaluation harness.

    architecture "single": one tool-calling agent (Architecture A).
    architecture "staged": fixed evidence collection plus two narrow agents
    (Architecture B).

    Raises KeyError for an unknown request_id. Tool or model failures never
    raise: they degrade to a deterministic recommendation that routes to human
    review and records what could not be verified.
    """
    return analyze_request(request_id, architecture).decision
