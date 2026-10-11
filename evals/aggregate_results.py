"""Merge several run_comparison result sets into one combined summary.

The free model tier has a per-day token cap, so a full 26-case comparison is
run in batches (see README). This script merges the batches that used the same
model into one table and a per-case matrix.

    python evals/aggregate_results.py --tags gpt-oss-20b gpt-oss-20b-boundary --out gpt-oss-20b_all-cases
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_comparison import RESULTS_DIR, render_markdown, summarize  # noqa: E402

BOOL_FIELDS = {
    "passed", "action_ok", "approvals_ok", "flags_ok", "missing_ok", "grounded_ok", "safe_ok", "fallback_used",
}
INT_FIELDS = {
    "latency_ms", "latency_net_of_rate_limit_ms", "llm_calls", "tool_calls", "tokens", "forced_tool_calls",
    "llm_retries", "dropped_llm_claims", "repeat",
}


def load(tag: str) -> list[dict]:
    path = RESULTS_DIR / f"{tag}_cases.csv"
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for raw in csv.DictReader(f):
            row: dict = dict(raw)
            for k in BOOL_FIELDS:
                if k in row:
                    row[k] = row[k] == "True"
            for k in INT_FIELDS:
                if k in row and row[k] != "":
                    row[k] = int(float(row[k]))
            if row.get("public_minimum_pass") in ("True", "False"):
                row["public_minimum_pass"] = row["public_minimum_pass"] == "True"
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tags", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    rows: list[dict] = []
    for tag in args.tags:
        rows.extend(load(tag))
    archs = [a for a in ("rules", "staged", "single") if any(r["architecture"] == a for r in rows)]
    case_ids = sorted({r["case_id"] for r in rows})
    model = args.model or "see per-batch files"
    summary = summarize(rows, archs, model, len(case_ids), 1)
    md = render_markdown(summary)

    labels = {"single": "A", "staged": "B", "rules": "Rules"}
    matrix = "\n| Case | " + " | ".join(labels[a] for a in archs) + " | Notes |\n|---|" + ":-:|" * len(archs) + "---|\n"
    for cid in case_ids:
        cells, notes = [], []
        for a in archs:
            mine = [r for r in rows if r["case_id"] == cid and r["architecture"] == a]
            if not mine:
                cells.append("-")
                continue
            r = mine[0]
            cells.append("pass" if r.get("passed") else "FAIL")
            if not r.get("passed"):
                bad = [k for k in ("action_ok", "approvals_ok", "flags_ok", "missing_ok", "grounded_ok", "safe_ok") if r.get(k) is False]
                detail = f"{labels[a]}: {', '.join(bad)}"
                if r.get("flags_extra"):
                    detail += f" (extra flag {r['flags_extra']})"
                notes.append(detail)
            if r.get("fallback_used"):
                notes.append(f"{labels[a]}: model path failed, rules fallback")
        matrix += f"| {cid} | " + " | ".join(cells) + f" | {'; '.join(notes)} |\n"

    out_md = RESULTS_DIR / f"{args.out}.md"
    out_md.write_text(md + "\nPer-case results:\n" + matrix, encoding="utf-8")
    (RESULTS_DIR / f"{args.out}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(md)
    print(matrix)
    print(f"Wrote {out_md.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
