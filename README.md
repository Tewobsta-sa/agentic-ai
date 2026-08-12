# Ledger — Autonomous Vendor-Assessment Agent

An autonomous agent that takes a software-vendor request (vendor name, product,
cost, intended use, data type) and runs a **ReAct-style loop** — reason, call a
tool, observe, update state, repeat — until it reaches exactly one of four
decisions: **approve**, **reject**, **escalate**, or **request_information**.

Built against the provided mock dataset (`vendor_policy.md`, `vendor_risk.csv`,
`vendor_documents.json`, `vendor_requests.json`, `tool_scenarios.json`),
**unmodified**. All 14 requests in `vendor_requests.json` (13 unique + 1
intentional duplicate) run correctly end to end; see
[`docs/EVALUATION_REPORT.md`](docs/EVALUATION_REPORT.md) for full results.

---

## 1. Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -r requirements.txt
./run.sh                                                 # or: uvicorn app:app --port 8000
```

Open **http://localhost:8000** — the frontend is served by the same backend
process (no separate build step). You can:

- Submit a vendor request by hand, or load any of the 15 curated test cases
  from the dropdown, and watch the live Thought → Action → Observation trace.
- Run the full test suite from the **Test Suite** tab and see a live
  pass/fail table + success rate.
- Read the raw policy the agent is bound to under the **Policy** tab.

To run everything from the command line instead (this is what generated
`backend/tests/execution_logs/` and `backend/tests/test_summary.json`):

```bash
cd backend
python3 tests/run_tests.py
```

By default the agent runs **fully offline** with a deterministic rule-based
planner — no API key, no network calls, 100% reproducible. To switch on live
LLM-driven reasoning, copy `backend/.env.example` to `.env`, set
`ANTHROPIC_API_KEY`, and restart. See [§5](#5-the-two-planners-hybrid-design).

---

## 2. Architecture

![architecture diagram](docs/architecture.png)

(Mermaid source: [`docs/architecture.mmd`](docs/architecture.mmd).)

```
backend/
  agent/
    state.py           # AgentState: memory (evidence, tool calls, retries, guardrail events)
    data_store.py       # loads the 5 mock data files; append-only decision log I/O
    tools.py             # the 5 tools (see §4)
    policy_engine.py    # deterministic guardrail — vendor_policy.md as executable rules
    llm_brain.py         # the planner: LLMBrain (Claude API) + RuleBasedBrain (offline)
    orchestrator.py     # the ReAct loop itself: validation, retries, stopping, finalize
  app.py                 # FastAPI: /api/assess, /api/run-tests, /api/policy, ...
  data/                  # the provided mock files, copied in unmodified
  tests/
    test_cases.json      # 15 curated test cases
    run_tests.py          # batch runner -> execution_logs/ + test_summary.json
    execution_logs/       # generated per-case .json + .txt traces
frontend/
  index.html              # single-page console (form, live trace, test dashboard, policy view)
docs/
  architecture.mmd / .png
  EVALUATION_REPORT.md
