"""End-to-end run: tools, architecture, guardrails, ProcurementDecision."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from src import policy
from src.agents import LLMOutcome, run_single, run_staged
from src.contracts import Architecture, EvidenceItem, ProcurementDecision, RunTelemetry
from src.llm import GroqClient, sanitize_text
from src.sources import SourceData
from src.tools import LLM_SOURCE, ToolBox, TraceEvent

_APPROVAL_LANGUAGE = re.compile(
    r"\b(is|are|was|has been|have been|been)\s+(hereby\s+)?(fully\s+)?(approved|authori[sz]ed|cleared|rejected|denied|declined)\b"
    r"|\bauto-?approv|\bapprove\s+(it|this|the request)\s+(immediately|now)\b|\bpurchase\s+(it|this)\s+(now|immediately)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(not|cannot|can't|can not|never|until|before|pending|unless|without|no|requires?|needs?|awaiting|await)\b|n't\b",
    re.IGNORECASE,
)


def grants_approval(text: str) -> bool:
    """True when a sentence presents an approval or purchase as granted.

    Negated or conditional phrasing ("has not been approved", "requires approval")
    is allowed because it is the correct way to describe a pending decision.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if _APPROVAL_LANGUAGE.search(sentence) and not _NEGATION.search(sentence):
            return True
    return False


@dataclass
class RunResult:
    decision: ProcurementDecision
    architecture: str
    trace: list[TraceEvent] = field(default_factory=list)
    latency_ms: float = 0.0
    llm_wait_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0
    llm_error: str | None = None
    used_fallback: bool = False
    forced_tools: list[str] = field(default_factory=list)
    guardrail_notes: list[str] = field(default_factory=list)
    llm_added_flags: list[str] = field(default_factory=list)
    dropped_llm_claims: int = 0
    action: str = ""
    action_floor: str = ""
    policy: dict = field(default_factory=dict)
    llm_raw: list[dict] = field(default_factory=list)


def _next_step_template(action: str, pol: policy.PolicyResult, matches: list[dict]) -> str:
    approvals = ", ".join(pol.approvals) or "the relevant approvers"
    if action == policy.ACT_CLARIFY:
        items = "; ".join(m.split(" (")[0] for m in pol.missing_information) or "the missing details"
        return (
            f"Return the request to the requester to provide: {items}. The approval chain cannot be finalised "
            "until these are supplied; re-run the review afterwards."
        )
    if action == policy.ACT_HOLD:
        return (
            "Send the evidence package to Security for manual verification of the vendor's status before any "
            f"approval is considered; the other required reviewers ({approvals}) wait for that result."
        )
    if action == policy.ACT_BUDGET:
        return (
            "Send the request to Finance for a budget exception review and route the evidence package to "
            f"{approvals} for the other required reviews."
        )
    if action == policy.ACT_EXISTING:
        names = ", ".join(m["product_name"] for m in matches[:2]) or "the existing tool"
        return (
            f"Ask the requester to confirm why {names} cannot meet the need before any new purchase; keep "
            f"the approval chain ({approvals}) pending that answer."
        )
    if action == policy.ACT_REVIEWS:
        return f"Send the evidence package to {approvals} for human review before any approval decision."
    return f"Route to {approvals} for the standard approval chain; no additional review was triggered."


def _summary_template(action: str, pol: policy.PolicyResult) -> str:
    if action == policy.ACT_CLARIFY:
        return "Required information is missing, so no approval path can be set yet."
    if action == policy.ACT_HOLD:
        return "Vendor security evidence is unavailable or conflicting, so manual review is needed before anything else."
    if action == policy.ACT_BUDGET:
        return "The annual cost exceeds the department's available budget and needs a Finance exception review."
    if action == policy.ACT_REVIEWS:
        return "Policy triggers additional human reviews before this request can be approved."
    if action == policy.ACT_EXISTING:
        return "An existing approved tool may already cover the need; confirm the gap before buying."
    return "No additional review is triggered; the standard approval chain applies."


