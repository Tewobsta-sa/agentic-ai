"""
run_tests.py
------------
Batch test runner. Runs every case in test_cases.json through the live
orchestrator, writes one execution log per case to tests/execution_logs/,
and prints/saves a summary used to write the evaluation report.

Usage:
    cd backend
    python3 tests/run_tests.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import data_store
from agent.orchestrator import run as run_agent

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_CASES_PATH = os.path.join(HERE, "test_cases.json")
LOGS_DIR = os.path.join(HERE, "execution_logs")
SUMMARY_PATH = os.path.join(HERE, "test_summary.json")


def main() -> int:
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(TEST_CASES_PATH) as f:
        cases = json.load(f)

    data_store.reset_decision_log()

    results = []
    passed = 0
    category_stats: dict = {}

    print(f"Running {len(cases)} test cases...\n")
    print(f"{'ID':<32} {'Category':<28} {'Expected':<20} {'Actual':<20} {'Result':<6} Steps")
    print("-" * 130)

    for case in cases:
        state = run_agent(case["request"], case["request_id"])
        ok = state.final_decision == case["expected_decision"]
        passed += int(ok)

        cat = case.get("category", "uncategorized")
        cstat = category_stats.setdefault(cat, {"total": 0, "passed": 0})
        cstat["total"] += 1
        cstat["passed"] += int(ok)

        log_record = {
            "request_id": case["request_id"],
            "category": cat,
            "description": case.get("description"),
            "request": case["request"],
            "expected_decision": case["expected_decision"],
            "actual_decision": state.final_decision,
            "pass": ok,
            "stop_reason": state.stop_reason,
            "steps_taken": state.step_count,
            "final_rationale": state.final_rationale,
            "final_citations": state.final_citations,
            "guardrail_events": state.guardrail_events,
            "retry_counts": state.retry_counts,
            "trace": state.to_trace(),
        }
        results.append({k: log_record[k] for k in
                         ("request_id", "category", "expected_decision", "actual_decision", "pass", "steps_taken")})

        log_path = os.path.join(LOGS_DIR, f"{case['request_id']}.json")
        with open(log_path, "w") as f:
            json.dump(log_record, f, indent=2)

        # Also a human-readable .txt trace for quick reading without a JSON viewer
        txt_path = os.path.join(LOGS_DIR, f"{case['request_id']}.txt")
        with open(txt_path, "w") as f:
            f.write(f"REQUEST {case['request_id']}  [{cat}]\n")
            f.write(f"{case.get('description','')}\n")
            f.write(f"Input: {json.dumps(case['request'])}\n")
            f.write("=" * 100 + "\n")
            for entry in state.to_trace():
                if entry["type"] == "thought":
                    f.write(f"THOUGHT: {entry['text']}\n")
                elif entry["type"] == "action":
                    f.write(f"  ACTION  step={entry['step']} tool={entry['tool']} "
                            f"attempt={entry['attempt']} route/variant={entry['route_or_variant']} "
                            f"args={json.dumps(entry['args'])}\n")
                elif entry["type"] == "observation":
                    f.write(f"  OBSERVE step={entry['step']} tool={entry['tool']} "
                            f"outcome={entry['outcome']} -> {json.dumps(entry['observation'])[:300]}\n")
                elif entry["type"] == "guardrail":
                    f.write(f"  GUARDRAIL[{entry['kind']}]: {entry['detail']}\n")
            f.write("=" * 100 + "\n")
            f.write(f"FINAL DECISION: {state.final_decision}  (expected: {case['expected_decision']}, "
                    f"{'PASS' if ok else 'FAIL'})\n")
            f.write(f"STOP REASON: {state.stop_reason}\n")
            f.write(f"RATIONALE: {state.final_rationale}\n")
            f.write(f"CITATIONS: {state.final_citations}\n")

        status = "PASS" if ok else "FAIL"
        print(f"{case['request_id']:<32} {cat:<28} {case['expected_decision']:<20} "
              f"{state.final_decision:<20} {status:<6} {state.step_count}")

    # --- Explicit duplicate-action-prevention check -------------------------
    # Re-submit the same duplicate-prevention request_id a second time and
    # confirm (a) it returns the same decision and (b) the decision log does
    # not grow a second entry for it.
    dup_case = next((c for c in cases if c.get("category") == "duplicate_action_prevention"), None)
    dup_check = None
    if dup_case is not None:
        before_count = len([r for r in data_store.load_decision_log()
                             if r["request_id"] == dup_case["request_id"]])
        state2 = run_agent(dup_case["request"], dup_case["request_id"])
        after_count = len([r for r in data_store.load_decision_log()
                            if r["request_id"] == dup_case["request_id"]])
        dup_check = {
            "request_id": dup_case["request_id"],
            "first_submission_decision": dup_case["expected_decision"],
            "second_submission_decision": state2.final_decision,
            "log_entries_before_resubmit": before_count,
            "log_entries_after_resubmit": after_count,
            "no_duplicate_written": before_count == after_count == 1,
            "second_submission_guardrail_events": state2.guardrail_events,
        }
        print(f"\nDuplicate-resubmission check for {dup_case['request_id']}: "
              f"log entries before={before_count} after={after_count} "
              f"-> {'PASS' if dup_check['no_duplicate_written'] else 'FAIL'}")

    total = len(cases)
    success_rate = round(passed / total, 4) if total else 0.0

    summary = {
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "success_rate": success_rate,
        "by_category": category_stats,
        "duplicate_resubmission_check": dup_check,
        "results": results,
    }
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 60)
    print(f"TOTAL: {passed}/{total} passed  ({success_rate*100:.1f}% success rate)")
    print("By category:")
    for cat, s in category_stats.items():
        print(f"  {cat:<28} {s['passed']}/{s['total']}")
    print(f"\nExecution logs written to: {LOGS_DIR}")
    print(f"Summary written to: {SUMMARY_PATH}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
