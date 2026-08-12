"""
state.py
--------
Defines the Agent's working memory (short-term state) for a single
vendor-assessment run. This is the "Memory" pillar from the ReAct
architecture: everything the planner and the guardrail need to see
in order to reason about the next step lives here, and nothing here
is ever mutated by the LLM directly -- only the orchestrator writes
to it, after validating tool results.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvidenceRecord:
    """A single piece of evidence collected from a tool, with full provenance."""
    fact: str                      # e.g. "vendor_risk", "security_assessment"
    source_id: str                 # e.g. "RISK-007", "SEC-005"
    source_type: str               # internal_vendor_risk_database | approved_security_assessment | vendor_document
    authority_tier: int            # 1 = policy, 2 = internal/approved, 3 = vendor-provided, 4 = unverified
    document_date: Optional[str]
    is_current: Optional[bool]     # computed via calculator tool against 180-day rule
    value: Dict[str, Any]          # the structured payload (risk_rating, result, etc.)
    collected_at_step: int


@dataclass
class ToolCallRecord:
    """One row of the execution log: a single tool invocation attempt."""
    step: int
    tool: str
    args: Dict[str, Any]
    attempt: int                    # 1 = first try, 2/3 = retries
    route_or_variant: str
    outcome: str                    # success | timeout | no_results | error
    observation: Any
    duration_ms: int
    timestamp: float = field(default_factory=time.time)
    seq: int = 0                    # global ordering stamp, set by add_tool_call


@dataclass
class AgentState:
    request: Dict[str, Any]
    request_id: str
    evaluation_date: str
    max_steps: int = 10
    step_count: int = 0
    done: bool = False

    thoughts: List[str] = field(default_factory=list)
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    evidence: List[EvidenceRecord] = field(default_factory=list)
    retry_counts: Dict[str, int] = field(default_factory=dict)   # key -> attempts used so far
    guardrail_events: List[Dict[str, Any]] = field(default_factory=list)

    # One monotonic counter stamped onto every thought, tool call and guardrail
    # event as it happens. step_count is not usable for ordering: a rejected
    # tool proposal burns a step without producing a tool_call, and several
    # guardrail events can share one step -- so interleaving the three lists by
    # step alone silently reorders the trace.
    thought_seqs: List[int] = field(default_factory=list)
    _seq: int = field(default=0, init=False, repr=False)

    final_decision: Optional[str] = None
    final_rationale: Optional[str] = None
    final_citations: List[str] = field(default_factory=list)
    stop_reason: Optional[str] = None

    # Which planner actually produced the reasoning, and whether a live
    # provider dropped out mid-run. Surfaced in the API response so a
    # degraded run is never mistaken for a live-LLM one.
    brain_name: Optional[str] = None
    provider_degraded: bool = False
    provider_error: Optional[str] = None

    run_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def add_thought(self, text: str) -> None:
        self.thoughts.append(text)
        self.thought_seqs.append(self._next_seq())

    def add_tool_call(self, record: ToolCallRecord) -> None:
        record.seq = self._next_seq()
        self.tool_calls.append(record)
        self.step_count += 1

    def add_evidence(self, record: EvidenceRecord) -> None:
        self.evidence.append(record)

    def add_guardrail_event(self, kind: str, detail: str) -> None:
        self.guardrail_events.append({"kind": kind, "detail": detail,
                                       "step": self.step_count, "seq": self._next_seq()})

    def retries_used(self, key: str) -> int:
        return self.retry_counts.get(key, 0)

    def register_attempt(self, key: str) -> int:
        self.retry_counts[key] = self.retry_counts.get(key, 0) + 1
        return self.retry_counts[key]

    def evidence_by_fact(self, fact: str) -> List[EvidenceRecord]:
        return [e for e in self.evidence if e.fact == fact]

    def to_trace(self) -> List[Dict[str, Any]]:
        """Flattened, genuinely chronological Thought/Action/Observation/Guardrail
        trace for display & logging.

        This used to concatenate the three lists instead of merging them, so
        every thought printed before every action and every guardrail event
        printed last regardless of when it fired. A run whose first two tool
        proposals were rejected therefore rendered as four thoughts, then the
        one call that worked, then the rejections -- which reads like the agent
        lost its mind rather than like a validator doing its job.
        """
        entries: List[tuple] = []

        for i, text in enumerate(self.thoughts):
            seq = self.thought_seqs[i] if i < len(self.thought_seqs) else 0
            entries.append((seq, 0, {"type": "thought", "index": i, "text": text}))

        for tc in self.tool_calls:
            entries.append((tc.seq, 0, {
                "type": "action",
                "step": tc.step,
                "tool": tc.tool,
                "args": tc.args,
                "attempt": tc.attempt,
                "route_or_variant": tc.route_or_variant,
            }))
            # Same seq as its action, ordered after it by the tie-breaker.
            entries.append((tc.seq, 1, {
                "type": "observation",
                "step": tc.step,
                "tool": tc.tool,
                "outcome": tc.outcome,
                "observation": tc.observation,
            }))

        for g in self.guardrail_events:
            entries.append((g.get("seq", 0), 0, {"type": "guardrail", **g}))

        entries.sort(key=lambda e: (e[0], e[1]))
        return [entry for _, _, entry in entries]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_uuid": self.run_uuid,
            "request_id": self.request_id,
            "request": self.request,
            "evaluation_date": self.evaluation_date,
            "steps_taken": self.step_count,
            "max_steps": self.max_steps,
            "final_decision": self.final_decision,
            "final_rationale": self.final_rationale,
            "final_citations": self.final_citations,
            "stop_reason": self.stop_reason,
            "brain_name": self.brain_name,
            "provider_degraded": self.provider_degraded,
            "provider_error": self.provider_error,
            "evidence": [e.__dict__ for e in self.evidence],
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "guardrail_events": self.guardrail_events,
            "thoughts": self.thoughts,
        }
