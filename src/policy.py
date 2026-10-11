"""Deterministic policy engine.

Everything in this module is plain code: no LLM, no I/O. It encodes the rules of
data/procurement_policy.md so thresholds, review triggers and date checks are
repeatable and testable. The LLM may interpret and explain; it may not override
what this module decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from src.config import LEGAL_NEW_VENDOR_THRESHOLD_USD, REVIEW_VALIDITY_DAYS
from src.vendor_client import VendorRiskResult

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
MANAGER = "Manager"
DEPT_HEAD = "Department Head"
PROCUREMENT = "Procurement"
FINANCE = "Finance"
CFO = "CFO"
SECURITY = "Security"
PRIVACY = "Privacy"
LEGAL = "Legal"
APPROVAL_ORDER = [MANAGER, DEPT_HEAD, PROCUREMENT, FINANCE, CFO, SECURITY, PRIVACY, LEGAL]

FLAG_OVERLAP = "existing_tool_overlap"
FLAG_BUDGET = "budget_insufficient"
FLAG_SECURITY = "security_review_required"
FLAG_PRIVACY = "privacy_review_required"
FLAG_LEGAL = "legal_review_required"
FLAG_EXPIRED = "vendor_review_expired"
FLAG_CONFLICT = "conflicting_vendor_evidence"
FLAG_UNAVAILABLE = "vendor_risk_unavailable"
FLAG_INJECTION = "prompt_injection_detected"
FLAG_MISSING = "missing_information"
FLAG_ORDER = [
    FLAG_MISSING, FLAG_INJECTION, FLAG_BUDGET, FLAG_OVERLAP, FLAG_SECURITY, FLAG_PRIVACY,
    FLAG_LEGAL, FLAG_EXPIRED, FLAG_CONFLICT, FLAG_UNAVAILABLE,
]

# Recommended-action codes, ordered from most to least cautious as a floor.
ACT_CLARIFY = "REQUEST_CLARIFICATION"
ACT_HOLD = "HOLD_FOR_MANUAL_REVIEW"
ACT_BUDGET = "BUDGET_EXCEPTION_REVIEW"
ACT_REVIEWS = "ROUTE_FOR_REVIEWS"
ACT_PROCEED = "PROCEED_WITH_APPROVALS"
ACT_EXISTING = "REVIEW_EXISTING_TOOL"
ALL_ACTIONS = [ACT_CLARIFY, ACT_HOLD, ACT_BUDGET, ACT_REVIEWS, ACT_PROCEED, ACT_EXISTING]
ACTION_LABELS = {
    ACT_CLARIFY: "Request clarification from the requester",
    ACT_HOLD: "Hold for manual review (vendor evidence unavailable or conflicting)",
    ACT_BUDGET: "Route for budget exception review",
    ACT_REVIEWS: "Route for required reviews before approval",
    ACT_PROCEED: "Proceed with the standard approval chain",
    ACT_EXISTING: "Review the existing tool before buying",
}

SENSITIVE_FOR_REGION = {"customer_pii", "employee_pii", "pii", "confidential_documents", "credentials", "source_code"}
PII_CLASSES = {"customer_pii", "employee_pii", "pii"}


def norm(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def parse_number(value: object) -> float | None:
    """Return a non-negative number or None when absent/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    if number != number or number < 0:  # NaN or negative
        return None
    return number


