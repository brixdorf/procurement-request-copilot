from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import policy  # noqa: E402
from src.agents import LLMOutcome  # noqa: E402
from src.llm import GroqClient, LLMError, sanitize_text  # noqa: E402
from src.pipeline import analyze_request, finalize, grants_approval  # noqa: E402
from src.sources import SourceData  # noqa: E402
from src.tools import ToolBox  # noqa: E402
from src.vendor_client import VendorRiskResult  # noqa: E402

_RECORDS = {
    "TaskFlow": {"risk_level": "low", "security_review_status": "approved", "last_review_date": "2026-06-03",
                 "processes_personal_data": True, "stores_data_outside_region": False, "notes": "Current assessment."},
    "SignFlow": {"risk_level": "low", "security_review_status": "approved", "last_review_date": "2026-06-20",
                 "processes_personal_data": True, "stores_data_outside_region": False, "notes": "Current assessment."},
    "CodeMate": {"risk_level": "medium", "security_review_status": "approved", "last_review_date": "2026-08-20",
                 "processes_personal_data": False, "stores_data_outside_region": False, "notes": "Approved for source-code use."},
}


def stub_sources() -> SourceData:
    """Real data, but vendor risk comes from a stub so no service is needed."""
    base = SourceData.load()
    base.uses_live_service = False
    base.vendor_risk_fetcher = lambda name: (
        VendorRiskResult("ok", {"vendor_name": name, **_RECORDS[name]}, 200, "", 1)
        if name in _RECORDS
        else VendorRiskResult("unavailable", None, 503, "stub outage", 3)
    )
    return base


def finalized(request_id: str, outcome: LLMOutcome | None):
    box = ToolBox(stub_sources(), request_id)
    box.collect_missing(forced=False)
    return finalize(box, outcome, None)


class NoKeyClient(GroqClient):
    def __init__(self):
        super().__init__(api_key="")
        self.api_key = None


