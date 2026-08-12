"""
test_api_e2e.py
----------------
End-to-end tests through the HTTP layer -- the path the frontend actually
uses. Everything else in tests/ drives the orchestrator directly, so nothing
covered app.py: request parsing, response shape, the trace contract the
frontend renders against, ledger idempotency across two HTTP calls, or the
provider-outage status mapping.

Runs against the deterministic offline planner (AGENT_MODE is forced to
rule_based before app is imported, so the suite is free, fast and repeatable).
For live-provider coverage see test_live_e2e.py.

Usage:
    cd backend
    python3 tests/test_api_e2e.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must happen before `import app`: app.py calls load_dotenv(), and .env sets
# AGENT_MODE=llm. load_dotenv does not override an existing os.environ value,
# so setting it here is what keeps this suite offline and deterministic.
os.environ["AGENT_MODE"] = "rule_based"

from fastapi.testclient import TestClient

import app as app_module
from agent import data_store
from agent.llm_brain import ProviderError

client = TestClient(app_module.app)

APPROVE = {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": 8000,
           "intended_use": "team documentation", "data_type": "internal"}
REJECT = {"vendor_name": "BlockedSoft", "product": "SyncNow", "cost": 5000,
          "intended_use": "file sync", "data_type": "internal"}
ESCALATE = {"vendor_name": "DataBridge", "product": "InsightPro", "cost": 9000,
            "intended_use": "analytics", "data_type": "internal"}
NEEDS_INFO = {"vendor_name": "SafeCloud", "product": "TeamDocs", "cost": None,
              "intended_use": "team documentation", "data_type": "internal"}


def assess(request, request_id=None):
    body = dict(request)
    if request_id is not None:
        body["request_id"] = request_id
    return client.post("/api/assess", json=body)


# ---------------------------------------------------------------------------

def test_health_and_frontend_are_served():
    problems = []
    health = client.get("/api/health")
    if health.status_code != 200 or health.json().get("status") != "ok":
        problems.append(f"/api/health returned {health.status_code} {health.text[:120]}")

    root = client.get("/")
    if root.status_code != 200:
        problems.append(f"GET / returned {root.status_code}; the frontend is not being served")
    elif "traceFeed" not in root.text:
        problems.append("GET / did not return the agent UI (no traceFeed element)")
    return problems


def test_brain_info_reports_the_active_planner():
    problems = []
    response = client.get("/api/brain-info")
    if response.status_code != 200:
        return [f"/api/brain-info returned {response.status_code}"]
    body = response.json()
    if "rule_based" not in str(body.get("mode", "")):
        problems.append(f"mode={body.get('mode')!r}; AGENT_MODE=rule_based was not honoured")
    for key in ("anthropic_key_configured", "gemini_key_configured", "forced_mode"):
        if key not in body:
            problems.append(f"/api/brain-info is missing '{key}'")
    return problems


def test_policy_endpoint_returns_the_real_document():
    response = client.get("/api/policy")
    if response.status_code != 200:
        return [f"/api/policy returned {response.status_code}"]
    content = response.json().get("content", "")
    missing = [h for h in ("## Required information", "## Cost", "## Approval",
                            "## Untrusted content") if h not in content]
    return [f"policy document is missing section heading(s): {missing}"] if missing else []


def test_test_cases_endpoint_exposes_the_suite():
    response = client.get("/api/test-cases")
    if response.status_code != 200:
        return [f"/api/test-cases returned {response.status_code}"]
    cases = response.json().get("test_cases", [])
    problems = []
    if len(cases) != 15:
        problems.append(f"{len(cases)} test cases exposed, expected 15")
    for case in cases:
        for key in ("request_id", "request", "expected_decision"):
            if key not in case:
                problems.append(f"case {case.get('request_id', '?')} is missing '{key}'")
    return problems


def test_assess_returns_the_documented_response_shape():
    data_store.reset_decision_log()
    response = assess(APPROVE, "API-SHAPE-01")
    if response.status_code != 200:
        return [f"/api/assess returned {response.status_code}: {response.text[:200]}"]
    body = response.json()

    problems = []
    for key in ("request_id", "decision", "rationale", "citations", "stop_reason",
                 "steps_taken", "brain", "provider_degraded", "trace", "full_state"):
        if key not in body:
            problems.append(f"response is missing '{key}'")
    if body.get("decision") != "approve":
        problems.append(f"decision={body.get('decision')!r}, expected 'approve'")
    if body.get("stop_reason") != "goal_complete":
        problems.append(f"stop_reason={body.get('stop_reason')!r}, expected 'goal_complete'")
    if not body.get("citations"):
        problems.append("an approval was returned with no citations")
    if not (body.get("steps_taken") or 0) > 0:
        problems.append(f"steps_taken={body.get('steps_taken')!r}")
    if not body.get("rationale"):
        problems.append("rationale is empty")

    state = body.get("full_state", {})
    for key in ("evidence", "tool_calls", "guardrail_events", "thoughts", "run_uuid"):
        if key not in state:
            problems.append(f"full_state is missing '{key}'")
    if not state.get("evidence"):
        problems.append("full_state.evidence is empty; an approval must cite collected evidence")
    return problems


def test_assess_reaches_all_four_decisions():
    data_store.reset_decision_log()
    expectations = [("approve", APPROVE), ("reject", REJECT),
                    ("escalate", ESCALATE), ("request_information", NEEDS_INFO)]
    problems = []
    for i, (expected, request) in enumerate(expectations):
        response = assess(request, f"API-DECISION-{i}")
        if response.status_code != 200:
            problems.append(f"{expected}: HTTP {response.status_code}")
            continue
        actual = response.json().get("decision")
        if actual != expected:
            problems.append(f"{request['vendor_name']} ({request['data_type']}, "
                            f"cost={request['cost']}) -> {actual!r}, expected {expected!r}")
    return problems


def test_assess_generates_a_request_id_when_omitted():
    data_store.reset_decision_log()
    response = assess(APPROVE)
    if response.status_code != 200:
        return [f"/api/assess returned {response.status_code}"]
    request_id = response.json().get("request_id", "")
    problems = []
    if not request_id.startswith("WEB-"):
        problems.append(f"generated request_id={request_id!r}, expected a 'WEB-' id")
    if data_store.find_existing_decision(request_id) is None:
        problems.append(f"{request_id} was returned but never written to the ledger")
    return problems


def test_trace_matches_what_the_frontend_renders():
    """index.html reads specific fields per entry type. A trace entry missing
    one renders as 'undefined' in the UI rather than raising anywhere."""
    data_store.reset_decision_log()
    # InjectCorp/confidential exercises every entry type at once: thoughts,
    # multiple tool calls, observations with documents, and a guardrail event.
    response = assess({"vendor_name": "InjectCorp", "product": "HelpDesk AI", "cost": 9000,
                       "intended_use": "support triage", "data_type": "confidential"},
                      "API-TRACE-01")
    if response.status_code != 200:
        return [f"/api/assess returned {response.status_code}"]
    trace = response.json().get("trace", [])

    required = {
        "thought": ["text"],
        "action": ["tool", "args", "attempt", "route_or_variant"],
        "observation": ["tool", "outcome", "observation"],
        "guardrail": ["kind", "detail"],
    }
    problems = []
    seen = set()
    for i, entry in enumerate(trace):
        kind = entry.get("type")
        seen.add(kind)
        if kind not in required:
            problems.append(f"trace[{i}] has unknown type {kind!r}; the UI cannot label it")
            continue
        for field in required[kind]:
            if field not in entry:
                problems.append(f"trace[{i}] ({kind}) is missing '{field}'")

    for kind in ("thought", "action", "observation", "guardrail"):
        if kind not in seen:
            problems.append(f"no {kind!r} entries produced; this case should exercise all four")

    # Every observation must directly follow its own action, or the UI shows a
    # result next to the wrong call.
    for i, entry in enumerate(trace):
        if entry.get("type") == "observation":
            previous = trace[i - 1] if i else {}
            if previous.get("type") != "action" or previous.get("step") != entry.get("step"):
                problems.append(f"trace[{i}] observation for step {entry.get('step')} does not "
                                f"follow its action")
    return problems


def test_resubmitting_a_request_id_is_idempotent():
    data_store.reset_decision_log()
    first = assess(APPROVE, "API-DUPLICATE-01")
    second = assess(APPROVE, "API-DUPLICATE-01")
    problems = []
    if first.status_code != 200 or second.status_code != 200:
        return [f"HTTP {first.status_code}/{second.status_code} on resubmission"]

    if first.json().get("decision") != second.json().get("decision"):
        problems.append(f"resubmission changed the decision: "
                        f"{first.json().get('decision')!r} -> {second.json().get('decision')!r}")

    entries = [r for r in data_store.load_decision_log() if r["request_id"] == "API-DUPLICATE-01"]
    if len(entries) != 1:
        problems.append(f"{len(entries)} ledger entries for one request_id, expected 1")

    kinds = [g["kind"] for g in second.json().get("full_state", {}).get("guardrail_events", [])]
    if "duplicate_prevented" not in kinds:
        problems.append(f"no 'duplicate_prevented' event on resubmission; got {kinds}")
    return problems


def test_decisions_and_reset_endpoints():
    data_store.reset_decision_log()
    assess(APPROVE, "API-LEDGER-01")
    listing = client.get("/api/decisions")
    problems = []
    if listing.status_code != 200:
        problems.append(f"/api/decisions returned {listing.status_code}")
    else:
        ids = [d.get("request_id") for d in listing.json().get("decisions", [])]
        if "API-LEDGER-01" not in ids:
            problems.append(f"assessed request missing from /api/decisions: {ids}")

    reset = client.post("/api/reset")
    if reset.status_code != 200:
        problems.append(f"/api/reset returned {reset.status_code}")
    remaining = client.get("/api/decisions").json().get("decisions", [])
    if remaining:
        problems.append(f"/api/reset left {len(remaining)} entries behind")
    return problems


def test_run_tests_endpoint_passes_every_case():
    response = client.post("/api/run-tests")
    if response.status_code != 200:
        return [f"/api/run-tests returned {response.status_code}: {response.text[:200]}"]
    body = response.json()
    problems = []
    if body.get("total") != 15:
        problems.append(f"total={body.get('total')}, expected 15")
    if body.get("failed"):
        failed = [r["request_id"] for r in body.get("results", []) if not r.get("pass")]
        problems.append(f"{body['failed']} case(s) failed: {failed}")
    if body.get("degraded_to_offline_planner"):
        problems.append(f"{body['degraded_to_offline_planner']} case(s) degraded, "
                        f"but this suite is already offline")
    return problems


def test_malformed_input_is_rejected_at_the_http_boundary():
    """A non-numeric cost never reaches the agent -- pydantic rejects it with
    422. Worth pinning: it is easy to assume the agent's own
    request_information path handles this, and it does not get the chance."""
    response = client.post("/api/assess", json={**APPROVE, "cost": "not-a-number"})
    if response.status_code != 422:
        return [f"non-numeric cost returned {response.status_code}, expected 422"]
    return []


def test_provider_outage_maps_to_a_truthful_status():
    """If a provider failure escapes the orchestrator's fallback, the API must
    report 429/503 with the provider's own message -- not a bare 500."""
    original = app_module.run_agent
    problems = []
    try:
        def raise_quota(*_args, **_kwargs):
            raise ProviderError("Claude is over quota.", status_code=429, retry_after=17, attempts=3)

        app_module.run_agent = raise_quota
        response = assess(APPROVE, "API-OUTAGE-429")
        if response.status_code != 429:
            problems.append(f"quota error returned {response.status_code}, expected 429")
        else:
            body = response.json()
            if body.get("error") != "llm_provider_unavailable":
                problems.append(f"error={body.get('error')!r}")
            if body.get("retry_after_seconds") != 17:
                problems.append(f"retry_after_seconds={body.get('retry_after_seconds')!r}, expected 17")
            if response.headers.get("Retry-After") != "17":
                problems.append(f"Retry-After header={response.headers.get('Retry-After')!r}")
            if "hint" not in body:
                problems.append("no actionable 'hint' in the outage response")

        def raise_outage(*_args, **_kwargs):
            raise ProviderError("Connection reset.", status_code=None, attempts=3)

        app_module.run_agent = raise_outage
        response = assess(APPROVE, "API-OUTAGE-503")
        if response.status_code != 503:
            problems.append(f"transport error returned {response.status_code}, expected 503")
    finally:
        app_module.run_agent = original
    return problems


