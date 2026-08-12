"""
test_live_e2e.py
-----------------
End-to-end verification against a REAL LLM provider.

Everything else in tests/ runs on the deterministic offline planner, which by
construction never invents a tool argument, never emits unparsable output and
never loops -- so it cannot tell you whether the live path works. This suite
can. It runs the real cases through the real provider and checks two separate
things:

  1. DECISIONS  -- did each case reach its expected decision?
  2. PLANNER HEALTH -- did it get there cleanly, or did the runtime have to
     catch it? Every failure mode from the original bug report is a hard
     assertion here: invented arguments (tool_validation_failed), unparsable
     output (planner_output_unusable), repeated identical calls burning the
     step budget (max_steps_reached), and the fail-safe firing at all.

A passing decision is NOT sufficient. The guardrail is good enough that a
completely broken planner can still produce correct decisions -- that is
exactly what happened in the run that prompted these fixes. So a case that
silently degraded to the offline planner is reported as NOT live-verified,
and a suite where nothing ran live fails rather than printing a false green.

This spends real API calls: roughly 3-5 per case.

Usage:
    cd backend
    python3 tests/test_live_e2e.py                  # all cases
    python3 tests/test_live_e2e.py --limit 3        # first 3 cases only
    python3 tests/test_live_e2e.py --only TC-01 --only TC-11
    python3 tests/test_live_e2e.py --mode gemini
    python3 tests/test_live_e2e.py --list           # no API calls
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from agent import data_store, llm_brain
from agent.orchestrator import run as run_agent

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_CASES_PATH = os.path.join(HERE, "test_cases.json")
REPORT_PATH = os.path.join(HERE, "live_report.json")

# Guardrail events that mean the PLANNER misbehaved and the runtime had to
# catch it. Any of these in a live run is a regression of the fixes.
PLANNER_FAULTS = {
    "tool_validation_failed": "proposed a tool/argument the runtime rejects",
    "planner_output_unusable": "returned output that could not be parsed as a plan",
    "invalid_decision_value": "proposed a decision outside the allowed set",
    "invalid_planner_output": "returned something that was not a plan object",
    "invalid_action": "returned an unrecognised action",
    "failed_safe": "run did not complete; escalation was forced",
    "max_steps_reached": "burned the whole step budget without deciding",
}

# Events that are the system working correctly, not planner faults. The
# offline planner produces these too (see tests/execution_logs).
BENIGN_EVENTS = {
    "repeated_route_rejected", "retry_budget_exceeded", "prompt_injection_detected",
    "duplicate_prevented", "decision_overridden", "llm_provider_unavailable",
}


def probe_forced_tool_use(mode: str):
    """Confirm the endpoint honours tool_choice before spending 60 calls on it.

    This matters more than it looks: ANTHROPIC_BASE_URL may point at a proxy
    or router rather than the Anthropic API, and a proxy that silently drops
    `tools`/`tool_choice` turns every plan back into free text -- which
    presents as the planner "hallucinating" rather than as a transport
    problem. Isolating it here means one failing check instead of fifteen
    confusing ones.
    """
    if mode == "gemini":
        return ["(skipped: Gemini path uses response_mime_type, not forced tool-use)"], True

    brain = llm_brain.LLMBrain()
    endpoint = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    message = brain.client.messages.create(
        model=brain.model,
        max_tokens=512,
        temperature=0,
        system=llm_brain.SYSTEM_PROMPT_CONTRACT,
        tools=[brain.plan_tool],
        tool_choice={"type": "tool", "name": llm_brain.PLAN_TOOL_NAME},
        messages=[{"role": "user", "content": json.dumps({
            "request": {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": 8000,
                         "intended_use": "docs", "data_type": "internal"},
            "evaluation_date": "2026-08-01",
            "evidence_collected_so_far": {},
            "steps_taken": 0, "max_steps": 10, "retry_counts": {},
            "recent_thoughts": [], "recent_tool_calls": [], "runtime_feedback": [],
        }, indent=2)}],
    )

    notes = [f"endpoint: {endpoint}", f"model: {brain.model}"]
    plan = next((b for b in message.content
                 if getattr(b, "type", None) == "tool_use"
                 and getattr(b, "name", None) == llm_brain.PLAN_TOOL_NAME), None)
    if plan is None:
        types = [getattr(b, "type", "?") for b in message.content]
        notes.append(f"NO tool_use block returned (got {types}); this endpoint does not honour "
                     f"tool_choice, so plans fall back to text parsing")
        return notes, False

    payload = plan.input if isinstance(plan.input, dict) else {}
    notes.append(f"tool_use returned: action={payload.get('action')!r} tool={payload.get('tool')!r} "
                 f"args={json.dumps(payload.get('args', {}))}")
    if "thought" not in payload or "action" not in payload:
        notes.append("plan object is missing a required field")
        return notes, False
    return notes, True


def evaluate_case(case, state):
    """Split verdicts into decision correctness, planner health, and liveness."""
    kinds = [event["kind"] for event in state.guardrail_events]
    faults = [k for k in kinds if k in PLANNER_FAULTS]
    unknown = [k for k in kinds if k not in PLANNER_FAULTS and k not in BENIGN_EVENTS]

    problems = []
    if state.final_decision != case["expected_decision"]:
        problems.append(f"decision {state.final_decision!r}, expected {case['expected_decision']!r}")
    for fault in sorted(set(faults)):
        problems.append(f"planner {PLANNER_FAULTS[fault]} ({fault} x{faults.count(fault)})")
    for kind in sorted(set(unknown)):
        problems.append(f"unclassified guardrail event {kind!r}")

    return {
        "request_id": case["request_id"],
        "category": case.get("category"),
        "expected_decision": case["expected_decision"],
        "actual_decision": state.final_decision,
        "stop_reason": state.stop_reason,
        "steps_taken": state.step_count,
        "brain": state.brain_name,
        "live": not state.provider_degraded,
        "provider_error": state.provider_error,
        "tools_used": [tc.tool for tc in state.tool_calls],
        "guardrail_events": kinds,
        "citations": state.final_citations,
        "problems": problems,
        "pass": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live end-to-end verification.")
    parser.add_argument("--mode", default="llm", choices=["llm", "claude", "gemini"],
                        help="Which live provider to force (default: llm/claude).")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases.")
    parser.add_argument("--only", action="append", default=[],
                        help="Run only cases whose request_id contains this. Repeatable.")
    parser.add_argument("--list", action="store_true", help="List selected cases and exit.")
    parser.add_argument("--stop-on-degrade", action="store_true",
                        help="Abort as soon as a case falls back to the offline planner.")
    args = parser.parse_args()

    with open(TEST_CASES_PATH) as f:
        cases = json.load(f)
    if args.only:
        cases = [c for c in cases if any(o in c["request_id"] for o in args.only)]
    if args.limit is not None:
        cases = cases[:args.limit]

    if not cases:
        print("No cases selected.")
        return 1

    if args.list:
        for case in cases:
            print(f"  {case['request_id']:<42} expect {case['expected_decision']}")
        print(f"\n{len(cases)} case(s) selected; ~{len(cases) * 4} API calls if run.")
        return 0

    key_var = "GEMINI_API_KEY" if args.mode == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_var):
        print(f"SKIP: {key_var} is not set, so there is no live path to verify.")
        print("      Offline coverage: tests/run_tests.py, tests/test_api_e2e.py")
        return 0

    os.environ["AGENT_MODE"] = args.mode

    print(f"Live end-to-end: {len(cases)} case(s) via AGENT_MODE={args.mode}")
    print(f"This spends real API calls (~{len(cases) * 4}).\n")

    print("Provider probe: forced tool-use")
    notes, probe_ok = probe_forced_tool_use(args.mode)
    for note in notes:
        print(f"    {note}")
    print(f"  {'PASS' if probe_ok else 'FAIL'}\n")
    if not probe_ok:
        print("Aborting: the provider will not return schema-shaped plans, so every case "
              "below would fail for the same single reason.")
        return 1

    data_store.reset_decision_log()
    results = []
    print(f"{'ID':<42} {'Expected':<20} {'Actual':<20} {'Steps':<6} {'Live':<5} Result")
    print("-" * 112)

    for case in cases:
        started = time.time()
        try:
            state = run_agent(case["request"], case["request_id"])
        except Exception as exc:
            results.append({
                "request_id": case["request_id"], "expected_decision": case["expected_decision"],
                "actual_decision": None, "live": False, "pass": False,
                "problems": [f"run raised {exc.__class__.__name__}: {exc}"],
            })
            print(f"{case['request_id']:<42} {case['expected_decision']:<20} "
                  f"{'ERROR':<20} {'-':<6} {'-':<5} FAIL")
            continue

        result = evaluate_case(case, state)
        result["elapsed_seconds"] = round(time.time() - started, 1)
        results.append(result)
        print(f"{case['request_id']:<42} {case['expected_decision']:<20} "
              f"{str(result['actual_decision']):<20} {result['steps_taken']:<6} "
              f"{'yes' if result['live'] else 'NO':<5} {'PASS' if result['pass'] else 'FAIL'}")
        for problem in result["problems"]:
            print(f"      - {problem}")

        if not result["live"]:
            print(f"      ! ran on the offline planner, so this is not live verification: "
                  f"{result.get('provider_error') or 'provider unavailable'}")
            if args.stop_on_degrade:
                print("      aborting on --stop-on-degrade")
                break

    # --- Resubmission is an end-to-end property of the ledger, not the brain --
    duplicate_check = None
    dup_case = next((c for c in cases if c.get("category") == "duplicate_action_prevention"), None)
    if dup_case is not None:
        before = len([r for r in data_store.load_decision_log()
                      if r["request_id"] == dup_case["request_id"]])
        state2 = run_agent(dup_case["request"], dup_case["request_id"])
        after = len([r for r in data_store.load_decision_log()
                     if r["request_id"] == dup_case["request_id"]])
        duplicate_check = {
            "request_id": dup_case["request_id"],
            "second_submission_decision": state2.final_decision,
            "entries_before": before, "entries_after": after,
            "pass": before == after == 1 and state2.final_decision == dup_case["expected_decision"],
        }
        print(f"\nResubmission check ({dup_case['request_id']}): ledger entries "
              f"{before} -> {after}, decision {state2.final_decision} "
              f"-> {'PASS' if duplicate_check['pass'] else 'FAIL'}")

    # --- Summary ---------------------------------------------------------
    passed = sum(1 for r in results if r["pass"])
    live = sum(1 for r in results if r.get("live"))
    faulty = sum(1 for r in results
                 if any(k in PLANNER_FAULTS for k in r.get("guardrail_events", [])))
    overridden = [r["request_id"] for r in results
                  if "decision_overridden" in r.get("guardrail_events", [])]
    all_tools = [t for r in results for t in r.get("tools_used", [])]

    print("\n" + "=" * 60)
    print(f"Decisions      : {passed}/{len(results)} correct")
    print(f"Live-verified  : {live}/{len(results)} ran on the live provider")
    print(f"Planner faults : {faulty} case(s) needed a runtime catch")
    print(f"Tool usage     : " + ", ".join(
        f"{tool} x{all_tools.count(tool)}" for tool in sorted(set(all_tools))) or "none")
    if overridden:
        print(f"Overridden     : {overridden}")
        print("                 (the guardrail worked, but the live planner disagreed with "
              "policy -- worth reading those traces)")
    cooldown = llm_brain._cooldown_remaining()
    if cooldown:
        print(f"Quota cooldown : {cooldown:.0f}s remaining; later cases may have degraded")

    report = {
        "mode": args.mode,
        "model": os.environ.get("CLAUDE_MODEL" if args.mode != "gemini" else "GEMINI_MODEL"),
        "endpoint": os.environ.get("ANTHROPIC_BASE_URL", "default"),
        "cases_run": len(results),
        "decisions_correct": passed,
        "live_verified": live,
        "planner_fault_cases": faulty,
        "duplicate_resubmission_check": duplicate_check,
        "results": results,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to: {REPORT_PATH}")

    failures = []
    if passed != len(results):
        failures.append(f"{len(results) - passed} case(s) reached the wrong decision")
    if faulty:
        failures.append(f"{faulty} case(s) required a runtime catch of planner output")
    if live == 0:
        failures.append("nothing ran on the live provider, so this run verified nothing live")
    elif live < len(results):
        failures.append(f"{len(results) - live} case(s) degraded to the offline planner and are "
                        f"therefore unverified")
    if duplicate_check is not None and not duplicate_check["pass"]:
        failures.append("resubmission was not idempotent")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nLive end-to-end verification passed: correct decisions, reached cleanly, "
          "on the live provider.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
