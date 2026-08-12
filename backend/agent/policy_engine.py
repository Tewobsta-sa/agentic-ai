"""
policy_engine.py
-----------------
The deterministic guardrail layer. This module encodes vendor_policy.md as
executable rules and is the single source of truth the orchestrator uses to:

  1. decide whether enough evidence has been gathered to reach a decision
     ("stop when goal complete"),
  2. decide what to do next when evidence is missing / stale / conflicting
     / unobtainable, and
  3. validate (and, if necessary, override) whatever decision the LLM
     planner proposes, before it is ever recorded.

This is what makes the system "hybrid": the LLM is free to reason, choose
tools, and draft explanations, but it can never single-handedly approve,
reject, or escalate something the policy disagrees with. Every override is
logged as a guardrail_event so it shows up in the execution trace and the
evaluation report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

REQUIRED_FIELDS = ["vendor_name", "product", "cost", "intended_use", "data_type"]
VALID_DATA_TYPES = {"public", "internal", "confidential", "restricted"}
COST_LIMIT = 10000
FRESHNESS_DAYS = 180

DECISIONS = ("approve", "reject", "escalate", "request_information")


@dataclass
class PolicyVerdict:
    decision: Optional[str]          # None => not enough evidence yet, keep gathering
    reasons: List[str]
    citations: List[str]
    stop: bool                       # True => loop must terminate now
    next_hint: Optional[str] = None  # advisory hint for the planner's next tool choice


def missing_fields(request: Dict[str, Any]) -> List[str]:
    missing = []
    for field in REQUIRED_FIELDS:
        val = request.get(field, None)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(field)
    return missing


def validate_request_shape(request: Dict[str, Any]) -> List[str]:
    """Structural validation beyond mere presence, e.g. an invalid data_type
    enum or a non-numeric cost. Returned as human-readable problems."""
    problems = []
    dt = request.get("data_type")
    if dt is not None and dt not in VALID_DATA_TYPES:
        problems.append(f"data_type '{dt}' is not one of {sorted(VALID_DATA_TYPES)}")
    cost = request.get("cost")
    if cost is not None:
        try:
            float(cost)
        except (TypeError, ValueError):
            problems.append(f"cost '{cost}' is not numeric")
    return problems


def evaluate(request: Dict[str, Any], evidence_view: Dict[str, Any],
             evaluation_date: str) -> PolicyVerdict:
    """
    evidence_view is a dict assembled by the orchestrator from AgentState.evidence:
      {
        "vendor_risk": {"attempted": bool, "exhausted": bool, "record": {...}|None},
        "security_assessment": {"attempted": bool, "exhausted": bool, "record": {...}|None},
        "conflicting_tier2": bool,
      }
    Returns a PolicyVerdict. decision=None with stop=False means: keep
    gathering evidence, and next_hint tells the orchestrator what's missing.
    """
    reasons: List[str] = []
    citations: List[str] = []

    # --- 1. Required fields -------------------------------------------------
    missing = missing_fields(request)
    if missing:
        reasons.append(f"Required field(s) missing: {', '.join(missing)}.")
        return PolicyVerdict("request_information", reasons, citations, stop=True)

    shape_problems = validate_request_shape(request)
    if shape_problems:
        reasons.append("Request failed validation: " + "; ".join(shape_problems))
        return PolicyVerdict("request_information", reasons, citations, stop=True)

    data_type = request["data_type"]
    cost = float(request["cost"])

    # --- 2. Restricted data always escalates, no further checks needed -----
    if data_type == "restricted":
        reasons.append("Data type is 'restricted', which always requires escalation regardless "
                        "of cost, vendor status, or evidence.")
        citations.append("policy:data_types")
        return PolicyVerdict("escalate", reasons, citations, stop=True)

    # --- 3. Cost ceiling ------------------------------------------------
    if cost > COST_LIMIT:
        reasons.append(f"Cost {cost:.0f} exceeds the USD {COST_LIMIT} approval ceiling.")
        citations.append("policy:cost")
        return PolicyVerdict("escalate", reasons, citations, stop=True)

    # --- 4. Vendor status / risk (requires vendor_risk evidence) -----------
    vr = evidence_view.get("vendor_risk", {})
    if not vr.get("attempted"):
        return PolicyVerdict(None, reasons, citations, stop=False,
                              next_hint="lookup_vendor_risk")

    vr_record = vr.get("record")
    if vr_record is None:
        if vr.get("exhausted"):
            reasons.append("Vendor-risk lookup could not be completed after the maximum "
                            "number of attempts (primary and backup routes both failed). "
                            "Required current evidence could not be found.")
            citations.append("policy:evidence_requirements")
            return PolicyVerdict("escalate", reasons, citations, stop=True)
        return PolicyVerdict(None, reasons, citations, stop=False, next_hint="retry_vendor_risk")

    if vr_record.get("status") == "prohibited":
        reasons.append(f"Vendor/product is marked 'prohibited' in the internal vendor-risk database "
                        f"(source {vr_record.get('source_id')}).")
        citations.append(vr_record.get("source_id", "vendor_risk_db"))
        return PolicyVerdict("reject", reasons, citations, stop=True)

    risk_rating = vr_record.get("risk_rating")
    vr_current = vr_record.get("is_current")
    if vr_current is False:
        # stale primary evidence: give the orchestrator a chance to refresh
        # via document search before giving up (mirrors the OldStack case).
        if not evidence_view.get("refresh_attempted"):
            return PolicyVerdict(None, reasons, citations, stop=False,
                                  next_hint="search_vendor_documents_for_refresh")
        if evidence_view.get("refresh_exhausted") and not evidence_view.get("refresh_found_current"):
            reasons.append(f"Vendor-risk record {vr_record.get('source_id')} is outdated "
                            f"(older than {FRESHNESS_DAYS} days) and no current replacement "
                            f"evidence could be found after retrying the document search.")
            citations.append("policy:evidence_requirements")
            return PolicyVerdict("escalate", reasons, citations, stop=True)

    if risk_rating == "high":
        reasons.append(f"Current vendor risk rating is 'high' (source {vr_record.get('source_id')}).")
        citations.append(vr_record.get("source_id", "vendor_risk_db"))
        return PolicyVerdict("reject", reasons, citations, stop=True)

    if risk_rating == "medium":
        reasons.append(f"Current vendor risk rating is 'medium' (source {vr_record.get('source_id')}), "
                        f"which requires escalation rather than approval or rejection.")
        citations.append(vr_record.get("source_id", "vendor_risk_db"))
        return PolicyVerdict("escalate", reasons, citations, stop=True)

    # --- 5. Same-tier conflicting evidence -----------------------------
    if evidence_view.get("conflicting_tier2"):
        reasons.append("Two current, equally authoritative (tier 2) sources disagree on a "
                        "material fact (risk rating). Per policy, conflicting same-priority "
                        "evidence must be escalated rather than resolved unilaterally.")
        citations.append("policy:source_priority")
        return PolicyVerdict("escalate", reasons, citations, stop=True)

    # --- 6. Confidential data needs a current PASS security assessment -----
    if data_type == "confidential":
        sec = evidence_view.get("security_assessment", {})
        if not sec.get("attempted"):
            return PolicyVerdict(None, reasons, citations, stop=False,
                                  next_hint="search_vendor_documents")
        sec_record = sec.get("record")
        if sec_record is None:
            if sec.get("exhausted"):
                reasons.append("A current approved security assessment is required for confidential "
                                "data but could not be retrieved after the maximum number of attempts.")
                citations.append("policy:evidence_requirements")
                return PolicyVerdict("escalate", reasons, citations, stop=True)
            return PolicyVerdict(None, reasons, citations, stop=False, next_hint="retry_search")

        if not sec_record.get("is_current"):
            reasons.append(f"Security assessment {sec_record.get('document_id')} is older than "
                            f"{FRESHNESS_DAYS} days and cannot be used as current evidence.")
            citations.append("policy:evidence_requirements")
            return PolicyVerdict("escalate", reasons, citations, stop=True)

        if sec_record.get("result") != "pass":
            reasons.append(f"Security assessment {sec_record.get('document_id')} result is "
                            f"'{sec_record.get('result')}', not 'pass'.")
            citations.append(sec_record.get("document_id", "security_assessment"))
            return PolicyVerdict("reject" if sec_record.get("risk_rating") == "high" else "escalate",
                                  reasons, citations, stop=True)

        citations.append(sec_record.get("document_id", "security_assessment"))

    # --- 7. Everything checks out -------------------------------------
    reasons.append("All required information present; vendor is not prohibited; cost is within "
                    "the approval ceiling; current low-risk vendor-risk evidence is on file"
                    + (" together with a current passing security assessment" if data_type == "confidential" else "")
                    + "; no unresolved conflicts.")
    citations.append(vr_record.get("source_id", "vendor_risk_db"))
    citations.append("policy:approval")
    return PolicyVerdict("approve", reasons, citations, stop=True)


def validate_or_override(proposed_decision: Optional[str], request: Dict[str, Any],
                          evidence_view: Dict[str, Any], evaluation_date: str
                          ) -> Tuple[str, List[str], List[str], bool]:
    """
    Recomputes the authoritative verdict and compares it against whatever the
    planner (LLM or rule-based) proposed. If they disagree, the deterministic
    verdict always wins. Returns (final_decision, reasons, citations, was_overridden).
    """
    verdict = evaluate(request, evidence_view, evaluation_date)
    if verdict.decision is None:
        # Guardrail thinks more evidence is needed; planner tried to finish early.
        return ("escalate",
                ["Guardrail: insufficient evidence to support a final decision; "
                 "escalating rather than allowing a premature approval/rejection."],
                ["policy:evidence_requirements"], True)

    if proposed_decision is not None and proposed_decision != verdict.decision:
        overridden_reasons = [f"Guardrail overrode planner's proposed decision "
                               f"('{proposed_decision}') because it conflicts with policy."] + verdict.reasons
        return (verdict.decision, overridden_reasons, verdict.citations, True)

    return (verdict.decision, verdict.reasons, verdict.citations, False)
