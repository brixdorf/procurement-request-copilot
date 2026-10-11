from __future__ import annotations

import json
import time

import pandas as pd
import streamlit as st

from src import config, policy
from src.config import REFERENCE_DATE
from src.llm import GroqClient
from src.pipeline import RunResult, analyze_request
from src.sources import SourceData

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

ARCH_LABELS = {"single": "A: Single agent", "staged": "B: Staged, 2 agents"}
ACTION_STYLE = {
    policy.ACT_PROCEED: "success",
    policy.ACT_REVIEWS: "warning",
    policy.ACT_BUDGET: "warning",
    policy.ACT_EXISTING: "info",
    policy.ACT_CLARIFY: "info",
    policy.ACT_HOLD: "error",
}
DATA_LEVELS = [
    "none", "internal_documents", "internal_marketing", "confidential_documents", "source_code",
    "customer_pii", "employee_pii", "production_telemetry", "credentials", "unknown",
]


@st.cache_resource
def load_sources() -> SourceData:
    return SourceData.load()


def money(value: object) -> str:
    return f"${float(value):,.0f}" if isinstance(value, (int, float)) else "not provided"


def run_analysis(request: dict, architecture: str) -> RunResult:
    return analyze_request(request["request_id"], architecture, sources=load_sources(), request=request)


def render_request(request: dict, sources: SourceData) -> None:
    emp = sources.employee(request.get("requester_id"))
    st.subheader("Purchase request")
    st.markdown(f"**{request.get('product_name')}**  \nVendor: {request.get('vendor_name')} | Category: {request.get('category')}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Annual cost", money(request.get("annual_cost_usd")))
    c2.metric("Users", request.get("user_count") if request.get("user_count") is not None else "n/a")
    c3.metric("Urgency", str(request.get("urgency") or "n/a").title())
    st.markdown(
        f"**Requester:** {emp['name'] if emp else 'unknown'} ({request.get('requester_id')}), "
        f"{emp['department'] if emp else 'unknown department'}  \n"
        f"**Data access:** `{request.get('data_access_level')}`  \n"
        f"**Integrations:** {', '.join(request.get('requested_integrations') or []) or 'none'}"
    )
    st.markdown("**Business justification** (untrusted text, handled as data)")
    st.info(request.get("business_justification") or "(empty)")
    hits = policy.scan_for_injection({"business_justification": request.get("business_justification")})
    if hits:
        st.warning("Instruction-like text detected in the justification. It will be ignored and flagged.")


def render_result(result: RunResult, key: str) -> None:
    d = result.decision
    style = ACTION_STYLE.get(result.action, "info")
    banner = getattr(st, style)
    banner(f"**{d.recommendation}**")
    st.markdown(f"**Next step:** {d.next_step}")
    st.caption("Advisory only. A human must approve any purchase, exception or budget change.")

    cols = st.columns(4)
    cols[0].metric("LLM calls", d.telemetry.llm_calls)
    cols[1].metric("Tool calls", d.telemetry.tool_calls)
    cols[2].metric("Latency", f"{result.latency_ms / 1000:.1f}s", help=f"Includes {result.llm_wait_ms / 1000:.1f}s waiting on provider rate limits")
    cols[3].metric("Tokens", result.prompt_tokens + result.completion_tokens)
    if result.used_fallback:
        st.warning(f"The language model was unavailable ({result.llm_error}). Deterministic rules produced this recommendation.")

    tabs = st.tabs(["Evidence", "Approvals and flags", "Missing information", "Trace", "Raw output"])
    with tabs[0]:
        if d.evidence:
            df = pd.DataFrame([e.model_dump() for e in d.evidence]).rename(
                columns={"source": "Source", "finding": "Finding", "reference": "Reference"}
            )
            st.dataframe(df, width="stretch", hide_index=True)
        else:
            st.write("No evidence collected.")
    with tabs[1]:
        a, b = st.columns(2)
        with a:
            st.markdown("**Required approvals**")
            if d.required_approvals:
                for item in d.required_approvals:
                    st.markdown(f"- {item}")
            else:
                st.write("None can be determined yet.")
        with b:
            st.markdown("**Risk flags**")
            if d.risk_flags:
                for item in d.risk_flags:
                    st.markdown(f"- `{item}`")
            else:
                st.write("No risk flags.")
    with tabs[2]:
        if d.missing_information:
            for item in d.missing_information:
                st.markdown(f"- {item}")
        else:
            st.write("Nothing missing.")
    with tabs[3]:
        if result.guardrail_notes:
            st.markdown("**Guardrail notes**")
            for n in result.guardrail_notes:
                st.markdown(f"- {n}")
        df = pd.DataFrame(
            [
                {"Step": t.kind, "Name": t.name, "Detail": t.detail, "ms": round(t.duration_ms), "OK": t.ok}
                for t in result.trace
            ]
        )
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption(
            f"Policy floor: `{result.action_floor}`; chosen action: `{result.action}`. "
            f"Tools forced by the coverage guard: {', '.join(result.forced_tools) or 'none'}."
        )
    with tabs[4]:
        st.json(json.loads(d.model_dump_json()))