def _all_untrusted_text(box: ToolBox) -> list[str]:
    st = box.state
    texts: list[str] = []
    req = st.request or {}
    for k in ("business_justification", "product_name", "category", "data_access_level"):
        texts.append(str(req.get(k) or ""))
    texts.extend(str(i) for i in (req.get("requested_integrations") or []))
    if st.registry:
        texts.append(str(st.registry.get("notes") or ""))
    if st.risk is not None and st.risk.ok and st.risk.data:
        texts.append(str(st.risk.data.get("notes") or ""))
    for m in (st.catalog_result or {}).get("matches", []):
        texts.append(str(m.get("notes") or ""))
    return texts


def finalize(box: ToolBox, outcome: LLMOutcome | None, llm: GroqClient | None) -> tuple[ProcurementDecision, dict]:
    """Merge model judgement with deterministic results under guardrails."""
    notes: list[str] = []
    extra: dict = {"added_flags": [], "dropped": 0, "action": "", "used_fallback": False}
    box.collect_missing(forced=True)
    st = box.state
    pol = st.policy
    assert pol is not None
    matches = (st.catalog_result or {}).get("matches", [])
    match_ids = {m["software_id"]: m for m in matches}

    evidence = box.build_evidence()
    flags = set(pol.risk_flags)
    approvals = list(pol.approvals)

    action = pol.action_floor
    summary = ""
    next_step = ""

    if outcome is None or outcome.error or not outcome.action:
        extra["used_fallback"] = True
        reason = outcome.error if outcome and outcome.error else "no model output"
        notes.append(f"LLM unavailable ({reason}); deterministic fallback used for wording and action")
        evidence.append(
            EvidenceItem(
                source="apply_policy_rules",
                finding="Language model unavailable; recommendation and next step come from deterministic rules only",
                reference="fallback_policy",
            )
        )
    else:
        # --- overlap assessments: must cite real catalog ids -----------------
        seen: set[str] = set()
        duplicates = 0
        for item in outcome.overlap_assessments:
            sid = str(item.get("software_id") or "")
            verdict = str(item.get("verdict") or "")
            if sid not in match_ids or sid in seen:
                extra["dropped"] += 1
                notes.append(f"dropped overlap assessment citing unknown software_id '{sid}'")
                continue
            seen.add(sid)
            m = match_ids[sid]
            reason = sanitize_text(str(item.get("reason") or ""))[:240]
            evidence.append(
                EvidenceItem(
                    source=LLM_SOURCE,
                    finding=f"{m['product_name']}: {verdict.replace('_', ' ')}. {reason}",
                    reference=sid,
                )
            )
            if verdict == "duplicate_need":
                duplicates += 1
            if verdict in ("duplicate_need", "partial_overlap") and policy.FLAG_OVERLAP not in flags:
                flags.add(policy.FLAG_OVERLAP)
                extra["added_flags"].append(policy.FLAG_OVERLAP)

        # --- injection claims must quote text that really exists --------------
        inj = outcome.injection or {}
        quote = str(inj.get("quote") or "").strip()
        if inj.get("detected") and quote:
            haystack = " ".join(_all_untrusted_text(box)).casefold()
            if quote.casefold() in haystack:
                if policy.FLAG_INJECTION not in flags:
                    flags.add(policy.FLAG_INJECTION)
                    extra["added_flags"].append(policy.FLAG_INJECTION)
                    evidence.append(
                        EvidenceItem(
                            source=LLM_SOURCE,
                            finding=f"Instruction-like text found in untrusted input and ignored: \"{quote[:160]}\"",
                            reference="Policy section 9",
                        )
                    )
            else:
                extra["dropped"] += 1
                notes.append("dropped prompt-injection claim whose quote is not present in the input")
        elif inj.get("detected"):
            extra["dropped"] += 1
            notes.append("dropped prompt-injection claim without a quote")

        # --- action: must come from the allowed set ---------------------------
        chosen = outcome.action
        if chosen not in pol.allowed_actions:
            notes.append(f"action '{chosen}' not allowed (allowed: {pol.allowed_actions}); using floor")
            chosen = pol.action_floor
        elif chosen == policy.ACT_EXISTING and duplicates == 0:
            notes.append("REVIEW_EXISTING_TOOL chosen without a duplicate_need assessment; using floor")
            chosen = pol.action_floor
        action = chosen
        summary = sanitize_text(outcome.summary)[:320]
        next_step = sanitize_text(outcome.next_step)[:420]

    # --- wording guardrails (never present an approval as granted) ----------
    if not summary or grants_approval(summary):
        if summary:
            notes.append("summary replaced: contained approval language")
        summary = _summary_template(action, pol)
    if not next_step or grants_approval(next_step):
        if next_step:
            notes.append("next step replaced: contained approval language")
        next_step = _next_step_template(action, pol, pol.overlap_candidates or matches)

    # Reflect any flags added after policy evaluation, in canonical order.
    ordered_flags = [f for f in policy.FLAG_ORDER if f in flags] + sorted(f for f in flags if f not in policy.FLAG_ORDER)

    telemetry = RunTelemetry(
        llm_calls=llm.stats.calls if llm else 0,
        tool_calls=box.telemetry.tool_calls,
        tool_names=list(box.telemetry.tool_names),
    )
    decision = ProcurementDecision(
        request_id=st.request_id,
        recommendation=f"{policy.ACTION_LABELS[action]}. {summary}",
        evidence=evidence,
        required_approvals=approvals,
        missing_information=list(pol.missing_information),
        risk_flags=ordered_flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=telemetry,
    )
    extra["action"] = action
    extra["notes"] = notes
    return decision, extra


