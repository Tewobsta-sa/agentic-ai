# Ledger — Autonomous Vendor-Assessment Agent

An autonomous AI agent that takes a software-vendor request (**vendor name**, **product**, **cost**, **intended use**, **data type**) and executes a **ReAct-style loop** — *Reason $\rightarrow$ Act $\rightarrow$ Observe $\rightarrow$ Update State $\rightarrow$ Repeat* — until it reaches exactly one of four policy-validated decisions: **approve**, **reject**, **escalate**, or **request_information**.

Built with a **hybrid architecture**: live LLM reasoning (Google Gemini / Anthropic Claude) coupled with an **independent deterministic policy guardrail engine** and **Live Web Threat Intelligence tools**.

---

## 1. Quick Start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate  # optional but recommended
pip install -r requirements.txt
./run.sh   # or: uvicorn app:app --port 8000
```

Open **http://localhost:8000** in your browser. The single-page console is served directly by the backend:
- **Assess View**: Submit custom vendor requests or load any of the 16 curated test cases from the dropdown, watching the live Thought $\rightarrow$ Action $\rightarrow$ Observation trace.
- **Test Suite View**: Run the full 16-case test suite on demand with a live success rate gauge and category pass/fail summary.
- **Policy View**: Inspect the authoritative policy document (`vendor_policy.md`) the agent is bound to.

To run the automated test suite from the command line:
```bash
cd backend
python3 tests/run_tests.py
```

---

## 2. Architecture & System Pillars

```
backend/
├── agent/
│   ├── state.py            # AgentState memory: evidence, tool calls, retries, guardrails
│   ├── data_store.py       # Mock database I/O & decision log ledger
│   ├── tools.py            # The 8 tools (5 internal + 3 web intelligence)
│   ├── policy_engine.py    # Deterministic policy guardrail engine
│   ├── llm_brain.py        # Brain planners: GeminiBrain, LLMBrain, RuleBasedBrain
│   └── orchestrator.py     # ReAct execution loop, schema validation, retries, finalizer
├── app.py                  # FastAPI REST API & static frontend server
├── data/                   # Mock policy, risk database, and document repositories
└── tests/
    ├── test_cases.json     # 16 curated test cases
    ├── run_tests.py        # Batch test runner
    └── execution_logs/     # Generated per-case .json and .txt execution traces
frontend/
└── index.html              # Modern dark glassmorphic web dashboard
```

---

## 3. The Tools Catalog

The agent has access to **8 specialized tools** registered in `tools.py`:

### Internal Tools
1. **`retrieve_policy(section)`**: Policy retrieval. Reads sections of `vendor_policy.md`.
2. **`lookup_vendor_risk(vendor_name, product, route)`**: Internal vendor risk database lookup (`primary` and `backup` routes).
3. **`search_vendor_documents(vendor_name, query_variant, source_type)`**: Document repository search. Wraps text content as untrusted data to defend against prompt injection.
4. **`calculate_days_since(date_str, reference_date)`**: Calculator tool for the 180-day evidence freshness rule.
5. **`record_final_decision(request_id, decision, rationale, citations)`**: Ledger recorder (**idempotent** per `request_id`).

### Live Web Intelligence Tools
6. **`search_web_threat_intel(vendor_name)`**: Searches public web threat intelligence for vendor security breaches, news, and public reputation alerts.
7. **`lookup_cve_vulnerabilities(vendor_name, product)`**: Queries public National Vulnerability Database (NIST NVD) / CVE vulnerability records.
8. **`check_domain_security(domain_or_vendor)`**: Checks domain WHOIS registration age, SSL/TLS certificate validity, and domain security ratings.

---

## 4. ReAct Loop & Safety Guardrails

Each iteration of `orchestrator.run()` executes the following pipeline:

1. **Plan**: The active brain (`GeminiBrain`, `LLMBrain`, or `RuleBasedBrain`) inspects current state and evidence to propose a tool call or final decision with natural-language reasoning.
2. **Validate**: The orchestrator validates proposed tool names and arguments against strict `TOOL_SPECS` before execution.
3. **Retry Budget Gate**: Enforces a maximum of **3 attempts total** per evidence need, rejecting repeated identical routes/queries (`repeated_route_rejected`).
4. **Act**: The tool executes against internal databases or live web feeds.
5. **Observe & Untrusted Content Defense**: Tool observations are scanned for prompt-injection markers (`prompt_injection_detected`). Free-text document content is treated strictly as data, never as instructions.
6. **Policy Override**: Proposed final decisions are independently validated against `policy_engine.py`. If a proposed decision violates policy rules, the guardrail overrides it (`decision_overridden`) and enforces the correct verdict.

---

## 5. Test Suite Benchmarks (16 Test Cases)

The agent suite contains **16 end-to-end test cases** covering all critical operational categories (**100.0% pass rate**):

- **Normal Workflows (5 cases)**: `TC-01`, `TC-02`, `TC-03`, `TC-13`, `TC-14`
- **Missing Information (4 cases)**: `TC-04`, `TC-05`, `TC-06`, `TC-15`
- **Tool Failure Retries (3 cases)**: `TC-07`, `TC-08`, `TC-09`
- **Conflicting Evidence (1 case)**: `TC-10`
- **Prompt Injection Defense (1 case)**: `TC-11`
- **Duplicate Action Prevention (1 case)**: `TC-12`
- **Web Threat Intelligence Exploration (1 case)**: `TC-16`

---

## 6. Environment Configuration

To enable live LLM reasoning, copy `.env.example` to `.env` in the `backend/` directory:

```env
# Mode Selection: auto | gemini | llm | rule_based
AGENT_MODE=gemini

# Google Gemini API Configuration
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash
GEMINI_MAX_RPM=4

# Anthropic Claude Configuration (Optional)
ANTHROPIC_API_KEY=your_anthropic_api_key_here
CLAUDE_MODEL=claude-sonnet-5
```

If API keys are omitted or rate-limited (HTTP 429), the orchestrator automatically degrades to `RuleBasedBrain` (offline planner) to ensure **100% operational uptime**.