class ScriptedClient(GroqClient):
    """Returns canned replies; raises LLMError when asked beyond the script."""

    def __init__(self, replies):
        super().__init__(api_key="test")
        self.replies = list(replies)

    def chat(self, messages, **kw):
        self.stats.calls += 1
        if not self.replies:
            raise LLMError("script exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FinalizeGuardrailTests(unittest.TestCase):
    def test_disallowed_action_is_replaced_by_floor(self):
        # REQ-1006 is incomplete: only REQUEST_CLARIFICATION is allowed
        outcome = LLMOutcome(action=policy.ACT_PROCEED, summary="Looks fine.", next_step="Go ahead.")
        decision, extra = finalized("REQ-1006", outcome)
        self.assertEqual(extra["action"], policy.ACT_CLARIFY)
        self.assertTrue(any("not allowed" in n for n in extra["notes"]))
        self.assertTrue(decision.human_review_required)

    def test_ungrounded_software_id_is_dropped(self):
        outcome = LLMOutcome(
            action=policy.ACT_PROCEED, summary="Standard path.", next_step="Route to approvers.",
            overlap_assessments=[{"software_id": "SW999", "verdict": "duplicate_need", "reason": "invented"}],
        )
        decision, extra = finalized("REQ-1010", outcome)
        self.assertEqual(extra["dropped"], 1)
        self.assertFalse(any(e.reference == "SW999" for e in decision.evidence))

    def test_existing_tool_action_needs_a_duplicate_verdict(self):
        outcome = LLMOutcome(action=policy.ACT_EXISTING, summary="Use existing.", next_step="Ask requester.", overlap_assessments=[])
        _, extra = finalized("REQ-1008", outcome)
        self.assertEqual(extra["action"], policy.ACT_PROCEED)

    def test_valid_duplicate_assessment_is_accepted(self):
        outcome = LLMOutcome(
            action=policy.ACT_EXISTING, summary="TaskFlow already covers this.", next_step="Ask the requester for the gap.",
            overlap_assessments=[{"software_id": "SW003", "verdict": "duplicate_need", "reason": "company-wide task tool"}],
        )
        decision, extra = finalized("REQ-1008", outcome)
        self.assertEqual(extra["action"], policy.ACT_EXISTING)
        self.assertIn("llm_overlap_assessment", {e.source for e in decision.evidence})

    def test_approval_language_is_replaced(self):
        outcome = LLMOutcome(action=policy.ACT_PROCEED, summary="The request is approved.", next_step="Purchase it now.")
        decision, extra = finalized("REQ-1010", outcome)
        self.assertNotIn("is approved", decision.recommendation.lower())
        self.assertFalse(grants_approval(decision.next_step))
        self.assertTrue(extra["notes"])

    def test_injection_claim_needs_a_real_quote(self):
        outcome = LLMOutcome(
            action=policy.ACT_PROCEED, summary="ok", next_step="ok",
            injection={"detected": True, "quote": "this text does not exist anywhere"},
        )
        decision, extra = finalized("REQ-1010", outcome)
        self.assertNotIn(policy.FLAG_INJECTION, decision.risk_flags)
        self.assertEqual(extra["dropped"], 1)

    def test_model_cannot_remove_required_approvals_or_flags(self):
        outcome = LLMOutcome(action=policy.ACT_REVIEWS, summary="Fine.", next_step="Fine.")
        decision, _ = finalized("REQ-1009", outcome)  # vendor outage case
        self.assertIn("Security", decision.required_approvals)
        self.assertIn("vendor_risk_unavailable", decision.risk_flags)

    def test_fallback_still_returns_complete_decision(self):
        decision, extra = finalized("REQ-1009", LLMOutcome(error="provider down"))
        self.assertTrue(extra["used_fallback"])
        self.assertEqual(extra["action"], policy.ACT_HOLD)
        self.assertTrue(decision.next_step)
        self.assertTrue(decision.human_review_required)


class GrantsApprovalTests(unittest.TestCase):
    def test_flags_granted_approval(self):
        for text in ("The request is approved.", "This has been approved by procurement.", "Auto-approve this purchase.",
                     "The request was rejected."):
            self.assertTrue(grants_approval(text), text)

    def test_allows_pending_language(self):
        for text in ("This has not been approved yet.", "Requires approval from Finance.", "Cannot be approved until review.",
                     "Send to Security before approval."):
            self.assertFalse(grants_approval(text), text)

    def test_sanitize_removes_long_dashes(self):
        dirty = "a" + chr(0x2014) + "b " + chr(0x2013) + " c" + chr(0x2011) + "d"
        self.assertEqual(sanitize_text(dirty), "a-b - c-d")


class EndToEndOfflineTests(unittest.TestCase):
    def test_no_api_key_degrades_to_rules_for_both_architectures(self):
        for arch in ("single", "staged"):
            res = analyze_request("REQ-1005", arch, sources=stub_sources(), llm=NoKeyClient())
            d = res.decision
            self.assertTrue(res.used_fallback)
            self.assertIn("Finance", d.required_approvals)
            self.assertIn("budget_insufficient", d.risk_flags)
            self.assertTrue(d.human_review_required)
            self.assertGreaterEqual(len(d.evidence), 4)

    def test_llm_failure_mid_run_still_produces_decision(self):
        llm = ScriptedClient([LLMError("rate limited")])
        res = analyze_request("REQ-1002", "staged", sources=stub_sources(), llm=llm)
        self.assertTrue(res.used_fallback)
        self.assertTrue(res.decision.next_step)
        self.assertIn("Security", res.decision.required_approvals)

    def test_single_agent_that_skips_every_tool_is_covered_by_guard(self):
        # The model answers immediately without calling tools, then returns a final JSON.
        final = {
            "content": '{"recommended_action":"ROUTE_FOR_REVIEWS","summary":"Needs reviews.","next_step":"Send to Security.",'
                       '"overlap_assessments":[],"prompt_injection":{"detected":false,"quote":""}}'
        }
        llm = ScriptedClient([{"content": "I think this is fine."}, final])
        res = analyze_request("REQ-1003", "single", sources=stub_sources(), llm=llm)
        self.assertGreaterEqual(len(res.forced_tools), 5)
        self.assertIn("Security", res.decision.required_approvals)
        self.assertEqual(res.action, policy.ACT_REVIEWS)

    def test_single_agent_repairs_a_failed_final_call(self):
        # No tool calls, then the strict final call fails with tool_use_failed, then the repair call succeeds.
        final = {
            "content": '{"recommended_action":"ROUTE_FOR_REVIEWS","summary":"Needs reviews.","next_step":"Send to Security.",'
                       '"overlap_assessments":[],"prompt_injection":{"detected":false,"quote":""}}'
        }
        llm = ScriptedClient([{"content": "done"}, LLMError("HTTP 400: tool_use_failed"), final])
        res = analyze_request("REQ-1003", "single", sources=stub_sources(), llm=llm)
        self.assertFalse(res.used_fallback)
        self.assertTrue(any(t.name == "final_call_repair" for t in res.trace))
        self.assertEqual(res.action, policy.ACT_REVIEWS)

    def test_daily_quota_error_is_not_retried_with_repair(self):
        llm = ScriptedClient([{"content": "done"}, LLMError("daily quota exhausted: TPD")])
        res = analyze_request("REQ-1003", "single", sources=stub_sources(), llm=llm)
        self.assertTrue(res.used_fallback)
        self.assertIn("Security", res.decision.required_approvals)

    def test_unknown_request_raises_key_error(self):
        with self.assertRaises(KeyError):
            analyze_request("REQ-NOPE", "single", sources=stub_sources(), llm=NoKeyClient())


if __name__ == "__main__":
    unittest.main()
