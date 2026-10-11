"""Architecture comparison harness.

Runs the same case set through the rules-only reference, Architecture A
(single agent) and Architecture B (staged, 2 agents), scores each run against
hand-written ground truth, and writes CSV/JSON/Markdown results.

    python evals/run_comparison.py                       # all cases, all architectures
    python evals/run_comparison.py --architectures single staged --kinds public dataset
    python evals/run_comparison.py --cases PUB-01 SYN-08 --tag smoke
    python evals/run_comparison.py --model openai/gpt-oss-120b --tag gpt-oss-120b

Ground truth lives in evals/extended_cases.json and is derived from the policy
text, not from the solution's own output.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_public_evals import evaluate as public_minimum_checks  # noqa: E402
from src.pipeline import RunResult, analyze_request, grants_approval  # noqa: E402
from src.sources import SourceData  # noqa: E402
from src.tools import LLM_SOURCE  # noqa: E402
from src.vendor_client import VendorRiskResult  # noqa: E402

CASES_PATH = ROOT / "evals" / "extended_cases.json"
PUBLIC_PATH = ROOT / "evals" / "public_cases.json"
RESULTS_DIR = ROOT / "evals" / "results"
ARCH_LABEL = {"rules": "Rules only (no LLM)", "single": "A: single agent", "staged": "B: staged, 2 agents"}


# ---------------------------------------------------------------- fixtures
def build_sources(case: dict, base: SourceData) -> SourceData:
    overrides = case.get("overrides") or {}
    if not overrides:
        return base
    vendor_risk: dict[str, VendorRiskResult | dict] = {}
    for vendor, spec in (overrides.get("vendor_risk") or {}).items():
        if "record" in spec:
            vendor_risk[vendor] = spec["record"]
        else:
            r = spec["result"]
            vendor_risk[vendor] = VendorRiskResult(
                status=r["status"], data=None, http_status=r.get("http_status"), message=r.get("message", ""), attempts=3
            )
    return base.with_overrides(
        employees=overrides.get("employees"),
        budgets=overrides.get("budgets"),
        catalog=overrides.get("catalog"),
        vendors=overrides.get("vendors"),
        vendor_risk=vendor_risk or None,
    )


# ----------------------------------------------------------------- scoring
def score(case: dict, result: RunResult) -> dict:
    exp = case["expected"]
    d = result.decision
    approvals = set(d.required_approvals)
    flags = set(d.risk_flags)
    tool_names = set(d.telemetry.tool_names if d.telemetry else [])

    action_ok = result.action in exp["actions"]
    expected_approvals = exp.get("approvals")
    approvals_ok = True if expected_approvals is None else approvals == set(expected_approvals)
    required = set(exp.get("flags_required", []))
    optional = set(exp.get("flags_optional", []))
    flags_missing = sorted(required - flags)
    flags_extra = sorted(flags - required - optional)
    flags_ok = not flags_missing and not flags_extra

    missing_ok = True
    missing_text = " | ".join(m.lower() for m in d.missing_information)
    if exp.get("no_missing"):
        missing_ok = not d.missing_information
    for group in exp.get("missing_keywords", []):
        if not any(token in missing_text for token in group):
            missing_ok = False

    ungrounded = 0
    for item in d.evidence:
        known_source = item.source in tool_names or item.source == LLM_SOURCE
        if not known_source or not (item.reference or "").strip() or not item.finding.strip():
            ungrounded += 1
    grounded_ok = ungrounded == 0 and len(d.evidence) >= 2

    text = f"{d.recommendation} {d.next_step}"
    safe_ok = d.human_review_required is True and not grants_approval(text)

    policy_ok = approvals_ok and flags_ok and missing_ok
    passed = policy_ok and action_ok and grounded_ok and safe_ok
    return {
        "action_ok": action_ok,
        "approvals_ok": approvals_ok,
        "flags_ok": flags_ok,
        "missing_ok": missing_ok,
        "grounded_ok": grounded_ok,
        "safe_ok": safe_ok,
        "policy_ok": policy_ok,
        "passed": passed,
        "flags_missing": flags_missing,
        "flags_extra": flags_extra,
        "ungrounded_items": ungrounded,
    }


# ------------------------------------------------------------------ runner
def run(args: argparse.Namespace) -> None:
    if args.model:
        os.environ["MODEL_NAME"] = args.model
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    if args.cases:
        cases = [c for c in cases if c["case_id"] in set(args.cases)]
    if args.kinds:
        cases = [c for c in cases if c["kind"] in set(args.kinds)]
    public_expect = {c["case_id"]: c for c in json.loads(PUBLIC_PATH.read_text(encoding="utf-8"))}
    # extended ids PUB-0x map to public ids by request, since numbering differs for REQ-1004 etc.
    public_by_request = {c["request_id"]: c for c in public_expect.values()}

    base = SourceData.load()
    from src.config import model_name

    rows: list[dict] = []
    traces: list[dict] = []
    print(f"\nModel: {model_name()}  | cases: {len(cases)}  | architectures: {', '.join(args.architectures)}")
    for arch in args.architectures:
        print(f"\n=== {ARCH_LABEL[arch]} ===")
        stop_arch = False
        for case in cases:
            if stop_arch:
                break
            sources = build_sources(case, base)
            request = case.get("request")
            request_id = case.get("request_id") or request["request_id"]
            for rep in range(1, args.repeats + 1):
                started = time.perf_counter()
                try:
                    result = analyze_request(request_id, arch, sources=sources, request=request)
                except Exception as exc:  # a crash is a failed case, not a stopped evaluation
                    print(f"ERROR {case['case_id']}: {type(exc).__name__}: {exc}")
                    rows.append({"case_id": case["case_id"], "architecture": arch, "repeat": rep, "passed": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
                    continue
                s = score(case, result)
                pub = public_by_request.get(request_id) if case["kind"] in ("public",) and request is None else None
                pub_fail = public_minimum_checks(result.decision, pub["expectations"]) if pub else None
                tel = result.decision.telemetry
                net_ms = result.latency_ms - result.llm_wait_ms
                row = {
                    "case_id": case["case_id"], "kind": case["kind"], "title": case["title"] if "title" in case else "",
                    "architecture": arch, "repeat": rep,
                    "passed": s["passed"], "action_ok": s["action_ok"], "approvals_ok": s["approvals_ok"],
                    "flags_ok": s["flags_ok"], "missing_ok": s["missing_ok"], "grounded_ok": s["grounded_ok"],
                    "safe_ok": s["safe_ok"],
                    "public_minimum_pass": (not pub_fail) if pub is not None else "",
                    "action": result.action, "action_floor": result.action_floor,
                    "latency_ms": round(result.latency_ms), "latency_net_of_rate_limit_ms": round(net_ms),
                    "llm_calls": tel.llm_calls, "tool_calls": tel.tool_calls,
                    "tokens": result.prompt_tokens + result.completion_tokens,
                    "forced_tool_calls": len(result.forced_tools), "llm_retries": result.llm_retries,
                    "fallback_used": result.used_fallback, "dropped_llm_claims": result.dropped_llm_claims,
                    "llm_added_flags": ";".join(result.llm_added_flags),
                    "flags_missing": ";".join(s["flags_missing"]), "flags_extra": ";".join(s["flags_extra"]),
                    "guardrail_notes": " | ".join(result.guardrail_notes), "error": result.llm_error or "",
                }
                rows.append(row)
                traces.append(
                    {
                        "case_id": case["case_id"], "architecture": arch, "repeat": rep,
                        "recommendation": result.decision.recommendation, "next_step": result.decision.next_step,
                        "required_approvals": result.decision.required_approvals,
                        "risk_flags": result.decision.risk_flags,
                        "missing_information": result.decision.missing_information,
                        "evidence": [e.model_dump() for e in result.decision.evidence],
                        "trace": [t.__dict__ for t in result.trace],
                    }
                )
                if result.used_fallback and arch != "rules":
                    if "daily quota" in (result.llm_error or ""):
                        print(
                            f"STOP {case['case_id']}: provider daily quota exhausted; later cases would only measure "
                            "the rules fallback. Re-run when the quota refills.",
                            flush=True,
                        )
                        rows.pop()
                        traces.pop()
                        stop_arch = True
                        break
                    print(f"NOTE  {case['case_id']}: model path failed, deterministic fallback used ({(result.llm_error or '')[:90]})", flush=True)
                mark = "PASS" if s["passed"] else "FAIL"
                extra = ""
                if not s["passed"]:
                    bits = [k for k in ("action_ok", "approvals_ok", "flags_ok", "missing_ok", "grounded_ok", "safe_ok") if not s[k]]
                    extra = f"  <- {', '.join(bits)}"
                print(
                    f"{mark}  {case['case_id']:<7} {result.action:<24} llm={tel.llm_calls} tools={tel.tool_calls} "
                    f"{result.latency_ms/1000:5.1f}s (net {net_ms/1000:4.1f}s){extra}",
                    flush=True,
                )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag
    if rows:
        fields: list[str] = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
        with (out_dir / f"{tag}_cases.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        (out_dir / f"{tag}_traces.json").write_text(json.dumps(traces, indent=2), encoding="utf-8")
        summary = summarize(rows, args.architectures, model_name(), len(cases), args.repeats)
        (out_dir / f"{tag}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        md = render_markdown(summary)
        (out_dir / f"{tag}_summary.md").write_text(md, encoding="utf-8")
        print("\n" + md)
        print(f"Results written to {out_dir.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_dir}/{tag}_*")


# ----------------------------------------------------------------- summary
def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def summarize(rows: list[dict], architectures: list[str], model: str, n_cases: int, repeats: int) -> dict:
    out: dict = {"model": model, "cases": n_cases, "repeats": repeats, "architectures": {}}
    for arch in architectures:
        mine = [r for r in rows if r["architecture"] == arch]
        if not mine:
            continue
        n = len(mine)

        def rate(key: str) -> float:
            return round(100 * sum(1 for r in mine if r.get(key) is True) / n, 1)

        public_rows = [r for r in mine if r.get("public_minimum_pass") != ""]
        lat = [r["latency_ms"] for r in mine if "latency_ms" in r]
        net = [r["latency_net_of_rate_limit_ms"] for r in mine if "latency_net_of_rate_limit_ms" in r]
        out["architectures"][arch] = {
            "label": ARCH_LABEL[arch],
            "runs": n,
            "cases_passed": sum(1 for r in mine if r.get("passed") is True),
            "pass_rate_pct": rate("passed"),
            "public_minimum_checks_passed": f"{sum(1 for r in public_rows if r['public_minimum_pass'] is True)}/{len(public_rows)}",
            "action_correct_pct": rate("action_ok"),
            "approvals_exact_pct": rate("approvals_ok"),
            "risk_flags_exact_pct": rate("flags_ok"),
            "missing_info_correct_pct": rate("missing_ok"),
            "evidence_grounded_pct": rate("grounded_ok"),
            "safety_ok_pct": rate("safe_ok"),
            "policy_failures": sum(1 for r in mine if r.get("approvals_ok") is False or r.get("flags_ok") is False or r.get("missing_ok") is False),
            "avg_latency_ms": _mean(lat),
            "avg_latency_net_of_rate_limit_ms": _mean(net),
            "p95_latency_net_ms": sorted(net)[max(0, int(len(net) * 0.95) - 1)] if net else 0,
            "avg_llm_calls": _mean([r["llm_calls"] for r in mine if "llm_calls" in r]),
            "avg_tool_calls": _mean([r["tool_calls"] for r in mine if "tool_calls" in r]),
            "avg_tokens": _mean([r["tokens"] for r in mine if "tokens" in r]),
            "forced_tool_calls_total": sum(r.get("forced_tool_calls", 0) for r in mine),
            "fallbacks": sum(1 for r in mine if r.get("fallback_used") is True),
            "model_path_completed_pct": round(100 * sum(1 for r in mine if r.get("fallback_used") is False) / n, 1)
            if arch != "rules" else None,
            "dropped_llm_claims": sum(r.get("dropped_llm_claims", 0) for r in mine),
            "failed_cases": [f"{r['case_id']}" for r in mine if r.get("passed") is not True],
        }
    return out


def render_markdown(summary: dict) -> str:
    archs = summary["architectures"]
    keys = list(archs)
    head = "| Metric | " + " | ".join(archs[k]["label"] for k in keys) + " |\n"
    head += "|---|" + "---:|" * len(keys) + "\n"
    rows = [
        ("Cases passing every check", lambda a: f"{a['cases_passed']}/{a['runs']} ({a['pass_rate_pct']}%)"),
        ("Public minimum checks (starter runner)", lambda a: a["public_minimum_checks_passed"]),
        ("Correct next action", lambda a: f"{a['action_correct_pct']}%"),
        ("Approvals exactly right", lambda a: f"{a['approvals_exact_pct']}%"),
        ("Risk flags exactly right", lambda a: f"{a['risk_flags_exact_pct']}%"),
        ("Missing information right", lambda a: f"{a['missing_info_correct_pct']}%"),
        ("Evidence grounded in tool results", lambda a: f"{a['evidence_grounded_pct']}%"),
        ("Safety (human review, no granted approval)", lambda a: f"{a['safety_ok_pct']}%"),
        ("Policy failures (cases)", lambda a: str(a["policy_failures"])),
        ("Avg latency, ms (raw)", lambda a: f"{a['avg_latency_ms']:.0f}"),
        ("Avg latency, ms (excluding rate-limit waits)", lambda a: f"{a['avg_latency_net_of_rate_limit_ms']:.0f}"),
        ("P95 latency, ms (excluding rate-limit waits)", lambda a: f"{a['p95_latency_net_ms']:.0f}"),
        ("Avg LLM calls", lambda a: str(a["avg_llm_calls"])),
        ("Avg tool calls", lambda a: str(a["avg_tool_calls"])),
        ("Avg tokens", lambda a: f"{a['avg_tokens']:.0f}"),
        ("Tool calls forced by the coverage guard", lambda a: str(a["forced_tool_calls_total"])),
        ("Runs completed on the model path (no fallback)", lambda a: "n/a" if a["model_path_completed_pct"] is None else f"{a['model_path_completed_pct']}%"),
        ("LLM fallbacks to rules", lambda a: str(a["fallbacks"])),
        ("LLM claims dropped by guardrails", lambda a: str(a["dropped_llm_claims"])),
    ]
    body = "".join(f"| {name} | " + " | ".join(fn(archs[k]) for k in keys) + " |\n" for name, fn in rows)
    failed = "".join(
        f"- {archs[k]['label']}: {', '.join(archs[k]['failed_cases']) or 'none'}\n" for k in keys
    )
    return (
        f"Model: `{summary['model']}`, {summary['cases']} cases x {summary['repeats']} run(s) per architecture\n\n"
        + head + body + "\nCases not passing every check:\n" + failed
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--architectures", nargs="+", choices=["rules", "single", "staged"], default=["rules", "single", "staged"])
    parser.add_argument("--cases", nargs="*", help="case ids to run (default: all)")
    parser.add_argument("--kinds", nargs="*", choices=["public", "dataset", "synthetic"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", help="override MODEL_NAME for this run")
    parser.add_argument("--tag", default="comparison")
    parser.add_argument("--out-dir", default=str(RESULTS_DIR))
    run(parser.parse_args())


if __name__ == "__main__":
    main()
