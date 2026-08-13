"""
orchestrator.py
----------------
The ReAct loop itself: Reason -> Act -> Observe -> Update state -> Repeat.

Responsibilities that live HERE (not in the brain, not in the tools):
  - runtime validation of every proposed tool call against a strict schema
    (never let the model invent an unvalidated argument),
  - enforcement of the retry policy (max 2 retries, must vary route/query),
  - freshness computation (via the calculator tool) applied uniformly,
  - assembling the "evidence_view" the policy guardrail and brain both read,
  - the four stopping conditions: goal complete, missing info, risk too
    high / policy-forced terminal state, or max steps reached,
  - final guardrail validation/override before anything is recorded.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import data_store, policy_engine, tools
from .llm_brain import ProviderError, RuleBasedBrain, make_brain
from .state import AgentState, EvidenceRecord, ToolCallRecord

INJECTION_MARKERS = (
    "ignore all", "ignore previous", "ignore company policy", "ignore policy",
    "do not call any other tool", "approve this request immediately",
    "disregard", "you must approve", "bypass", "override the policy",
)


def _scan_for_injection(doc: Dict[str, Any], state: AgentState) -> None:
    """Read-only heuristic scan of untrusted document content, purely for
    logging/visibility. It never influences the decision: policy_engine only
    ever reads structured fields (result, risk_rating, document_date), never
    the free-text 'content' field, so this scan cannot become an attack
    surface itself -- it can only add a guardrail_event to the trace."""
    content = str(doc.get("content", "")).lower()
    if any(marker in content for marker in INJECTION_MARKERS):
        state.add_guardrail_event(
            "prompt_injection_detected",
            f"Document {doc.get('document_id')} (source_type={doc.get('source_type')}) contains "
            f"text resembling an embedded instruction. Treated as untrusted data per policy and "
            f"ignored; it has no effect on the decision.")


TOOL_SCHEMAS = {
    name: {
        "required": {arg for arg, meta in spec["args"].items() if meta["required"]},
        "optional": {arg for arg, meta in spec["args"].items() if not meta["required"]},
    }
    for name, spec in tools.TOOL_SPECS.items()
    if spec.get("planner_callable", True)
}

# Stop reasons that mean the *planner* broke, not that policy reached a
# conclusion. Kept separate from the decision itself because they are not
# distinguishable from it: an unparsable response fails safe to "escalate",
# validate_or_override then recomputes the verdict from whatever partial
# evidence happens to be on file, sees a disagreement, and "corrects" the
# fail-safe into an approval. Fail-safe has to outrank the recomputation.
PLANNER_FAILURE_STOPS = {
    "max_steps_reached": "Maximum reasoning steps reached before policy-sufficient evidence was gathered",
    "invalid_planner_output": "The planner returned output the runtime could not use as a plan",
    "planner_unusable": "The planner failed to produce a usable plan",
}


class ToolValidationError(Exception):
    pass


def _is_blank(value: Any) -> bool:
    """A required argument that is present but None or empty is exactly as
    unusable as one that is absent -- treat both as missing here, where the
    loop can recover, rather than letting it surface as a KeyError or a
    TypeError deep inside _execute_tool."""
    return value is None or (isinstance(value, str) and not value.strip())


def _validate_tool_call(tool_name: Any, args: Dict[str, Any]) -> None:
    if not isinstance(tool_name, str) or tool_name not in TOOL_SCHEMAS:
        # Distinguish "no such tool" from "that tool exists but is not yours to
        # call", so the feedback the planner reads next turn is actionable.
        if isinstance(tool_name, str) and tool_name in tools.TOOL_SPECS:
            raise ToolValidationError(
                f"Tool '{tool_name}' is runtime-only and cannot be called by the planner. "
                f"Return action='final_decision' instead; the runtime records the "
                f"policy-validated decision itself.")
        raise ToolValidationError(f"Unknown tool '{tool_name}'. Available tools: "
                                   f"{sorted(TOOL_SCHEMAS)}.")
    schema = TOOL_SCHEMAS[tool_name]
    unknown = set(args.keys()) - (schema["required"] | schema["optional"])
    if unknown:
        raise ToolValidationError(
            f"Tool '{tool_name}' received unexpected argument(s): {sorted(unknown)}. "
            f"It accepts only: {sorted(schema['required'] | schema['optional'])}.")

    missing = sorted(name for name in schema["required"] if _is_blank(args.get(name)))
    if missing:
        raise ToolValidationError(f"Tool '{tool_name}' is missing required argument(s): {missing}.")

    for arg, value in args.items():
        allowed = tools.TOOL_SPECS[tool_name]["args"][arg].get("enum")
        if allowed and value is not None and value not in allowed:
            raise ToolValidationError(
                f"Tool '{tool_name}' received {arg}='{value}', which is not one of {allowed}.")


def _needs_key(tool_name: str, args: Dict[str, Any]) -> str:
    """A stable key identifying a single 'evidence need' for retry-BUDGET
    accounting (max 3 attempts total, across any route/variant), e.g.
    'lookup_vendor_risk::FailWare'.

    Tools whose calls are genuinely *different needs* rather than retries of
    one need must key on what distinguishes them. Reading two policy sections,
    or dating two different documents, is not retrying -- but with a bare tool
    name as the key both collapsed onto one budget and one route, so the
    second call was rejected as a repeated route. That made retrieve_policy and
    calculate_days_since effectively callable once per run.
    """
    if tool_name == "lookup_vendor_risk":
        return f"lookup_vendor_risk::{args.get('vendor_name')}"
    if tool_name == "search_vendor_documents":
        # One shared retry budget per vendor for document search, regardless
        # of whether a call filters by source_type -- it's the same
        # underlying "need" (find current, relevant vendor documents), and a
        # real search backend wouldn't give a caller a separate quota just
        # because they added a filter.
        return f"search_vendor_documents::{args.get('vendor_name')}"
    if tool_name == "retrieve_policy":
        return f"retrieve_policy::{args.get('section') or 'all'}"
    if tool_name == "calculate_days_since":
        return f"calculate_days_since::{args.get('date_str')}::{args.get('reference_date') or 'default'}"
    if tool_name == "search_web_threat_intel":
        return f"search_web_threat_intel::{args.get('vendor_name')}"
    if tool_name == "lookup_cve_vulnerabilities":
        return f"lookup_cve_vulnerabilities::{args.get('vendor_name')}"
    if tool_name == "check_domain_security":
        return f"check_domain_security::{args.get('domain_or_vendor')}"
    return tool_name


def _route_key(need_key: str, args: Dict[str, Any]) -> str:
    """A finer-grained key that also includes the route/query-variant, used
    purely to number 'attempt' *within that specific route* -- this is what
    the scripted scenarios key their timeouts on (e.g. 'backup, attempt 1'),
    matching how a real system would log a fresh attempt counter per
    route/endpoint rather than a single global counter."""
    route = args.get("route") or args.get("query_variant") or "default"
    return f"{need_key}::{route}"


def _build_evidence_view(state: AgentState) -> Dict[str, Any]:
    view: Dict[str, Any] = {}

    risk_key = f"lookup_vendor_risk::{state.request.get('vendor_name')}"
    risk_attempts = state.retries_used(risk_key)
    risk_evidence = state.evidence_by_fact("vendor_risk")
    risk_record = risk_evidence[-1].value if risk_evidence else None
    view["vendor_risk"] = {
        "attempted": risk_attempts > 0,
        "exhausted": risk_attempts >= tools.MAX_ATTEMPTS_PER_NEED and risk_record is None,
        "record": risk_record,
    }

    doc_key = f"search_vendor_documents::{state.request.get('vendor_name')}"
    doc_attempts = state.retries_used(doc_key)

    refresh_attempts = doc_attempts
    view["refresh_attempted"] = refresh_attempts > 0
    view["refresh_exhausted"] = refresh_attempts >= tools.MAX_ATTEMPTS_PER_NEED
    refresh_docs = state.evidence_by_fact("document_refresh")
    view["refresh_found_current"] = any(e.is_current for e in refresh_docs)

    sec_attempts = doc_attempts
    sec_evidence = state.evidence_by_fact("security_assessment")
    sec_record = sec_evidence[-1].value if sec_evidence else None
    view["security_assessment"] = {
        "attempted": sec_attempts > 0,
        "exhausted": sec_attempts >= tools.MAX_ATTEMPTS_PER_NEED and sec_record is None,
        "record": sec_record,
    }

    web_intel_evidence = state.evidence_by_fact("web_threat_intelligence")
    view["web_threat_intel"] = {
        "attempted": len(web_intel_evidence) > 0,
        "record": web_intel_evidence[-1].value if web_intel_evidence else None,
    }

    # Conflict detection: two current tier-2 sources disagreeing on risk_rating
    conflict = False
    if risk_record and risk_record.get("is_current") and sec_record and sec_record.get("is_current"):
        if risk_record.get("risk_rating") != sec_record.get("risk_rating"):
            conflict = True
    view["conflicting_tier2"] = conflict

    return view


def run(request: Dict[str, Any], request_id: str, max_steps: int = 10) -> AgentState:
    eval_date = data_store.evaluation_date()
    state = AgentState(request=request, request_id=request_id, evaluation_date=eval_date, max_steps=max_steps)
    brain = make_brain()
    state.brain_name = brain.name
    state.add_thought(f"[brain={brain.name}] Starting assessment of request {request_id} "
                       f"({request.get('vendor_name')} / {request.get('product')}).")

    # Lazily-created offline stand-in. A live provider can fail mid-run for
    # reasons that have nothing to do with the request (quota, timeout,
    # outage); an assessment agent that returns HTTP 500 in that case is
    # strictly worse than one that finishes on the deterministic planner and
    # says so in the trace. The guardrail still validates the outcome either
    # way, so degrading cannot weaken the decision -- only the narrative.
    fallback: Optional[RuleBasedBrain] = None

    while not state.done:
        if state.step_count >= state.max_steps:
            state.add_guardrail_event("max_steps_reached",
                                        f"Reached the maximum of {state.max_steps} steps without a final decision.")
            _finalize(state, proposed_decision=None, stop_reason="max_steps_reached")
            break

        evidence_view = _build_evidence_view(state)
        try:
            proposal = brain.propose_action(state, evidence_view)
        except ProviderError as exc:
            state.provider_degraded = True
            state.provider_error = str(exc)
            state.add_guardrail_event(
                "llm_provider_unavailable",
                f"{exc} Falling back to the deterministic offline planner for the rest of this run; "
                f"the policy guardrail is unaffected.")
            fallback = fallback or RuleBasedBrain()
            # Swap for the remainder of the run: if the provider is out of
            # quota it will still be out of quota on the next step, and
            # re-asking once per step would just multiply the latency.
            brain = fallback
            state.brain_name = f"{state.brain_name} -> {fallback.name} (degraded)"
            proposal = brain.propose_action(state, evidence_view)

        if not isinstance(proposal, dict):
            state.add_guardrail_event("invalid_planner_output",
                                        f"Planner returned {type(proposal).__name__}, not an object.")
            _finalize(state, proposed_decision=None, stop_reason="invalid_planner_output")
            break

        thought = proposal.get("thought")
        if isinstance(thought, str) and thought.strip():
            state.add_thought(thought.strip())

        # A fail-safe proposal from the brain (unparsable/unusable model output)
        # must not be routed through the normal final_decision path: down there
        # the guardrail recomputes the verdict and would override the fail-safe
        # with whatever partial evidence implies.
        if proposal.get("planner_failed"):
            state.add_guardrail_event(
                "planner_output_unusable",
                str(proposal.get("rationale") or "Planner produced no usable plan."))
            _finalize(state, proposed_decision=None, stop_reason="planner_unusable")
            break

        action = proposal.get("action")

        if action == "final_decision":
            proposed_decision = proposal.get("decision")
            if proposed_decision not in policy_engine.DECISIONS:
                state.add_guardrail_event(
                    "invalid_decision_value",
                    f"Planner proposed an invalid decision '{proposed_decision}'; treating the "
                    f"plan as unusable rather than substituting a policy verdict for it.")
                _finalize(state, proposed_decision=None, stop_reason="planner_unusable")
                break
            _finalize(state, proposed_decision=proposed_decision, stop_reason="goal_complete")
            break

        if action != "tool_call":
            state.add_guardrail_event("invalid_action", f"Planner returned unrecognized action '{action}'.")
            _finalize(state, proposed_decision=None, stop_reason="invalid_planner_output")
            break

        tool_name = proposal.get("tool")
        raw_args = proposal.get("args") or {}

        try:
            if not isinstance(raw_args, dict):
                raise ToolValidationError(
                    f"Tool '{tool_name}' received 'args' as {type(raw_args).__name__}, expected an object.")
            args = dict(raw_args)
            _validate_tool_call(tool_name, args)
        except ToolValidationError as e:
            state.add_guardrail_event("tool_validation_failed", str(e))
            # Do not let a malformed proposal crash or stall the loop; count it
            # as a wasted step and let the loop continue (brain will get another
            # chance, bounded by max_steps).
            state.step_count += 1
            continue

        key = _needs_key(tool_name, args)
        total_attempt = state.register_attempt(key)
        if total_attempt > tools.MAX_ATTEMPTS_PER_NEED:
            state.add_guardrail_event("retry_budget_exceeded",
                                        f"Retry budget exceeded for '{key}'; not calling the tool again.")
            continue

        route_or_variant = args.get("route") or args.get("query_variant") or "-"
        route_key = _route_key(key, args)
        route_attempt = state.register_attempt(route_key)
        if route_attempt > 1:
            state.add_guardrail_event(
                "repeated_route_rejected",
                f"Planner tried route/variant '{route_or_variant}' for '{key}' more than once; "
                f"policy requires each retry to use a corrected query or a different approach.")
            continue

        # The scripted scenarios key their outcomes on a per-route attempt
        # counter (e.g. "backup, attempt 1"), which is what a real monitoring
        # system would log per endpoint. total_attempt (below) is used only
        # for the overall 3-attempts-per-need retry budget.
        observation, evidence = _execute_tool(tool_name, args, route_attempt, state)
        state.add_tool_call(ToolCallRecord(
            step=state.step_count + 1, tool=tool_name, args=args, attempt=total_attempt,
            route_or_variant=route_or_variant, outcome=observation.get("outcome", "unknown"),
            observation=observation, duration_ms=0,
        ))
        if evidence is not None:
            state.add_evidence(evidence)

    return state


def _execute_tool(tool_name: str, args: Dict[str, Any], attempt: int, state: AgentState):
    eval_date = state.evaluation_date

    if tool_name == "retrieve_policy":
        obs = tools.retrieve_policy(section=args.get("section"))
        return obs, None

    if tool_name == "lookup_vendor_risk":
        obs = tools.lookup_vendor_risk(vendor_name=args["vendor_name"], product=args.get("product"),
                                        attempt=attempt, route=args.get("route", "primary"))
        if obs.get("outcome") != "success":
            return obs, None
        fresh = tools.calculate_days_since(obs["assessment_date"], eval_date)
        obs["is_current"] = fresh.get("is_current_180d")
        obs["days_since_assessment"] = fresh.get("days_elapsed")
        ev = EvidenceRecord(
            fact="vendor_risk", source_id=obs["source_id"], source_type=obs["source_type"],
            authority_tier=obs["authority_tier"], document_date=obs["assessment_date"],
            is_current=obs["is_current"], value=obs, collected_at_step=state.step_count + 1,
        )
        return obs, ev

    if tool_name == "search_vendor_documents":
        obs = tools.search_vendor_documents(vendor_name=args["vendor_name"], attempt=attempt,
                                             query_variant=args.get("query_variant", "default"),
                                             source_type=args.get("source_type"))
        if obs.get("outcome") != "success" or not obs.get("documents"):
            return obs, None

        wanted_type = args.get("source_type")

        # Every returned document is scanned for embedded-instruction patterns
        # before anything else happens. This runs regardless of wanted_type,
        # so even a security-assessment-only query still surfaces an
        # injection attempt if the search backend returns it alongside.
        security_doc = None
        for d in obs["documents"]:
            fresh = tools.calculate_days_since(d["document_date"], eval_date)
            d["is_current"] = fresh.get("is_current_180d")
            d["days_since_document"] = fresh.get("days_elapsed")
            _scan_for_injection(d, state)
            if d["source_type"] == "approved_security_assessment":
                security_doc = d

        # Prefer a security-assessment doc if one came back (whether or not
        # the caller filtered for it) -- this is what confidential-data
        # requests need as evidence.
        if security_doc is not None and (wanted_type is None or wanted_type == "approved_security_assessment"):
            ev = EvidenceRecord(
                fact="security_assessment", source_id=security_doc["document_id"],
                source_type=security_doc["source_type"], authority_tier=security_doc["authority_tier"],
                document_date=security_doc["document_date"], is_current=security_doc["is_current"],
                value=security_doc, collected_at_step=state.step_count + 1,
            )
            return obs, ev

        if wanted_type is None:
            for d in obs["documents"]:
                if d["is_current"] and d["source_type"] == "internal_vendor_risk_database":
                    ev = EvidenceRecord(
                        fact="document_refresh", source_id=d["document_id"], source_type=d["source_type"],
                        authority_tier=d["authority_tier"], document_date=d["document_date"],
                        is_current=True, value=d, collected_at_step=state.step_count + 1,
                    )
                    return obs, ev
        return obs, None

    if tool_name == "calculate_days_since":
        obs = tools.calculate_days_since(args["date_str"], args.get("reference_date", eval_date))
        return obs, None

    if tool_name == "search_web_threat_intel":
        obs = tools.search_web_threat_intel(vendor_name=args["vendor_name"])
        ev = EvidenceRecord(
            fact="web_threat_intelligence", source_id=f"WEB-{args['vendor_name']}",
            source_type=obs.get("source_type", "web_threat_intelligence"), authority_tier=3,
            document_date=eval_date, is_current=True, value=obs, collected_at_step=state.step_count + 1,
        )
        return obs, ev

    if tool_name == "lookup_cve_vulnerabilities":
        obs = tools.lookup_cve_vulnerabilities(vendor_name=args["vendor_name"], product=args.get("product"))
        ev = EvidenceRecord(
            fact="cve_vulnerabilities", source_id=f"CVE-{args['vendor_name']}",
            source_type=obs.get("source_type", "cve_vulnerability_database"), authority_tier=3,
            document_date=eval_date, is_current=True, value=obs, collected_at_step=state.step_count + 1,
        )
        return obs, ev

    if tool_name == "check_domain_security":
        obs = tools.check_domain_security(domain_or_vendor=args["domain_or_vendor"])
        ev = EvidenceRecord(
            fact="domain_security", source_id=f"DOM-{args['domain_or_vendor']}",
            source_type=obs.get("source_type", "domain_security_check"), authority_tier=3,
            document_date=eval_date, is_current=True, value=obs, collected_at_step=state.step_count + 1,
        )
        return obs, ev

    # No record_final_decision branch on purpose. It is not planner-callable
    # (tools.TOOL_SPECS marks it runtime-only, and _validate_tool_call rejects
    # it), because reaching the ledger from here would skip
    # policy_engine.validate_or_override entirely -- and the idempotency guard
    # in _finalize would then adopt that unvalidated record as the run's
    # decision. Only _finalize records.
    return {"outcome": "error", "detail": f"Unknown tool '{tool_name}'"}, None


def _finalize(state: AgentState, proposed_decision: Optional[str], stop_reason: str) -> None:
    evidence_view = _build_evidence_view(state)
    decision, reasons, citations, overridden = policy_engine.validate_or_override(
        proposed_decision, state.request, evidence_view, state.evaluation_date)

    # A planner failure is not evidence of anything. The verdict computed from
    # partial evidence may well be 'approve' -- that is exactly the case this
    # guards: an unparsable model response must not be recorded as a decision
    # the agent reached. reject/request_information are left alone because both
    # are already safe terminal states.
    failure = PLANNER_FAILURE_STOPS.get(stop_reason)
    if failure and decision not in ("reject", "request_information"):
        if decision != "escalate":
            state.add_guardrail_event(
                "failed_safe",
                f"{failure}. The evidence on file would have implied '{decision}', but a run that "
                f"did not complete cannot record that as its decision; recording 'escalate'.")
        decision = "escalate"
        reasons = [f"{failure}; failing safe to escalation for manual review."] + reasons

    if overridden:
        state.add_guardrail_event(
            "decision_overridden",
            f"Planner proposed '{proposed_decision}'; guardrail enforced '{decision}' per policy.")

    rationale = " ".join(reasons)
    record_obs = tools.record_final_decision(
        request_id=state.request_id, decision=decision, rationale=rationale, citations=citations)

    state.add_tool_call(ToolCallRecord(
        step=state.step_count + 1, tool="record_final_decision",
        args={"request_id": state.request_id, "decision": decision}, attempt=1,
        route_or_variant="-", outcome=record_obs.get("outcome", "unknown"),
        observation=record_obs, duration_ms=0,
    ))

    if record_obs.get("outcome") == "return_existing":
        state.add_guardrail_event(
            "duplicate_prevented",
            f"Request {state.request_id} already had a recorded decision "
            f"('{record_obs.get('decision')}'); not recording a duplicate.")
        decision = record_obs.get("decision", decision)

    state.final_decision = decision
    state.final_rationale = rationale
    state.final_citations = citations
    state.stop_reason = stop_reason
    state.done = True
