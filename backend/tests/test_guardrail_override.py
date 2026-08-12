"""
test_guardrail_override.py
---------------------------
The 15 end-to-end cases in test_cases.json never exercise
policy_engine.validate_or_override()'s override branch, because
RuleBasedBrain's proposals are derived from the same policy logic by
construction -- it never disagrees with itself. That's expected for the
offline planner, but it means the safety net itself needs its own direct
test: this file proves that if a planner (e.g. a live LLM having a bad day)
proposes a decision that conflicts with policy, the deterministic guardrail
overrides it rather than trusting it.

Usage:
    cd backend
    python3 tests/test_guardrail_override.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import policy_engine

CASES = [
    {
        "name": "prohibited vendor: planner proposes 'approve', policy must reject",
        "proposed": "approve",
        "request": {"vendor_name": "BlockedSoft", "product": "SyncNow", "cost": 5000,
                     "intended_use": "x", "data_type": "internal"},
        "evidence_view": {
            "vendor_risk": {"attempted": True, "exhausted": False,
                              "record": {"source_id": "RISK-003", "status": "prohibited",
                                          "risk_rating": "high", "is_current": True}},
            "security_assessment": {"attempted": False, "exhausted": False, "record": None},
            "refresh_attempted": False, "refresh_exhausted": False, "refresh_found_current": False,
            "conflicting_tier2": False,
        },
        "expected": "reject",
    },
    {
        "name": "restricted data: planner proposes 'approve', policy must escalate",
        "proposed": "approve",
        "request": {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": 3000,
                     "intended_use": "x", "data_type": "restricted"},
        "evidence_view": {
            "vendor_risk": {"attempted": False, "exhausted": False, "record": None},
            "security_assessment": {"attempted": False, "exhausted": False, "record": None},
            "refresh_attempted": False, "refresh_exhausted": False, "refresh_found_current": False,
            "conflicting_tier2": False,
        },
        "expected": "escalate",
    },
    {
        "name": "planner tries to finish with no evidence at all: policy must escalate, not approve",
        "proposed": "approve",
        "request": {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": 3000,
                     "intended_use": "x", "data_type": "internal"},
        "evidence_view": {
            "vendor_risk": {"attempted": False, "exhausted": False, "record": None},
            "security_assessment": {"attempted": False, "exhausted": False, "record": None},
            "refresh_attempted": False, "refresh_exhausted": False, "refresh_found_current": False,
            "conflicting_tier2": False,
        },
        "expected": "escalate",
    },
]


def main() -> int:
    all_ok = True
    for case in CASES:
        decision, reasons, citations, overridden = policy_engine.validate_or_override(
            proposed_decision=case["proposed"], request=case["request"],
            evidence_view=case["evidence_view"], evaluation_date="2026-08-01")
        ok = decision == case["expected"]
        all_ok &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['name']}")
        print(f"      proposed={case['proposed']!r} -> final={decision!r} overridden={overridden}")
        if not ok:
            print(f"      expected {case['expected']!r}")
    print("\nAll guardrail-override checks passed." if all_ok else "\nSOME CHECKS FAILED.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
