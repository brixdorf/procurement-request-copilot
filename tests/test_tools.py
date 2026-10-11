from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sources import SourceData  # noqa: E402
from src.tools import (  # noqa: E402
    EVIDENCE_TOOLS,
    TOOL_BUDGET,
    TOOL_CATALOG,
    TOOL_POLICY,
    TOOL_REGISTRY,
    TOOL_REQUEST,
    TOOL_RISK,
    ToolBox,
    tool_specs,
)
from src.vendor_client import VendorRiskResult  # noqa: E402


def sources() -> SourceData:
    s = SourceData.load()
    s.uses_live_service = False
    s.vendor_risk_fetcher = lambda name: VendorRiskResult(
        "ok",
        {"vendor_name": name, "risk_level": "low", "security_review_status": "approved", "last_review_date": "2026-06-20",
         "processes_personal_data": False, "stores_data_outside_region": False, "notes": "Current assessment."},
        200, "", 1,
    )
    return s


class ToolBoxTests(unittest.TestCase):
    def test_policy_tool_refuses_without_evidence(self):
        box = ToolBox(sources(), "REQ-1001")
        out = box.call(TOOL_POLICY, {})
        self.assertIn("error", out)
        self.assertEqual(set(out["call_these_tools_first"]), set(EVIDENCE_TOOLS))

    def test_policy_tool_runs_after_evidence(self):
        box = ToolBox(sources(), "REQ-1001")
        for name in EVIDENCE_TOOLS:
            box.call(name, {})
        out = box.call(TOOL_POLICY, {})
        self.assertNotIn("error", out)
        self.assertEqual(out["required_approvals"], ["Manager"])

    def test_off_request_vendor_lookup_does_not_fill_the_official_slot(self):
        box = ToolBox(sources(), "REQ-1001")  # vendor SignFlow
        box.call(TOOL_RISK, {"vendor_name": "TaskFlow"})
        self.assertIsNone(box.state.risk)
        box.call(TOOL_RISK, {"vendor_name": "SignFlow"})
        self.assertIsNotNone(box.state.risk)

    def test_off_request_department_budget_is_not_used(self):
        box = ToolBox(sources(), "REQ-1001")  # Finance requester
        out = box.call(TOOL_BUDGET, {"department": "Marketing"})
        self.assertIn("note", out)
        self.assertFalse(box.state.budget_done)

    def test_request_context_rejects_other_request_ids(self):
        box = ToolBox(sources(), "REQ-1001")
        out = box.call(TOOL_REQUEST, {"request_id": "REQ-1005"})
        self.assertIn("error", out)
        self.assertFalse(box.state.context_done)

    def test_unknown_tool_is_reported_not_raised(self):
        box = ToolBox(sources(), "REQ-1001")
        self.assertIn("error", box.call("delete_budget", {}))

    def test_duplicate_calls_are_flagged(self):
        box = ToolBox(sources(), "REQ-1001")
        box.call(TOOL_CATALOG, {})
        again = box.call(TOOL_CATALOG, {})
        self.assertIn("note", again)

    def test_telemetry_counts_every_call(self):
        box = ToolBox(sources(), "REQ-1001")
        box.collect_missing(forced=False)
        self.assertEqual(box.telemetry.tool_calls, 6)
        self.assertEqual(set(box.telemetry.tool_names), set(EVIDENCE_TOOLS) | {TOOL_POLICY})

    def test_no_tool_can_purchase_or_approve(self):
        names = {s["function"]["name"] for s in tool_specs()}
        self.assertEqual(
            names, {TOOL_REQUEST, TOOL_BUDGET, TOOL_CATALOG, TOOL_REGISTRY, TOOL_RISK, TOOL_POLICY}
        )

    def test_evidence_is_grounded_in_called_tools(self):
        box = ToolBox(sources(), "REQ-1002")
        box.collect_missing(forced=False)
        called = set(box.telemetry.tool_names)
        evidence = box.build_evidence()
        self.assertGreaterEqual(len(evidence), 4)
        for item in evidence:
            self.assertIn(item.source, called)
            self.assertTrue(item.reference)


if __name__ == "__main__":
    unittest.main()