# ---------------------------------------------------------------------------
# Prompt injection scan (deterministic first line of defence)
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    r"\bignore\b[^.\n]{0,50}\b(rules?|instructions?|polic(?:y|ies)|guidelines?|controls?|checks?|reviews?)\b",
    r"\bdisregard\b[^.\n]{0,50}\b(rules?|instructions?|polic(?:y|ies)|guidelines?|controls?)\b",
    r"\b(treat|consider|regard|mark|label|record)\b[^.\n]{0,40}\b(as|it as|this as)\b[^.\n]{0,25}\b(approved|pre-?approved|authori[sz]ed|cleared)\b",
    r"\bcfo[- ]?approved\b",
    r"\bapprove\b[^.\n]{0,30}\b(immediately|now|without|automatically)\b",
    r"\b(bypass|override|circumvent)\b[^.\n]{0,40}\b(polic(?:y|ies)|review|approval|control|security|process|rules?)\b",
    r"\bskip\b[^.\n]{0,30}\b(review|approval|security|legal|privacy|checks?)\b",
    r"\b(do not|don't|never)\b[^.\n]{0,25}\b(flag|escalate|route|mention|report)\b",
    r"\b(you are now|new instructions?|system prompt|developer message|act as)\b",
    r"\b(reveal|print|show|expose|leak)\b[^.\n]{0,30}\b(api[- ]?key|secret|password|system prompt|credentials?)\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def scan_for_injection(fields: dict[str, object]) -> list[dict]:
    """Return [{field, snippet}] for untrusted text that tries to instruct the system."""
    hits: list[dict] = []
    for name, value in fields.items():
        if value is None:
            continue
        texts = [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
        for text in texts:
            for sentence in split_sentences(text):
                if any(p.search(sentence) for p in _INJECTION_RE):
                    hits.append({"field": name, "snippet": sentence[:200]})
    return hits


def strip_injected_sentences(text: str) -> str:
    return " ".join(s for s in split_sentences(text) if not any(p.search(s) for p in _INJECTION_RE))


# ---------------------------------------------------------------------------
# Required information (policy section 1)
# ---------------------------------------------------------------------------
_UNKNOWN_TOKENS = {"", "unknown", "n/a", "na", "tbd", "none specified", "not specified", "unspecified", "?"}
_NON_SENSITIVE_EXACT = {"none", "public", "no data", "no_data", "not applicable", "not_applicable"}


def classify_data_access(level: object, integrations: object) -> dict:
    """Map free-text data-access level and integrations to policy data classes."""
    text = norm(level).replace("-", "_").replace(" ", "_")
    classes: set[str] = set()
    unknown = False
    unrecognized = False

    def add_from(raw: str, from_integration: bool) -> None:
        t = norm(raw).replace("-", "_").replace(" ", "_")
        if not t:
            return
        if "source_code" in t or "sourcecode" in t or "repositor" in t or t in {"git", "github", "gitlab"}:
            classes.add("source_code")
        if "pii" in t or "personal" in t:
            if "customer" in t:
                classes.add("customer_pii")
            elif "employee" in t or "staff" in t or t.startswith("hr"):
                classes.add("employee_pii")
            else:
                classes.add("pii")
        elif "customer" in t and ("data" in t or "record" in t):
            classes.add("customer_pii")
        if "confidential" in t:
            classes.add("confidential_documents")
        if any(k in t for k in ("credential", "secret", "password", "api_key", "apikey")):
            classes.add("credentials")
        if "production" in t or "cloud_account" in t or "cloud_infra" in t:
            classes.add("production_access")

    add_from(str(level or ""), False)
    for item in integrations or []:
        add_from(str(item), True)

    if text in _UNKNOWN_TOKENS or level is None:
        unknown = True
    elif not classes and text not in _NON_SENSITIVE_EXACT and not text.startswith("internal"):
        unrecognized = True
    return {
        "classes": sorted(classes),
        "unknown": unknown,
        "unrecognized": unrecognized,
        "raw": level,
    }


def check_required_fields(request: dict, employee: dict | None) -> dict:
    """Policy section 1: list what is missing; never invent values."""
    missing: list[str] = []
    notes: list[str] = []
    if employee is None:
        missing.append("Requester identity and department (requester_id not found in the employee directory)")
    if not norm(request.get("product_name")) or not norm(request.get("vendor_name")):
        missing.append("Product and vendor name")
    if parse_number(request.get("annual_cost_usd")) is None:
        missing.append("Annual cost or a reasonable annual estimate (annual_cost_usd is missing or invalid)")
    users = parse_number(request.get("user_count"))
    if users is None or users <= 0:
        missing.append("Number of users/licenses (user_count is missing or invalid)")
    purpose = strip_injected_sentences(str(request.get("business_justification") or ""))
    if len(purpose.split()) < 5:
        missing.append("Business purpose (justification is missing or contains no substantive use case)")
    data = classify_data_access(request.get("data_access_level"), request.get("requested_integrations"))
    if data["unknown"]:
        missing.append("Intended data-access level (data_access_level is unknown)")
    elif data["unrecognized"]:
        missing.append(
            f"Intended data-access level ('{request.get('data_access_level')}' does not map to a policy data class)"
        )
    if "requested_integrations" not in request or request.get("requested_integrations") is None:
        missing.append("Required integrations (not stated; state 'none' if there are none)")
    return {"missing": missing, "notes": notes, "data_profile": data}


# ---------------------------------------------------------------------------
# Budget (section 2) and approval thresholds (section 4)
# ---------------------------------------------------------------------------
def budget_check(department: str | None, cost: float | None, budget_row: dict | None) -> dict:
    if budget_row is None:
        return {"status": "unverifiable", "department": department, "reason": "department budget record not found"}
    available = float(budget_row["available_usd"])
    base = {
        "department": budget_row["department"],
        "available_usd": available,
        "annual_budget_usd": float(budget_row["annual_software_budget_usd"]),
        "committed_usd": float(budget_row["committed_usd"]),
    }
    if cost is None:
        return {**base, "status": "unverifiable", "reason": "annual cost not provided"}
    within = cost <= available
    return {
        **base,
        "status": "within_budget" if within else "over_budget",
        "annual_cost_usd": cost,
        "remaining_usd": round(available - cost, 2) if within else 0.0,
        "shortfall_usd": 0.0 if within else round(cost - available, 2),
    }


def approval_tier(cost: float | None) -> dict:
    """Deterministic minimum business approvals for the annualized amount."""
    if cost is None:
        return {"band": None, "approvals": []}
    if cost <= 1_000:
        return {"band": "up to $1,000", "approvals": [MANAGER]}
    if cost <= 10_000:
        return {"band": "$1,000.01 to $10,000", "approvals": [DEPT_HEAD, PROCUREMENT]}
    if cost <= 25_000:
        return {"band": "$10,000.01 to $25,000", "approvals": [DEPT_HEAD, FINANCE, PROCUREMENT]}
    return {"band": "above $25,000", "approvals": [DEPT_HEAD, FINANCE, CFO, PROCUREMENT]}


# ---------------------------------------------------------------------------
# Existing software / overlap (section 3)
# ---------------------------------------------------------------------------
def find_catalog_matches(request: dict, catalog: list[dict], purchases: list[dict], department: str | None) -> dict:
    product = norm(request.get("product_name"))
    vendor = norm(request.get("vendor_name"))
    category = norm(request.get("category"))
    matches: list[dict] = []
    for row in catalog:
        types: list[str] = []
        row_product = norm(row.get("product_name"))
        row_vendor = norm(row.get("vendor_name"))
        row_category = norm(row.get("category"))
        if product and row_product == product:
            types.append("same_product")
        elif product and row_product and (row_product in product or product in row_product):
            types.append("same_product_family")
        if category and row_category == category:
            types.append("same_category")
        if vendor and row_vendor == vendor:
            types.append("same_vendor")
        if not types:
            continue
        scope = norm(row.get("scope"))
        matches.append(
            {
                "software_id": row["software_id"],
                "product_name": row["product_name"],
                "vendor_name": row["vendor_name"],
                "category": row["category"],
                "status": row["status"],
                "annual_cost_usd": row["annual_cost_usd"],
                "licensed_seats": row["licensed_seats"],
                "scope": row["scope"],
                "notes": row["notes"],
                "match_types": types,
                "covers_requester_department": bool(scope == "company-wide" or (department and scope == norm(department))),
            }
        )
    history = [
        {k: p[k] for k in ("purchase_id", "purchase_date", "department", "product_name", "annual_amount_usd", "status", "notes")}
        for p in purchases
        if vendor and norm(p.get("vendor_name")) == vendor
    ]
    return {"matches": matches, "purchase_history": history}


def overlap_flag_candidates(matches: list[dict]) -> list[dict]:
    """Matches that justify the existing_tool_overlap flag (same category or exact product)."""
    return [m for m in matches if "same_category" in m["match_types"] or "same_product" in m["match_types"]]


# ---------------------------------------------------------------------------
# Vendor evidence (sections 5, 7 and 10)
# ---------------------------------------------------------------------------
def assess_vendor(registry: dict | None, risk: VendorRiskResult | None, ref_date: date) -> dict:
    out: dict = {
        "registry_known": registry is not None,
        "registry_security_status": registry.get("security_status") if registry else None,
        "registry_review_date": registry.get("security_review_date") if registry else None,
        "registry_legal_terms": registry.get("legal_terms_status") if registry else None,
        "registry_procurement_status": registry.get("procurement_status") if registry else None,
        "service_status": risk.status if risk else "not_called",
        "service_message": risk.message if risk else "",
        "effective_status": "unverified",
        "review_date": None,
        "age_days": None,
        "conflicts": [],
        "risk_level": None,
        "processes_personal_data": None,
        "stores_data_outside_region": None,
        "service_notes": None,
    }
    if risk is not None and risk.ok and risk.data:
        d = risk.data
        status = norm(d.get("security_review_status"))
        review = parse_date(d.get("last_review_date"))
        out.update(
            risk_level=d.get("risk_level"),
            processes_personal_data=d.get("processes_personal_data"),
            stores_data_outside_region=d.get("stores_data_outside_region"),
            service_notes=d.get("notes"),
            review_date=review.isoformat() if review else None,
            service_review_status=d.get("security_review_status"),
        )
        if review is not None:
            out["age_days"] = (ref_date - review).days
        if status == "approved":
            if review is None:
                out["effective_status"] = "not_completed"
            elif (ref_date - review).days <= REVIEW_VALIDITY_DAYS:
                out["effective_status"] = "current"
            else:
                out["effective_status"] = "expired"
        elif status == "expired":
            out["effective_status"] = "expired"
        else:
            out["effective_status"] = "not_completed"
    # Conflict detection: do the internal registry and the external service disagree?
    # Compared on the statuses each source states, not on the derived expiry, so two
    # sources that agree on a stale review are reported as expired, not conflicting.
    if registry is not None and risk is not None and risk.ok and risk.data:
        reg_status = registry.get("security_status")
        svc_status = risk.data.get("security_review_status")
        reg_approved = norm(reg_status) == "approved"
        svc_approved = norm(svc_status) == "approved"
        if reg_approved != svc_approved:
            out["conflicts"].append(
                f"Registry lists security status '{reg_status}' but the vendor-risk service reports '{svc_status}'"
            )
        reg_date = parse_date(registry.get("security_review_date"))
        svc_date = parse_date(risk.data.get("last_review_date"))
        if reg_date and svc_date and reg_date != svc_date:
            out["conflicts"].append(
                f"Review dates differ: registry {reg_date.isoformat()} vs service {svc_date.isoformat()}"
            )
    return out


# ---------------------------------------------------------------------------
# Full policy evaluation
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    source: str
    finding: str
    reference: str | None = None


@dataclass
class PolicyResult:
    approvals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    reviews: dict[str, list[str]] = field(default_factory=lambda: {"security": [], "privacy": [], "legal": []})
    approval_band: str | None = None
    budget: dict = field(default_factory=dict)
    vendor: dict = field(default_factory=dict)
    data_profile: dict = field(default_factory=dict)
    overlap_candidates: list[dict] = field(default_factory=list)
    injection_hits: list[dict] = field(default_factory=list)
    action_floor: str = ACT_PROCEED
    allowed_actions: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "required_approvals": self.approvals,
            "risk_flags": self.risk_flags,
            "missing_information": self.missing_information,
            "reviews_triggered": {k: v for k, v in self.reviews.items() if v},
            "approval_band": self.approval_band,
            "budget": self.budget,
            "vendor_status": {
                k: self.vendor.get(k)
                for k in ("effective_status", "review_date", "age_days", "service_status", "conflicts")
            },
            "prompt_injection_hits": self.injection_hits,
            "action_floor": self.action_floor,
            "allowed_actions": self.allowed_actions,
        }

    def to_llm_dict(self) -> dict:
        """Compact view shown to the model: conclusions and the facts that explain them."""
        b = self.budget or {}
        return {
            "required_approvals": self.approvals,
            "risk_flags": self.risk_flags,
            "missing_information": [m.split(" (")[0] for m in self.missing_information],
            "reviews_triggered": {k: v for k, v in self.reviews.items() if v},
            "approval_band": self.approval_band,
            "budget": {k: b[k] for k in ("status", "available_usd", "annual_cost_usd", "shortfall_usd") if k in b},
            "vendor_security": {
                "status": self.vendor.get("effective_status"),
                "review_date": self.vendor.get("review_date"),
                "service": self.vendor.get("service_status"),
                "conflicts": self.vendor.get("conflicts"),
            },
            "injection_fields": sorted({h["field"] for h in self.injection_hits}),
            "allowed_actions": self.allowed_actions,
        }