def analyze_request(
    request_id: str,
    architecture: Architecture = "single",
    *,
    sources: SourceData | None = None,
    request: dict | None = None,
    llm: GroqClient | None = None,
) -> RunResult:
    """Run one request through the chosen architecture and return the full result."""
    if architecture not in ("single", "staged", "rules"):
        raise ValueError(f"unknown architecture '{architecture}'")
    sources = sources or SourceData.load()
    started = time.perf_counter()
    box = ToolBox(sources, request_id, request)
    llm = llm or GroqClient()
    outcome: LLMOutcome | None = None

    if architecture == "rules":
        # Reference point for the evaluation: deterministic engine only, no model.
        box.collect_missing(forced=False)
        outcome = None
    elif not llm.configured:
        outcome = LLMOutcome(error="GROQ_API_KEY is not set")
        box.collect_missing(forced=True)
    elif architecture == "single":
        outcome = run_single(box, llm)
    else:
        outcome = run_staged(box, llm)

    decision, extra = finalize(box, outcome, llm if architecture != "rules" else None)
    box.trace.append(TraceEvent("stage", "finalize", f"action={extra['action']}", 0.0, True))
    result = RunResult(
        decision=decision,
        architecture=architecture,
        trace=box.trace,
        latency_ms=(time.perf_counter() - started) * 1000,
        llm_wait_ms=llm.stats.wait_seconds * 1000,
        prompt_tokens=llm.stats.prompt_tokens,
        completion_tokens=llm.stats.completion_tokens,
        llm_retries=llm.stats.retries,
        llm_error=outcome.error if outcome else None,
        used_fallback=extra["used_fallback"] and architecture != "rules",
        forced_tools=list(box.forced_tools),
        guardrail_notes=extra.get("notes", []),
        llm_added_flags=extra["added_flags"],
        dropped_llm_claims=extra["dropped"],
        action=extra["action"],
        action_floor=box.state.policy.action_floor if box.state.policy else "",
        policy=box.state.policy.to_dict() if box.state.policy else {},
        llm_raw=outcome.raw if outcome else [],
    )
    return result