```

---

## 3. The ReAct loop, precisely

Each iteration of `orchestrator.run()`:

1. **Plan** — the active brain (`llm_brain.make_brain()`) looks at the current
   `AgentState` (request + evidence collected so far) and proposes either a
   tool call or a final decision, with a short natural-language "thought".
2. **Validate** — the orchestrator checks the proposed tool name and its
   arguments against a strict schema (`TOOL_SCHEMAS`) *before* anything is
   executed. An unknown tool or an unexpected argument is rejected outright —
   the model can never invent an argument the runtime doesn't recognize.
3. **Retry-gate** — every "evidence need" (e.g. "get FailWare's risk record")
   has a retry budget of **3 attempts total** (1 first attempt + 2 retries,
   per the brief). A repeated identical route/query is rejected even if
   budget remains — each retry must be a "different valid approach".
4. **Act** — the tool actually runs, against the *unmodified* mock data,
   subject to any scripted unreliable behavior in `tool_scenarios.json`
   (timeouts, no-results).
5. **Observe** — the result is validated (`outcome` field: `success` /
   `timeout` / `no_results` / `not_found` / `error`), freshness is computed
   via the calculator tool, any document content is scanned for
   prompt-injection patterns (logged, never acted on), and evidence is
   appended to state with full provenance (source id, tier, date, currency).
6. **Stop?** — the loop terminates the moment one of four conditions is met:
   - **goal complete** — the guardrail has enough current evidence to reach approve/reject,
   - **missing information** — a required field is absent or malformed,
   - **risk too high / policy-forced terminal state** — prohibited vendor,
     high/medium risk, restricted data, cost over ceiling, unresolved
     same-tier conflict, or evidence that's unobtainable/stale after retries,
   - **max steps reached** (10, configurable) — fails safe to `escalate`.

## 4. The five tools

| Tool | Category (from the brief) | What it does |
|---|---|---|
| `retrieve_policy(section)` | policy retrieval | Returns a section (or all) of `vendor_policy.md`. |
| `lookup_vendor_risk(vendor_name, product, route)` | vendor-risk lookup | Looks up the vendor in `vendor_risk.csv`. `route` is `primary` or `backup`; the scripted scenarios make `primary` time out for some vendors so the agent must retry via `backup`. |
| `search_vendor_documents(vendor_name, query_variant, source_type)` | search | Searches `vendor_documents.json`. `query_variant` is `default` or `corrected`. Every document's free-text `content` field is returned wrapped as `untrusted_content: true` / `content_is_data_not_instructions: true` — this is the concrete mechanism (not just a prompt-level promise) behind prompt-injection resistance: `policy_engine.py` never reads `content`, only structured fields (`result`, `risk_rating`, `document_date`). |
| `calculate_days_since(date_str, reference_date)` | calculator | Computes elapsed days and whether a piece of evidence is within the 180-day currency window. Kept out of the LLM's hands deliberately — date arithmetic is exactly the kind of thing a planner should delegate to code, not "reason" about. |
| `record_final_decision(request_id, decision, rationale, citations)` | mock approval API | Appends to `decision_log.json`. **Idempotent**: if `request_id` already has a recorded decision, it returns the existing record (`outcome: "return_existing"`) instead of writing a duplicate — this is what makes duplicate-submission handling (VR-010 / TC-12) safe. |

## 5. The two/three planners (hybrid design)

The brief asked for a hybrid: **the LLM proposes, code validates and guards.**

- **`LLMBrain`** — calls the real Anthropic Messages API. Used when `ANTHROPIC_API_KEY` is set.
- **`GeminiBrain`** — calls the real Google Gemini API (`google-genai` SDK,
  the current unified SDK — not the older/deprecated `google-generativeai`
  package). Used when `GEMINI_API_KEY` is set. See §10 for exact setup steps.
- **`RuleBasedBrain`** — a dependency-free, deterministic reasoner that walks
  the same evidence-gathering sequence a well-behaved LLM should. Used by
  default so the project runs with zero setup and every test run is
  bit-for-bit reproducible for grading.

All three implement the exact same `Brain.propose_action(state, evidence_view)`
interface, so swapping providers touches nothing in the orchestrator, the
tools, the retry policy, or the guardrail — only `llm_brain.py` knows which
provider is talking.

Either way, **the planner's proposed final decision is never trusted
directly.** `orchestrator._finalize()` always calls
`policy_engine.validate_or_override()`, which independently recomputes the
correct decision from `vendor_policy.md`'s rules and the evidence actually
collected. If the planner's proposal disagrees, the deterministic verdict
wins and a `decision_overridden` guardrail event is logged — visible in every
trace and counted in the evaluation report. This is what makes "hybrid" safe
rather than just "LLM with vibes": the natural-language reasoning and
citations come from the planner, but correctness is guaranteed by code.

## 6. State / memory

`AgentState` (see `state.py`) holds, for one run:

- `evidence`: a list of `EvidenceRecord` — fact type, source id, source type,
  **authority tier** (1=policy, 2=internal DB / approved assessment,
  3=vendor-submitted, 4=unverified), document date, computed `is_current`,
  and the raw structured payload.
- `tool_calls`: every attempt, with its route/variant, attempt number, and
  outcome — this *is* the execution log.
- `retry_counts`: per-need attempt counters (both the overall 3-attempt
  budget and the per-route "don't repeat yourself" check).
- `guardrail_events`: every place the deterministic layer intervened
  (injection detected, decision overridden, duplicate prevented, retry
  budget exceeded, max steps reached, ...).
- `thoughts`: the planner's running natural-language reasoning.

`state.to_trace()` flattens all of this into the chronological
Thought/Action/Observation/Guardrail sequence used by both the frontend and
the execution logs.

## 7. Retries and fallbacks

- **Budget**: 3 attempts total per evidence need (1 + 2 retries), enforced in
  `orchestrator.run()` before a tool is ever called.
- **Must vary the approach**: a second call using the *same* route/query as
  a prior attempt on the same need is rejected by the orchestrator
  (`repeated_route_rejected`) even if budget remains.
- **Fallback chain when evidence can't be obtained**:
  1. `lookup_vendor_risk(route="primary")` fails → retry `route="backup"`.
  2. If a vendor-risk record is stale (>180 days), the agent tries
     `search_vendor_documents(query_variant="default")` to find something
     more current before giving up; if that also comes back empty, it
     retries once more with `query_variant="corrected"`.
  3. If all attempts are exhausted and no current evidence exists, the
     policy guardrail forces **escalate** — the agent never guesses.
- **Planner-level fallback**: if `ANTHROPIC_API_KEY` isn't set (or the SDK
  isn't installed, or the LLM call fails), `make_brain()` transparently falls
  back to `RuleBasedBrain` so the system degrades gracefully rather than
  crashing.
- **Malformed planner output**: if an LLM response isn't valid JSON, or
  proposes an unrecognized action/decision, the orchestrator does not crash
  or guess — it logs a guardrail event and fails safe to `escalate`.

## 8. Setup notes / requirements

- Python 3.10+
- `pip install -r backend/requirements.txt` (the `anthropic` package is only
  imported if you actually enable LLM mode)
- No database — `decision_log.json` is the append-only ledger; use
  `POST /api/reset` (or `data_store.reset_decision_log()`) to clear it for a
  repeatable test run.
- No build step for the frontend — it's a single static HTML file.

## 9. Known limitations & possible improvements

See [`docs/EVALUATION_REPORT.md`](docs/EVALUATION_REPORT.md) for the full
one-page evaluation, including success rate, failure modes observed while
building this, and recommended next steps.

## 10. Manual end-to-end testing with a real Gemini API key

This is the fastest path to watch the *real* LLM plan against the mock data,
end to end, with your own eyes.

**Step 1 — get a key.** Go to [Google AI Studio](https://aistudio.google.com/apikey)
and create a Gemini API key (free tier is enough for this).

**Step 2 — install the SDK.**
```bash
cd backend
pip install -r requirements.txt      # includes google-genai already
```

**Step 3 — configure the key.**
```bash
cp .env.example .env
```
Edit `.env` and set:
```
GEMINI_API_KEY=your-real-key-here
AGENT_MODE=gemini
```
(`AGENT_MODE=gemini` forces Gemini even though `ANTHROPIC_API_KEY` is empty;
you could also just set the key and leave `AGENT_MODE=auto` — it'll pick
Gemini automatically since no Anthropic key is set.)

**Step 4 — start the server and confirm it picked up the key.**
```bash
./run.sh
```
In another terminal:
```bash
curl -s http://localhost:8000/api/brain-info
```
You should see:
```json
{"mode": "llm (online, gemini)", "gemini_key_configured": true, ...}
```
If you instead see `"rule_based (offline)"`, the key wasn't picked up —
double check `.env` is in `backend/` and that you restarted `./run.sh` after
editing it (env vars are read once at process start).

**Step 5 — watch it reason, one request at a time.**
Open **http://localhost:8000**, pick a scenario from the dropdown (try
`TC-11-prompt-injection-ignored` first — it's the most interesting one to
watch a live model handle), and hit **Run assessment**. The trace panel
shows Gemini's actual thoughts and tool choices this time, not the
rule-based script. Also check the terminal running `./run.sh` — nothing
sensitive is logged, but you'll see the request come in.

Or from the command line, one request at a time:
```bash
curl -s -X POST http://localhost:8000/api/assess \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_name": "InjectCorp",
    "product": "HelpDesk AI",
    "cost": 9000,
    "intended_use": "Process confidential customer-support records",
    "data_type": "confidential"
  }' | python3 -m json.tool