def _ordered(items: set[str], order: list[str]) -> list[str]:
    return [i for i in order if i in items] + sorted(i for i in items if i not in order)


def evaluate_policy(
    *,
    request: dict,
    employee: dict | None,
    budget_row: dict | None,
    catalog_result: dict,
    registry: dict | None,
    risk: VendorRiskResult | None,
    untrusted_extra: dict[str, object] | None = None,
    ref_date: date,
) -> PolicyResult:
    res = PolicyResult()
    approvals: set[str] = set()
    flags: set[str] = set()
    department = employee.get("department") if employee else None
    cost = parse_number(request.get("annual_cost_usd"))
    vendor_name = request.get("vendor_name") or "unknown vendor"
    rid = request.get("request_id", "request")

    # 1. Required information
    required = check_required_fields(request, employee)
    res.missing_information = required["missing"]
    res.data_profile = required["data_profile"]
    classes = set(res.data_profile["classes"])
    if res.missing_information:
        flags.add(FLAG_MISSING)
        res.findings.append(
            Finding("apply_policy_rules", f"{len(res.missing_information)} required item(s) missing or invalid: "
                    + "; ".join(m.split(" (")[0] for m in res.missing_information), "Policy section 1")
        )

    # 2. Prompt injection scan across every untrusted text field we can see
    scan_fields: dict[str, object] = {
        "request.business_justification": request.get("business_justification"),
        "request.product_name": request.get("product_name"),
        "request.category": request.get("category"),
        "request.data_access_level": request.get("data_access_level"),
        "request.requested_integrations": request.get("requested_integrations"),
    }
    scan_fields.update(untrusted_extra or {})
    res.injection_hits = scan_for_injection(scan_fields)
    if res.injection_hits:
        flags.add(FLAG_INJECTION)
        fields = ", ".join(sorted({h["field"] for h in res.injection_hits}))
        res.findings.append(
            Finding(
                "apply_policy_rules",
                f"Embedded instruction detected in untrusted text ({fields}); treated as data and ignored",
                "Policy section 9",
            )
        )

    # 3. Budget
    res.budget = budget_check(department, cost, budget_row)
    b = res.budget
    if b["status"] == "over_budget":
        flags.add(FLAG_BUDGET)
        approvals.add(FINANCE)
        res.findings.append(
            Finding(
                "check_budget",
                f"{b['department']} available software budget is {money(b['available_usd'])}; request of "
                f"{money(b['annual_cost_usd'])} exceeds it by {money(b['shortfall_usd'])}. Finance budget exception review required",
                "Policy section 2",
            )
        )
    elif b["status"] == "within_budget":
        res.findings.append(
            Finding(
                "check_budget",
                f"{b['department']} available software budget is {money(b['available_usd'])}; request of "
                f"{money(b['annual_cost_usd'])} is within budget ({money(b['remaining_usd'])} would remain). "
                "A positive budget check does not imply approval",
                "Policy section 2",
            )
        )
    else:
        res.findings.append(
            Finding("check_budget", f"Budget could not be verified: {b.get('reason')}", "Policy section 2")
        )

    # 4. Approval thresholds
    tier = approval_tier(cost)
    res.approval_band = tier["band"]
    approvals.update(tier["approvals"])
    if tier["band"]:
        res.findings.append(
            Finding(
                "apply_policy_rules",
                f"Annual amount {money(cost)} falls in the '{tier['band']}' band: minimum approvals "
                + " + ".join(tier["approvals"]),
                "Policy section 4",
            )
        )

    # 5. Overlap
    res.overlap_candidates = overlap_flag_candidates(catalog_result.get("matches", []))
    if res.overlap_candidates:
        flags.add(FLAG_OVERLAP)

    # 6. Vendor evidence
    res.vendor = assess_vendor(registry, risk, ref_date)
    v = res.vendor
    sec = res.reviews["security"]
    priv = res.reviews["privacy"]
    legal = res.reviews["legal"]

    if "source_code" in classes:
        sec.append("source code access")
    if "production_access" in classes:
        sec.append("production or cloud-account integration")
    if "confidential_documents" in classes:
        sec.append("confidential documents")
    if "employee_pii" in classes or "pii" in classes:
        sec.append("employee/personal PII")
    if "customer_pii" in classes:
        sec.append("customer PII")
    if "credentials" in classes:
        sec.append("credentials or secrets")

    status = v["effective_status"]
    if risk is None or not (risk.ok):
        # Policy section 10: never infer a favourable status when the check failed.
        flags.add(FLAG_UNAVAILABLE)
        reason = {
            "unavailable": "the vendor-risk service was unavailable",
            "not_found": "the vendor-risk service has no record for this vendor",
            "invalid": "the vendor-risk service returned an unusable response",
        }.get(risk.status if risk else "", "the vendor-risk service was not queried")
        sec.append(f"vendor security status could not be verified ({reason})")
        res.findings.append(
            Finding(
                "get_vendor_risk",
                f"Vendor security status for {vendor_name} could not be verified: {reason}"
                + (f" ({risk.message})" if risk and risk.message else "")
                + ". No favourable status was assumed; routed to manual review",
                "Policy section 10",
            )
        )
    elif status == "expired":
        flags.add(FLAG_EXPIRED)
        sec.append("vendor security assessment is expired")
        res.findings.append(
            Finding(
                "get_vendor_risk",
                f"Vendor assessment for {vendor_name} dated {v['review_date']} is {v['age_days']} days old at "
                f"{ref_date.isoformat()}; the 365-day validity has lapsed",
                "Policy section 5",
            )
        )
    elif status == "not_completed":
        sec.append("vendor security assessment is missing or not completed")
        res.findings.append(
            Finding(
                "get_vendor_risk",
                f"Vendor security assessment for {vendor_name} is not completed "
                f"(service status '{v.get('service_review_status')}', no current review date)",
                "Policy section 5",
            )
        )
    elif status == "current":
        res.findings.append(
            Finding(
                "get_vendor_risk",
                f"Vendor assessment for {vendor_name} is current: reviewed {v['review_date']} "
                f"({v['age_days']} days before {ref_date.isoformat()}), risk level {v['risk_level']}",
                "Policy section 5",
            )
        )

    if v["conflicts"]:
        flags.add(FLAG_CONFLICT)
        sec.append("internal registry and vendor-risk service disagree")
        for c in v["conflicts"]:
            res.findings.append(Finding("get_vendor_registry", f"Conflicting vendor evidence: {c}", "Policy section 5"))

    # 7. Privacy (section 6)
    if classes & PII_CLASSES:
        priv.append("employee or customer PII")
    outside = v.get("stores_data_outside_region")
    if outside and (classes & SENSITIVE_FOR_REGION):
        priv.append("sensitive data may be stored outside the operating region")
    if outside and res.data_profile["unknown"]:
        res.findings.append(
            Finding(
                "get_vendor_risk",
                f"{vendor_name} stores data outside the operating region and the requested data class is unknown; "
                "privacy exposure cannot be ruled out until data access is stated",
                "Policy section 6",
            )
        )

    # 8. Legal (section 7)
    new_vendor = registry is None or norm(registry.get("procurement_status")) == "new"
    if new_vendor and cost is not None and cost >= LEGAL_NEW_VENDOR_THRESHOLD_USD:
        legal.append(f"new vendor with annual spend of {money(cost)} (threshold {money(LEGAL_NEW_VENDOR_THRESHOLD_USD)})")
    terms = norm(registry.get("legal_terms_status")) if registry else ""
    if terms not in ("approved", "standard"):
        legal.append(
            "legal terms are not approved/standard"
            + (f" (registry status '{registry.get('legal_terms_status')}')" if registry else " (vendor not in registry)")
        )
    if outside and (classes & SENSITIVE_FOR_REGION):
        legal.append("material data-processing / cross-region issue")

    if registry is None:
        res.findings.append(
            Finding("get_vendor_registry", f"{vendor_name} is not in the internal vendor registry; treated as a new vendor", "vendors.csv")
        )
    else:
        res.findings.append(
            Finding(
                "get_vendor_registry",
                f"Registry: procurement status {registry.get('procurement_status')}, security {registry.get('security_status')}, "
                f"legal terms {registry.get('legal_terms_status')}",
                registry.get("vendor_id"),
            )
        )

    if sec:
        flags.add(FLAG_SECURITY)
        approvals.add(SECURITY)
        res.findings.append(Finding("apply_policy_rules", "Security review required: " + "; ".join(sec), "Policy section 5"))
    if priv:
        flags.add(FLAG_PRIVACY)
        approvals.add(PRIVACY)
        res.findings.append(Finding("apply_policy_rules", "Privacy review required: " + "; ".join(priv), "Policy section 6"))
    if legal:
        flags.add(FLAG_LEGAL)
        approvals.add(LEGAL)
        res.findings.append(Finding("apply_policy_rules", "Legal review required: " + "; ".join(legal), "Policy section 7"))

    # 9. Urgency never relaxes policy
    urgency = norm(request.get("urgency"))
    if urgency in ("high", "urgent", "critical"):
        res.findings.append(
            Finding("apply_policy_rules", f"Urgency '{urgency}' noted; it does not change policy requirements or approvals", f"{rid}.urgency")
        )

    # 10. Action floor and the actions the model may choose from
    if res.missing_information:
        floor = ACT_CLARIFY
    elif FLAG_UNAVAILABLE in flags or FLAG_CONFLICT in flags:
        floor = ACT_HOLD
    elif FLAG_BUDGET in flags:
        floor = ACT_BUDGET
    elif sec or priv or legal:
        floor = ACT_REVIEWS
    else:
        floor = ACT_PROCEED
    res.action_floor = floor
    if floor in (ACT_CLARIFY, ACT_HOLD):
        res.allowed_actions = [floor]
    else:
        res.allowed_actions = [floor] + ([ACT_EXISTING] if catalog_result.get("matches") else [])

    res.approvals = _ordered(approvals, APPROVAL_ORDER)
    res.risk_flags = _ordered(flags, FLAG_ORDER)
    return res