def request_form(sources: SourceData) -> dict | None:
    employees = list(sources.employees.values())
    vendors = sorted({v["vendor_name"] for v in sources.vendors.values()})
    with st.sidebar.form("new_request"):
        requester = st.selectbox(
            "Requester", [e["employee_id"] for e in employees],
            format_func=lambda i: f"{i} - {sources.employees[i]['name']} ({sources.employees[i]['department']})",
        )
        product = st.text_input("Product name", "Example Analytics Add-on")
        vendor = st.selectbox("Vendor", vendors + ["(other vendor)"])
        other = st.text_input("Vendor name if other", "")
        category = st.text_input("Category", "Analytics / BI")
        cost = st.text_input("Annual cost USD (blank = not provided)", "9000")
        users = st.text_input("Number of users (blank = not provided)", "10")
        data = st.selectbox("Data access level", DATA_LEVELS)
        integrations = st.text_input("Integrations (comma separated)", "")
        urgency = st.selectbox("Urgency", ["normal", "high", "urgent"])
        why = st.text_area("Business justification", "Need dashboards for weekly pipeline reviews.")
        submitted = st.form_submit_button("Use this request")
    if not submitted and "custom_request" not in st.session_state:
        return None

    def num(text: str) -> float | None:
        try:
            return float(text.replace(",", "")) if text.strip() else None
        except ValueError:
            return None

    if submitted:
        cost_v, users_v = num(cost), num(users)
        st.session_state["custom_request"] = {
            "request_id": "REQ-CUSTOM",
            "requester_id": requester,
            "product_name": product,
            "vendor_name": other.strip() if vendor == "(other vendor)" and other.strip() else vendor,
            "category": category,
            "annual_cost_usd": int(cost_v) if cost_v is not None and cost_v.is_integer() else cost_v,
            "user_count": int(users_v) if users_v is not None else None,
            "business_justification": why,
            "data_access_level": data,
            "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()],
            "urgency": urgency,
        }
        st.session_state.pop("results", None)
    return st.session_state.get("custom_request")


def main() -> None:
    sources = load_sources()
    st.title("AI Procurement Request Copilot")
    st.caption(
        f"Gathers evidence, applies deterministic policy rules and recommends the next action. "
        f"Policy reference date: {REFERENCE_DATE.isoformat()}. Recommendations are advisory."
    )

    st.sidebar.header("Request")
    mode = st.sidebar.radio("Source", ["Dataset request", "New request"], horizontal=True)
    if mode == "Dataset request":
        ids = list(sources.requests)
        rid = st.sidebar.selectbox(
            "Request", ids, format_func=lambda r: f"{r} - {sources.requests[r]['product_name']}"
        )
        request = sources.requests[rid]
    else:
        request = request_form(sources)

    st.sidebar.header("Architecture")
    choice = st.sidebar.radio(
        "Run", ["single", "staged", "compare"],
        format_func=lambda c: {"single": ARCH_LABELS["single"], "staged": ARCH_LABELS["staged"], "compare": "Compare A and B"}[c],
    )
    llm = GroqClient()
    st.sidebar.caption(f"Model: `{llm.model}`  \nAPI key: {'set' if llm.configured else 'missing (rules fallback only)'}")

    if request is None:
        st.info("Fill in the request form in the sidebar, then press Run analysis.")
        return

    left, right = st.columns([0.9, 1.1], gap="large")
    with left:
        render_request(request, sources)
    with right:
        st.subheader("Copilot recommendation")
        run = st.button("Run analysis", type="primary", width="stretch")
        state_key = (request["request_id"], json.dumps(request, sort_keys=True), choice)
        if run:
            archs = ["single", "staged"] if choice == "compare" else [choice]
            results = {}
            for arch in archs:
                with st.spinner(f"Running {ARCH_LABELS[arch]} ..."):
                    results[arch] = run_analysis(request, arch)
            st.session_state["results"] = (state_key, results)
        stored = st.session_state.get("results")
        if stored and stored[0] == state_key:
            results = stored[1]
            if len(results) == 1:
                arch, res = next(iter(results.items()))
                st.caption(ARCH_LABELS[arch])
                render_result(res, arch)
            else:
                cols = st.columns(2)
                for col, (arch, res) in zip(cols, results.items()):
                    with col:
                        st.markdown(f"##### {ARCH_LABELS[arch]}")
                        render_result(res, arch)
                a, b = results["single"].decision, results["staged"].decision
                same = (
                    set(a.required_approvals) == set(b.required_approvals)
                    and set(a.risk_flags) == set(b.risk_flags)
                    and results["single"].action == results["staged"].action
                )
                if same:
                    st.success("Both architectures agree on action, approvals and flags.")
                else:
                    st.warning("The architectures differ on action, approvals or flags. Compare the traces.")
        else:
            st.info("Press Run analysis to gather evidence and get a recommendation.")

    st.divider()
    st.caption("Important: recommendations are advisory. Human approval remains required for purchasing decisions.")


main()
