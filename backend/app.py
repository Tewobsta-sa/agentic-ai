"""
app.py
------
FastAPI backend exposing the Vendor-Assessment Agent over HTTP for the
frontend. Run with:

    uvicorn app:app --reload --port 8000

Endpoints:
  POST /api/assess        run the ReAct loop on a new request, return full trace
  GET  /api/decisions      list everything recorded in decision_log.json
  GET  /api/policy         return the raw policy markdown
  GET  /api/test-cases     return the curated test-case suite
  POST /api/run-tests      run the full test suite, return pass/fail + logs
  POST /api/reset          clear decision_log.json (dev/demo convenience)
  GET  /api/brain-info     which reasoning mode is active (llm vs rule_based)
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# Load backend/.env before anything reads os.environ, so the documented
# `uvicorn app:app` command behaves identically to run.sh (which exports the
# file itself). Without this, starting the server directly silently drops the
# API keys and the agent quietly falls back to the offline planner.
try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; run.sh still exports the file
    pass
else:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from agent import data_store
from agent.llm_brain import ProviderError, make_brain
from agent.orchestrator import run as run_agent

app = FastAPI(title="Vendor-Assessment Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ProviderError)
def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    """Safety net for the case where a live-LLM failure is *not* absorbed by
    the orchestrator's fallback. A quota or outage is not an internal server
    error, and reporting it as one hides the one detail that makes it
    actionable -- so report the real status and the provider's own message."""
    status = 429 if exc.status_code == 429 else 503
    return JSONResponse(
        status_code=status,
        content={
            "error": "llm_provider_unavailable",
            "detail": str(exc),
            "provider_status": exc.status_code,
            "retry_after_seconds": exc.retry_after,
            "hint": "Set AGENT_MODE=rule_based to run the agent offline, or retry once quota resets.",
        },
        headers={"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None,
    )


class VendorRequestIn(BaseModel):
    request_id: Optional[str] = None
    vendor_name: Optional[str] = None
    product: Optional[str] = None
    cost: Optional[float] = None
    intended_use: Optional[str] = None
    data_type: Optional[str] = None


def _next_request_id() -> str:
    log = data_store.load_decision_log()
    return f"WEB-{len(log) + 1:04d}-{int(time.time()) % 100000}"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/brain-info")
def brain_info():
    try:
        brain = make_brain()
    except Exception as exc:
        return {
            "mode": "rule_based (offline)",
            "anthropic_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "gemini_key_configured": bool(os.environ.get("GEMINI_API_KEY")),
            "forced_mode": os.environ.get("AGENT_MODE", "auto"),
            "provider_error": f"{exc.__class__.__name__}: {exc}",
        }

    response = {
        "mode": brain.name,
        "anthropic_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "gemini_key_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "forced_mode": os.environ.get("AGENT_MODE", "auto"),
    }
    if getattr(brain, "provider_error", None):
        response["provider_error"] = brain.provider_error
    return response


@app.get("/api/policy")
def get_policy():
    return {"content": data_store.load_policy_text()}


@app.get("/api/decisions")
def get_decisions():
    return {"decisions": data_store.load_decision_log()}


@app.post("/api/reset")
def reset():
    data_store.reset_decision_log()
    return {"status": "reset"}


@app.post("/api/assess")
def assess(req: VendorRequestIn):
    request_dict: Dict[str, Any] = {
        "vendor_name": req.vendor_name,
        "product": req.product,
        "cost": req.cost,
        "intended_use": req.intended_use,
        "data_type": req.data_type,
    }
    request_id = req.request_id or _next_request_id()
    state = run_agent(request_dict, request_id)
    return {
        "request_id": request_id,
        "decision": state.final_decision,
        "rationale": state.final_rationale,
        "citations": state.final_citations,
        "stop_reason": state.stop_reason,
        "steps_taken": state.step_count,
        "brain": state.brain_name,
        "provider_degraded": state.provider_degraded,
        "provider_error": state.provider_error,
        "trace": state.to_trace(),
        "full_state": state.to_dict(),
    }


TEST_CASES_PATH = os.path.join(os.path.dirname(__file__), "tests", "test_cases.json")


@app.get("/api/test-cases")
def get_test_cases():
    if not os.path.exists(TEST_CASES_PATH):
        raise HTTPException(status_code=404, detail="test_cases.json not found")
    with open(TEST_CASES_PATH) as f:
        return {"test_cases": json.load(f)}


@app.post("/api/run-tests")
def run_tests():
    if not os.path.exists(TEST_CASES_PATH):
        raise HTTPException(status_code=404, detail="test_cases.json not found")
    with open(TEST_CASES_PATH) as f:
        cases = json.load(f)

    data_store.reset_decision_log()
    results = []
    passed = 0
    degraded = 0
    for case in cases:
        state = run_agent(case["request"], case["request_id"])
        ok = state.final_decision == case["expected_decision"]
        passed += int(ok)
        degraded += int(state.provider_degraded)
        results.append({
            "request_id": case["request_id"],
            "category": case.get("category"),
            "description": case.get("description"),
            "expected_decision": case["expected_decision"],
            "actual_decision": state.final_decision,
            "pass": ok,
            "steps_taken": state.step_count,
            "brain": state.brain_name,
            "provider_degraded": state.provider_degraded,
            "guardrail_events": state.guardrail_events,
            "trace": state.to_trace(),
        })
    return {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "degraded_to_offline_planner": degraded,
        "success_rate": round(passed / len(cases), 4) if cases else 0,
        "results": results,
    }


# Serve the static frontend, if present, at "/"
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_frontend_dir):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
