"""
data_store.py
--------------
Loads the mock backend systems the agent's tools talk to:
  - vendor_policy.md                 (policy retrieval source)
  - vendor_risk.csv                  (internal vendor-risk database)
  - vendor_documents.json            (security assessments + vendor-submitted docs)
  - tool_scenarios.json              (scripted unreliable-tool behavior for demo/testing)
  - decision_log.json                (append-only, idempotent final-decision ledger)

This module has zero business logic -- it is purely I/O. Business
rules live in policy_engine.py; unreliable-tool simulation lives in tools.py.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from typing import Any, Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

_lock = threading.Lock()


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def load_policy_text() -> str:
    with open(_path("vendor_policy.md"), "r", encoding="utf-8") as f:
        return f.read()


def load_vendor_risk() -> List[Dict[str, Any]]:
    rows = []
    with open(_path("vendor_risk.csv"), "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def load_vendor_documents() -> List[Dict[str, Any]]:
    with open(_path("vendor_documents.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_tool_scenarios() -> Dict[str, Any]:
    with open(_path("tool_scenarios.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_vendor_requests() -> List[Dict[str, Any]]:
    with open(_path("vendor_requests.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def evaluation_date() -> str:
    return load_tool_scenarios().get("evaluation_date", "2026-08-01")


def load_decision_log() -> List[Dict[str, Any]]:
    path = _path("decision_log.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else []


def find_existing_decision(request_id: str) -> Optional[Dict[str, Any]]:
    for rec in load_decision_log():
        if rec.get("request_id") == request_id:
            return rec
    return None


def append_decision(record: Dict[str, Any]) -> Dict[str, Any]:
    """Idempotent append: if request_id already has a final decision, return
    the existing record untouched instead of writing a duplicate. This is the
    mock-approval-API's duplicate-action guard (mirrors the DuplicateCo test)."""
    with _lock:
        existing = find_existing_decision(record["request_id"])
        if existing is not None:
            return {**existing, "outcome": "return_existing"}
        log = load_decision_log()
        record = {**record, "outcome": "recorded"}
        log.append(record)
        with open(_path("decision_log.json"), "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
        return record


def reset_decision_log() -> None:
    with _lock:
        with open(_path("decision_log.json"), "w", encoding="utf-8") as f:
            json.dump([], f)
