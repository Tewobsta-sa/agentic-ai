"""
test_planner_contract.py
-------------------------
Regression tests for the LLM planner path, written against the exact run that
exposed them (SafeCloud / TeamDocs, internal, 8000):

    planner -> retrieve_policy(data_type="internal")   REJECTED, step burned
    planner -> retrieve_policy(data_type="internal")   REJECTED, step burned
    planner -> lookup_vendor_risk(vendor_name="SafeCloud")   success
    planner -> unparsable output -> fail-safe "escalate"
    RECORDED: approve

The last line is the bug: a planner that crashed produced an autonomous
approval, because the fail-safe escalate was handed to the guardrail, which
recomputed "approve" from the evidence on file and overrode it.

RuleBasedBrain cannot exercise any of this -- it never emits a malformed
proposal by construction -- so these cases drive the orchestrator with a
scripted brain that misbehaves on purpose.

Usage:
    cd backend
    python3 tests/test_planner_contract.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import data_store, llm_brain, orchestrator, tools
from agent.llm_brain import build_planner_payload

SAFECLOUD = {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": 8000,
             "intended_use": "team documentation", "data_type": "internal"}


class ScriptedBrain:
    """Replays a fixed list of proposals and records every payload it was
    given, so tests can assert on what the planner would actually have seen."""

    name = "scripted (test)"

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.payloads = []

    def propose_action(self, state, evidence_view):
        self.payloads.append(build_planner_payload(state, evidence_view))
        if self.proposals:
            return self.proposals.pop(0)
        return {"thought": "script exhausted", "action": "final_decision", "decision": "escalate"}


def run_scripted(proposals, request, request_id, max_steps=10):
    brain = ScriptedBrain(proposals)
    original = orchestrator.make_brain
    orchestrator.make_brain = lambda: brain
    try:
        state = orchestrator.run(request, request_id, max_steps=max_steps)
    finally:
        orchestrator.make_brain = original
    return state, brain


def kinds(state):
    return [event["kind"] for event in state.guardrail_events]


# ---------------------------------------------------------------------------

def test_unparsable_output_cannot_become_an_approval():
    """The headline bug. Same four steps as the real run; must end escalate."""
    state, _ = run_scripted([
        {"thought": "Need the governing policy first.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"data_type": "internal"}},
        {"thought": "Policy must be retrieved first.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"data_type": "internal"}},
        {"thought": "Vendor risk record is the next required evidence.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "SafeCloud"}},
        llm_brain._unparsable("Planner output could not be parsed as JSON."),
    ], SAFECLOUD, "TEST-UNPARSABLE")

    problems = []
    if state.final_decision != "escalate":
        problems.append(f"final_decision={state.final_decision!r}, expected 'escalate' "
                        f"(a crashed planner must not record a decision)")
    if state.stop_reason != "planner_unusable":
        problems.append(f"stop_reason={state.stop_reason!r}, expected 'planner_unusable'")
    if "failed_safe" not in kinds(state):
        problems.append(f"no 'failed_safe' guardrail event; got {kinds(state)}")
    recorded = data_store.find_existing_decision("TEST-UNPARSABLE")
    if recorded is None or recorded["decision"] != "escalate":
        problems.append(f"ledger recorded {recorded and recorded['decision']!r}, expected 'escalate'")
    return problems


def test_invalid_decision_value_is_not_silently_replaced():
    """A planner proposing a nonsense decision is a broken planner, not an
    invitation to substitute the policy verdict and call the run complete."""
    state, _ = run_scripted([
        {"thought": "Getting risk.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "SafeCloud", "route": "primary"}},
        {"thought": "Done.", "action": "final_decision", "decision": "definitely_fine"},
    ], SAFECLOUD, "TEST-BAD-DECISION")

    problems = []
    if state.final_decision != "escalate":
        problems.append(f"final_decision={state.final_decision!r}, expected 'escalate'")
    if "invalid_decision_value" not in kinds(state):
        problems.append(f"no 'invalid_decision_value' event; got {kinds(state)}")
    return problems


def test_validator_feedback_reaches_the_planner():
    """The Observe half of the loop. A planner whose call was rejected must be
    told, or it re-issues the same call until the step budget is gone."""
    _, brain = run_scripted([
        {"thought": "Try a bad arg.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"data_type": "internal"}},
        {"thought": "Fixed.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "SafeCloud"}},
        {"thought": "Done.", "action": "final_decision", "decision": "approve"},
    ], SAFECLOUD, "TEST-FEEDBACK")

    problems = []
    second = brain.payloads[1] if len(brain.payloads) > 1 else {}
    feedback = json.dumps(second.get("runtime_feedback", []))
    if "data_type" not in feedback:
        problems.append(f"turn 2 runtime_feedback did not mention the rejected argument: {feedback}")
    if "unexpected argument" not in feedback:
        problems.append("turn 2 runtime_feedback did not explain the rejection")

    third = brain.payloads[2] if len(brain.payloads) > 2 else {}
    calls = third.get("recent_tool_calls", [])
    if not calls or calls[-1].get("tool") != "lookup_vendor_risk":
        problems.append(f"turn 3 recent_tool_calls missing the successful lookup: {calls}")
    elif calls[-1].get("outcome") != "success":
        problems.append(f"turn 3 saw outcome={calls[-1].get('outcome')!r}, expected 'success'")

    for i, payload in enumerate(brain.payloads):
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            problems.append(f"payload {i} is not JSON-serialisable: {exc}")
    return problems


def test_planner_cannot_write_to_the_ledger():
    """record_final_decision reaches the ledger without passing through
    validate_or_override, and the idempotency guard would then make that
    unvalidated write win. It must not be planner-callable."""
    state, _ = run_scripted([
        {"thought": "Approving directly.", "action": "tool_call", "tool": "record_final_decision",
         "args": {"request_id": "TEST-DIRECT-WRITE", "decision": "approve"}},
        {"thought": "Getting risk.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "BlockedSoft", "product": "SyncNow"}},
        {"thought": "Done.", "action": "final_decision", "decision": "reject"},
    ], {"vendor_name": "BlockedSoft", "product": "SyncNow", "cost": 5000,
        "intended_use": "x", "data_type": "internal"}, "TEST-DIRECT-WRITE")

    problems = []
    if "record_final_decision" in orchestrator.TOOL_SCHEMAS:
        problems.append("record_final_decision is still in the planner's callable tool set")
    if "tool_validation_failed" not in kinds(state):
        problems.append(f"direct ledger write was not rejected; got {kinds(state)}")
    if state.final_decision != "reject":
        problems.append(f"final_decision={state.final_decision!r}, expected 'reject' from policy")
    recorded = data_store.find_existing_decision("TEST-DIRECT-WRITE")
    if recorded is None or recorded["decision"] != "reject":
        problems.append(f"ledger holds {recorded and recorded['decision']!r}; the planner's "
                        f"unvalidated 'approve' must never have been written")
    return problems


def test_prompt_advertises_the_enforced_schema():
    """The root cause: the prompt promised 'exactly the argument schema given'
    and gave only tool names, so the planner guessed argument names."""
    prompt = llm_brain.SYSTEM_PROMPT_CONTRACT
    problems = []
    for name, spec in tools.TOOL_SPECS.items():
        if not spec.get("planner_callable", True):
            if name in prompt:
                problems.append(f"runtime-only tool {name} is offered to the planner")
            continue
        if name not in prompt:
            problems.append(f"tool {name} missing from the prompt")
        for arg in spec["args"]:
            if arg not in prompt:
                problems.append(f"argument {name}.{arg} missing from the prompt")

    for name, schema in orchestrator.TOOL_SCHEMAS.items():
        spec_args = set(tools.TOOL_SPECS[name]["args"])
        if (schema["required"] | schema["optional"]) != spec_args:
            problems.append(f"validator and contract disagree on {name}'s arguments")
    return problems


def test_trace_is_chronological():
    """Rejections must render where they happened, not in a block at the end."""
    state, _ = run_scripted([
        {"thought": "Bad arg.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"data_type": "internal"}},
        {"thought": "Fixed.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "SafeCloud"}},
        {"thought": "Done.", "action": "final_decision", "decision": "approve"},
    ], SAFECLOUD, "TEST-TRACE-ORDER")

    trace = state.to_trace()
    problems = []
    rejection = next((i for i, e in enumerate(trace)
                      if e["type"] == "guardrail" and e["kind"] == "tool_validation_failed"), None)
    lookup = next((i for i, e in enumerate(trace)
                   if e["type"] == "action" and e["tool"] == "lookup_vendor_risk"), None)
    if rejection is None or lookup is None:
        problems.append(f"expected both a rejection and a lookup in the trace: "
                        f"{[e['type'] for e in trace]}")
    elif rejection > lookup:
        problems.append("the rejected call renders after the call that followed it")

    for entry in trace:
        if entry["type"] == "observation":
            action_at = trace.index(entry) - 1
            if trace[action_at]["type"] != "action" or trace[action_at]["step"] != entry["step"]:
                problems.append(f"observation for step {entry['step']} is detached from its action")
    return problems


def test_distinct_policy_sections_are_not_treated_as_retries():
    """Two different sections are two needs, not a repeated route."""
    state, _ = run_scripted([
        {"thought": "Cost rules.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"section": "cost"}},
        {"thought": "Approval rules.", "action": "tool_call",
         "tool": "retrieve_policy", "args": {"section": "approval"}},
        {"thought": "Risk.", "action": "tool_call",
         "tool": "lookup_vendor_risk", "args": {"vendor_name": "SafeCloud"}},
        {"thought": "Done.", "action": "final_decision", "decision": "approve"},
    ], SAFECLOUD, "TEST-POLICY-SECTIONS")

    problems = []
    policy_calls = [tc for tc in state.tool_calls if tc.tool == "retrieve_policy"]
    if len(policy_calls) != 2:
        problems.append(f"{len(policy_calls)} retrieve_policy call(s) executed, expected 2")
    if "repeated_route_rejected" in kinds(state):
        problems.append("a different policy section was rejected as a repeated route")
    if state.final_decision != "approve":
        problems.append(f"final_decision={state.final_decision!r}, expected 'approve'")
    return problems


TESTS = [
    test_unparsable_output_cannot_become_an_approval,
    test_invalid_decision_value_is_not_silently_replaced,
    test_validator_feedback_reaches_the_planner,
    test_planner_cannot_write_to_the_ledger,
    test_prompt_advertises_the_enforced_schema,
    test_trace_is_chronological,
    test_distinct_policy_sections_are_not_treated_as_retries,
]


def main() -> int:
    data_store.reset_decision_log()
    failures = 0
    for test in TESTS:
        name = test.__name__.replace("test_", "").replace("_", " ")
        try:
            problems = test() or []
        except Exception as exc:  # a raising test is a failing test, not a crash
            problems = [f"{exc.__class__.__name__}: {exc}"]
        if problems:
            failures += 1
            print(f"FAIL  {name}")
            for problem in problems:
                print(f"      - {problem}")
        else:
            print(f"PASS  {name}")

    data_store.reset_decision_log()
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} planner-contract checks passed."
          if failures else "\nAll planner-contract checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