TESTS = [
    test_health_and_frontend_are_served,
    test_brain_info_reports_the_active_planner,
    test_policy_endpoint_returns_the_real_document,
    test_test_cases_endpoint_exposes_the_suite,
    test_assess_returns_the_documented_response_shape,
    test_assess_reaches_all_four_decisions,
    test_assess_generates_a_request_id_when_omitted,
    test_trace_matches_what_the_frontend_renders,
    test_resubmitting_a_request_id_is_idempotent,
    test_decisions_and_reset_endpoints,
    test_run_tests_endpoint_passes_every_case,
    test_malformed_input_is_rejected_at_the_http_boundary,
    test_provider_outage_maps_to_a_truthful_status,
]


def main() -> int:
    print(f"HTTP end-to-end against app.py  (planner: {os.environ['AGENT_MODE']})\n")
    failures = 0
    for test in TESTS:
        name = test.__name__.replace("test_", "").replace("_", " ")
        try:
            problems = test() or []
        except Exception as exc:
            problems = [f"{exc.__class__.__name__}: {exc}"]
        if problems:
            failures += 1
            print(f"FAIL  {name}")
            for problem in problems:
                print(f"      - {problem}")
        else:
            print(f"PASS  {name}")

    data_store.reset_decision_log()
    total = len(TESTS)
    print(f"\n{total - failures}/{total} API end-to-end checks passed."
          if failures else f"\nAll {total} API end-to-end checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