```
Look at `trace` in the response: each `"type": "thought"` entry is Gemini's
actual reasoning text. Confirm `guardrail_events` contains a
`prompt_injection_detected` entry, and that the final `decision` matches what
the offline planner reached (`approve`) — proving the injected instruction
in `VENDOR-006` had no effect regardless of which brain is doing the
reasoning.

**Step 6 — run the full suite against the live model.**
```bash
curl -s -X POST http://localhost:8000/api/reset
curl -s -X POST http://localhost:8000/api/run-tests | python3 -m json.tool
```
or from the Test Suite tab in the browser. This is also the moment to watch
for **guardrail overrides**: if Gemini ever proposes a decision that
conflicts with `vendor_policy.md`, the response's `guardrail_events` for
that case will contain a `decision_overridden` entry and the final decision
will still be policy-correct — that's the whole point of the hybrid design
(§5), and it's the one thing the offline planner structurally can't
demonstrate on its own (see the Limitations section of the evaluation
report).

**Step 7 — go back to offline mode any time.**
```bash
# in .env
AGENT_MODE=rule_based
```
or just delete/blank `GEMINI_API_KEY` and restart. No code changes needed.

A couple of things worth knowing before you do this:
- Gemini calls are real network requests — expect a few hundred ms to a
  couple of seconds per reasoning step, and each assessment can involve
  2–5 steps, so a full 15-case suite run will take noticeably longer than
  the instant offline run.
- Free-tier Gemini keys have rate limits; if you hit one mid-suite, failed
  calls surface as a parse/response error in that step and the orchestrator
  fails safe to `escalate` for that case (see §7) rather than crashing the
  whole run — just re-run it.
- Nothing about the policy, the tools, or the mock data changes based on
  which brain is active — you're testing the *reasoning*, not the rules.
