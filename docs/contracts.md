# Contracts

This document defines the interface contracts between components of the
Tel Aviv Real Estate Listing Triage Agent. These contracts are the source of
truth for Phase 1+ implementation. **No code implementing these contracts
exists yet — Phase 0 is documentation and scaffolding only.**

All shapes below are described conceptually (field name: type) and will be
formalized as Pydantic v2 models in Phase 1+.

---

## 1. HTTP API Contracts

### `GET /api/team_info`

Returns static information about the team.

Response (conceptual):
```
{
  "team_name": string,
  "members": [ { "name": string, "role": string } ]
}
```

### `GET /api/agent_info`

Returns static information describing the agent (name, purpose, version).

Response (conceptual):
```
{
  "agent_name": string,
  "description": string,
  "version": string
}
```

### `GET /api/model_architecture`

Returns a static description of the agent's architecture: the LLM modules,
deterministic modules, and tools, and how they connect.

Response (conceptual):
```
{
  "llm_modules": [string],       // exactly ["triage_planner", "report_writer"]
  "deterministic_modules": [string],
  "tools": [string]
}
```

### `POST /api/execute`

Runs the agent end-to-end on a submitted listing. This is the only endpoint
that performs real work (LLM calls + tool calls).

Request (conceptual):
```
{
  "prompt": string,                  // required, the user's free-text input
  "conversation_id": string | null   // optional (Phase 2 - stateful follow-ups)
}
```

`conversation_id` is entirely optional and backward-compatible:
- Absent (or blank/whitespace) → exact original stateless behavior: no
  conversation lookup, no evidence reuse, nothing saved.
- Present, unknown id → treated as a brand-new conversation (never an
  error - no authentication, no ownership check).
- Present, known id → a follow-up turn about the same listing: missing
  listing facts are hydrated from the conversation's stored context, and
  already-collected `geo_resolver`/`nearby_services`/`price_signal`
  results are reused instead of re-executed when the exact same typed tool
  input would be produced again. See
  `docs/follow_up_context_plan.md` for the full design.
- Two different `conversation_id` values never share any state.
- Context is in-memory only, for the life of the current process - no
  persistence across a restart, no TTL, no distributed store.

Response — **must always** contain exactly these top-level fields, regardless
of success or failure, whether or not `conversation_id` was supplied:
```
{
  "status": "ok" | "error",
  "error": string | null,
  "response": string | null,   // the final triage report when status == "ok"
  "steps": [ StepTrace ]       // the LLM-module steps of this run, in order
}
```

Rules:
- `status` is `"ok"` only if the full pipeline completed and a report was
  produced; otherwise `"error"`.
- `error` is `null` on success, and a human-readable message on failure.
- `response` is `null` on failure.
- `steps` reports only the **LLM-module** steps (`triage_planner`,
  `report_writer`), in call order — never the intermediate `tool`,
  `deterministic`, or `context_reuse` steps. On a normal successful run
  this is always exactly two entries: `triage_planner` then
  `report_writer`, regardless of whether `conversation_id` was supplied.
  `step_tracer` still records every tool/deterministic/context-reuse step
  internally (for the Evidence Bundle and future debugging), but
  `/api/execute` only ever surfaces the LLM steps.

### `GET /`

Serves the plain HTML/CSS/JS GUI (static files, no server-side templating
logic beyond serving the file).

---

## 2. Deterministic Module Contracts

### `agent_orchestrator`

Coordinates a single `/api/execute` run:
1. (Optional, only when `conversation_id` was supplied) Loads any stored
   `ConversationContext` and builds a compact follow-up context summary.
2. Calls `triage_planner` (LLM) to get a plan, including the compact
   follow-up context (if any) as an additional `follow_up_context` field
   in its `user_payload` - never a new LLM call.
3. (Optional) Hydrates the plan's missing `PropertyDetails` fields from
   the stored context; detects a listing change.
4. Passes the plan to `plan_validator`.
5. Passes the validated plan to `evidence_collector` - or, when a
   conversation context is active, to `agent.conversation`'s cache-aware
   `collect_evidence_with_context`, which reuses already-cached tool
   results and executes only the missing work through the same
   `ToolExecutor` seam.
6. Passes collected evidence to `evidence_bundle_builder`.
7. Calls `report_writer` (LLM) with the evidence bundle.
8. Uses `step_tracer` throughout to build the `steps` list (plus, when
   stateful, one internal `context_reuse` deterministic step).
9. (Optional) Saves the updated `ConversationContext` back to the store.
10. Assembles the final `{status, error, response, steps}` payload.

### `plan_validator`

Input: raw plan object produced by `triage_planner`.
Output: validated/normalized plan, or a validation error.
Rules:
- Must reject any tool name not in the fixed tool whitelist
  (`geo_resolver`, `nearby_services`, `price_signal`).
- Must be pure/deterministic — no LLM or network calls.

### `evidence_collector`

Input: validated plan.
Output: raw evidence, one entry per planned tool call, including success/
failure per call.
Rules:
- Only calls tools through the injected `ToolExecutor` interface
  (`src/listing_agent/agent/tool_executor/`) — never a concrete tool
  implementation directly. `ToolExecutor` selects its actual transport
  (local in-process tools today, a future MCP server) per `TOOL_BACKEND`;
  `evidence_collector` and `agent_orchestrator` are unaware of which.
- Must not call an LLM.
- **Fail-soft**: one tool failing never aborts the run. Every planned call
  produces exactly one `ToolEvidence` entry, whether it succeeded, failed,
  or was skipped.
- A tool that depends on resolved coordinates/address (`nearby_services`,
  `price_signal`) and never gets them — because `geo_resolver` was never
  planned, technically failed, or produced a non-`"resolved"` business
  outcome — is recorded as `ToolEvidence(status="skipped", reason=...)`,
  distinct from `status="error"` (the tool itself was attempted and
  failed). This keeps "never ran because of a missing dependency" clearly
  separate from "ran and broke" in both the Evidence Bundle and the trace.

#### `ToolExecutor` error taxonomy, timeouts, and retries (Phase 14)

`ToolExecutor.execute()` (`src/listing_agent/agent/tool_executor/`) returns a
typed `ToolExecutionResult`, distinct from the tool-level `ToolResult`
(§4): it is what `evidence_collector` actually consumes.

```
ToolExecutionResult:
  status: "ok" | "error" | "skipped"
  output: <tool's typed output>  | null   # only when status == "ok"
  error: ToolError | null                 # only when status == "error"
  reason: string | null                   # only when status == "skipped"
  attempts: int                           # 1, or 2 if one retry was attempted

ToolError:
  code: ToolErrorCode
  message: string   # never contains secrets, auth headers, or raw config values
  retryable: bool   # derived from `code`, never stored independently
```

`ToolErrorCode` (`src/listing_agent/agent/tool_executor/errors.py`):
`invalid_input`, `not_found`, `ambiguous_result`, `insufficient_data`,
`timeout`, `rate_limited`, `provider_unavailable`, `internal_error`.

Retry policy — **at most one retry**, only for genuinely transient
failures:
- Retryable: `timeout`, `rate_limited`, `provider_unavailable`.
- Never retried: `invalid_input`, `not_found`, `ambiguous_result`,
  `insufficient_data` (a second attempt cannot change these).

`LocalToolExecutor` applies a conservative per-call timeout
(`Settings.tool_timeout_seconds`, default `8.0`s, env `TOOL_TIMEOUT_SECONDS`)
to every call regardless of backend.

This taxonomy is layered *on top of* — and does not replace — the Phase 12
business-outcome design in §4: a tool that runs successfully and simply
finds no match (`outcome="not_found"`/`"ambiguous"`/`"insufficient_data"`/
`"no_comparable_data"`) is still `ToolExecutionResult(status="ok", ...)`.
`ToolErrorCode` only classifies genuine execution failures — never used to
relabel a valid "we checked, and found nothing" business result.
`report_writer` must never conflate the two: a technical failure ("this
check could not be completed") is not the same statement as a business
"not found" result.

### `evidence_bundle_builder`

Input: raw evidence from `evidence_collector`.
Output: a normalized "evidence bundle" — the **only** input `report_writer`
is allowed to see besides the original listing.
Rules:
- Deterministic transformation only; no LLM calls.
- `EvidenceBundle.limitations` is simply the concatenation of every tool
  result's own `ToolEvidence.limitations` — the wording for each outcome
  (business "no answer", technical failure, dependency skip) is owned
  entirely by `agent.evidence_mapper` (below), not re-derived here.

#### `agent.evidence_mapper` — typed-output-to-evidence mapping (Phase 15)

`evidence_collector` converts every executed tool's `ToolExecutionResult`
into a `ToolEvidence` entry through
`agent.evidence_mapper.map_tool_evidence(call, result, claims)`. This is the
single place responsible for turning a tool's typed output into the
grounded facts `report_writer` is allowed to reason from:

- **`claim_id`** — links this evidence to the `Claim.id` (see below) it is
  relevant to, via a deterministic keyword match on `Claim.category` (e.g.
  category `"price"` → `price_signal`). `None` when no claim in the plan
  is a plausible match; evidence is never forced onto an unrelated claim.
- **`observations`** — a small, structured dict of *facts only*, per tool
  outcome (see the table below). Never an inferred conclusion: no
  `"family_friendly"`, no `"is_bargain"`, no `"school_quality"`, no
  `"walking_time_min"` — only counts, distances, prices, and typed
  outcomes exactly as the tool reported them.
- **`source`** — the tool's own source identifier, when it has a single
  one (`geo_resolver`, `price_signal`); `None` for `nearby_services` (each
  `NearbyPlace` has its own `source` instead — no single value to report).
- **`limitations`** — the tool's own disclaimers (e.g. "distances are
  straight-line, not walking time"; "not a professional appraisal") plus
  one clear, grounded caveat for any non-fully-confident outcome.

Outcome mapping (all tools' `status="ok"` cases keep the Phase 12
business-outcome design in §4; a `status="error"` result — regardless of
tool — always maps to `observations=None` and a limitation explicitly
stating the check could not be completed, e.g. *"this is not evidence
that no services exist nearby"*, never a negative/business conclusion):

| Tool              | `outcome`/status           | `observations` highlights                          |
|-------------------|-----------------------------|-----------------------------------------------------|
| `geo_resolver`     | `resolved`                  | `normalized_address`, `lat`, `lon`, `resolution_quality` |
| `geo_resolver`     | `ambiguous`                 | `candidates` (list)                                 |
| `geo_resolver`     | `not_found`                 | `outcome` only                                      |
| `geo_resolver`     | technical failure (`error`) | `None`                                               |
| `nearby_services`  | one or more results         | `place_count > 0`, per-category summaries           |
| `nearby_services`  | empty successful result     | `place_count == 0`, distinct from a technical failure |
| `nearby_services`  | technical failure (`error`) | `None`                                               |
| `price_signal`     | `compared`                  | price figures + `signal_strength: "normal"\|"weak"` (`"weak"` when `comparison_count < 3`) |
| `price_signal`     | `insufficient_data`         | `missing_fields`                                    |
| `price_signal`     | `no_comparable_data`        | `outcome` only                                      |
| `price_signal`     | technical failure (`error`) | `None`                                               |

`report_writer` is given only the *compact* projection of each
`ToolEvidence` (`tool`, `status`, `claim_id`, `observations`, `source`,
`limitations`, `error`, `error_code`, `reason`) — never the tool's raw
typed `output` (redundant with `observations`) and never executor
bookkeeping like retry `attempts`, keeping the payload compact and
guaranteeing the model only ever reasons from already-vetted facts. See
`src/listing_agent/llm/report_writer/bundle.py`.

**`Claim.id`**: `Claim` (produced by `triage_planner`) carries a `str` `id`
field, assigned a stable `"claim-{index}"` value by `plan_validator` if the
planner did not set one — the LLM output schema itself did not need to
change.

### `step_tracer`

Records a `StepTrace` entry for every step of the pipeline:
```
{
  "step": string,       // e.g. "triage_planner", "geo_resolver", "report_writer"
  "type": "llm" | "tool" | "deterministic",
  "status": "ok" | "error",
  "detail": object | null
}
```
Used to build the `steps` array returned by `/api/execute`.

---

## 3. LLM Module Contracts

Both LLM modules are accessed exclusively through the **abstract LLM
client** (`app/llm/client`). No module may call LLMod.ai directly.

### `triage_planner` (LLM module)

Input: the listing submitted by the caller.
Output: a plan object naming which tools to call and with what arguments,
drawn only from the tool whitelist. Must not fabricate evidence itself.

### `report_writer` (LLM module)

Input: the evidence bundle produced by `evidence_bundle_builder` (plus the
original listing for context).
Output: the final triage report (the `response` field of `/api/execute`).
Rules:
- Must ground all claims in the evidence bundle it was given.
- Must not call tools or the plan_validator — it only writes text/structured
  output from data it is handed.

### Abstract LLM Client (`app/llm/client`)

A single abstraction both LLM modules call through, so the concrete
LLMod.ai integration can be swapped/mocked in tests. Concrete request/
response schemas will be defined in Phase 1.

---

## 4. Tool Contracts (Nir's implementation surface)

These are the exact function-level contracts Nir's real implementations
must satisfy (formalized as Pydantic v2 models in
`src/listing_agent/tools/schemas.py`). Aviv provides mock implementations
conforming to the same contract for testing; Nir replaces the internals
only, not the interface. Every tool implements `BaseTool`
(`src/listing_agent/tools/base.py`) and returns a `ToolResult[...]`
envelope — see `docs/nir_handoff.md` for the full contract, including
error/skip behavior and required tests.

**`ToolResult.status` semantics** (frozen across all three tools):
`"ok"` means the tool ran and produced a well-formed answer — **including
a "no usable business answer" outcome** (address not found, zero nearby
services, insufficient listing data for a price comparison). The specific
business result is captured in a typed `outcome` field on the tool's
output model, never inferred from `status` or from a free-text `error`
string. `"error"` is reserved for genuine technical failures (e.g. an
unhandled upstream/data-source problem). `"skipped"` means the tool
intentionally did nothing for this input (e.g. `price_signal` on a rental
listing). See `docs/tool_contracts_examples.md` for worked examples.

### `geo_resolver`

- Input (`GeoResolverInput`): `address: str` (required), `city_hint: str
  | None` (optional, helps disambiguate).
- Output (`GeoResolverOutput`, wrapped in `ToolResult(status="ok")`):
  ```
  {
    "outcome": "resolved" | "ambiguous" | "not_found",
    "normalized_address": string | null,   // set only when outcome="resolved"
    "lat": float | null,                   // -90..90, set only when resolved
    "lon": float | null,                   // -180..180, set only when resolved
    "resolution_quality": "exact" | "approximate" | "low_confidence" | null,
    "neighborhood": string | null,
    "candidates": [string],                // set only when outcome="ambiguous"
    "source": string,
    "limitations": [string]
  }
  ```
- `outcome="ambiguous"` / `"not_found"` are still `ToolResult(status="ok")`
  — they are valid, typed "no single answer" results, not tool failures.
- Must return `ToolResult(status="error", ...)` only for a genuine
  technical failure (no exceptions leaking raw provider errors).

### `nearby_services`

- Input (`NearbyServicesInput`): `lat: float` (-90..90), `lon: float`
  (-180..180), `categories: list[NearbyServiceCategory]` (one or more;
  defaults to all supported categories), `radius_m: int = 500` (>0).
- Output (`NearbyServicesOutput`, wrapped in `ToolResult(status="ok")`):
  ```
  {
    "places": [
      { "name": string, "category": string, "distance_m": float, "source": string }
    ],
    "categories": [
      { "category": string, "count": int, "nearest_distance_m": float }
    ],
    "evidence_quality": "low" | "medium" | "high",
    "limitations": [string]   // must always disclose distances are
                              // straight-line, not walking distance/time
  }
  ```
- An empty result (`places=[]`, `categories=[]`) for a location with no
  coverage is a valid `status="ok"` result — distinguishable from a
  genuine technical failure (`status="error"`).

### `price_signal`

- Input (`PriceSignalInput`): `normalized_address: str`,
  `listing_type: "purchase" | "rental" | "unknown"`,
  `asking_price: float | None` (>0), `area_sqm: float | None` (>0),
  `rooms: float | None` (>0), `neighborhood: str | None` (optional
  comparables lookup key, typically from `geo_resolver`'s output).
- Output (`PriceSignalOutput`, wrapped in `ToolResult(status="ok")`):
  ```
  {
    "outcome": "compared" | "insufficient_data" | "no_comparable_data",
    "asking_price_per_sqm": number | null,
    "median_comparison_price_per_sqm": number | null,
    "reference_range_low_per_sqm": number | null,
    "reference_range_high_per_sqm": number | null,
    "deviation_percent": number | null,
    "comparison_count": int,
    "missing_fields": [string],   // set only when outcome="insufficient_data"
    "source": string,
    "limitations": [string]   // must always disclose this is not a
                              // professional/certified appraisal
  }
  ```
- Rental listings must be **skipped** (`ToolResult(status="skipped", ...)`),
  not evaluated.
- `outcome="insufficient_data"` (missing `asking_price`/`area_sqm`) and
  `outcome="no_comparable_data"` (no comparison data for the
  address/neighborhood) are still `ToolResult(status="ok")` — valid,
  typed "no comparison possible" results, not tool failures.

All three tools:
- Must be async Python callables (`execute(input_model) -> ToolResult`)
  matching the Pydantic v2 schemas in `src/listing_agent/tools/schemas.py`.
- Must never raise an unhandled exception — all failures/skips are
  returned as a structured `ToolResult`.
- Must not call an LLM.
- Must not require a database — any data source Nir uses must be
  self-contained (files, APIs, computation), not a persistent DB service.



---

## 5. Internal Tool Tracing and Module-Name Constants (Phase 16)

These additions are purely internal/observability-oriented: they do not add,
remove, or rename any top-level field of the public `/api/execute` response
(still exactly `status`, `error`, `response`, `steps`), and the public
`StepTrace` shape (`step`, `type`, `status`, `detail`) is unchanged.

### `agent.module_names`

A single source of truth for the frozen module/tool names used
throughout the codebase (`StepTrace.step` values, `docs/contracts.md`,
`scripts/generate_architecture_image.py`, `GET /api/agent_info` examples):

```
TRIAGE_PLANNER = "triage_planner"
REPORT_WRITER = "report_writer"
AGENT_ORCHESTRATOR = "agent_orchestrator"
PLAN_VALIDATOR = "plan_validator"
EVIDENCE_COLLECTOR = "evidence_collector"
EVIDENCE_BUNDLE_BUILDER = "evidence_bundle_builder"
STEP_TRACER = "step_tracer"
CONTEXT_REUSE = "context_reuse"          # Phase 13 - internal only, never in
                                          # the public /api/execute steps
GEO_RESOLVER = "geo_resolver"
NEARBY_SERVICES = "nearby_services"
PRICE_SIGNAL = "price_signal"

ALL_MODULE_NAMES: frozenset[str]  # all eleven of the above
```

Existing per-module `MODULE_NAME` constants (`llm/triage_planner/planner.py`,
`llm/report_writer/writer.py`) now alias these values instead of duplicating
the literal string.

### `ToolTraceRecord` (`agent.step_tracer.ToolTraceRecord`)

An internal, typed trace record built directly from a tool call's *real*
input and *real* result (timed with `time.perf_counter()` around the actual
`await tool_executor.execute(...)` call in `evidence_collector`, never
reconstructed afterward):

```
{
  "trace_type": "tool",
  "tool": string,                       // e.g. "geo_resolver"
  "request_summary": object | null,     // sanitized input_model.model_dump()
  "response_summary": object | null,    // sanitized {reason, error, observations}
  "status": "ok" | "error" | "skipped",
  "duration_ms": number,                // 0.0 when no real call was attempted
  "error_code": string | null
}
```

- Never contains authorization headers, API keys, environment values,
  access tokens, or raw provider payloads — request/response summaries are
  built only from already-validated Pydantic tool models and the Phase 15
  evidence-mapper's normalized fields, then still passed through the
  existing `StepTracer._redact()` helper as defense-in-depth.
- Recorded via `StepTracer.record_tool_trace(trace)`, which both appends the
  typed record to `StepTracer.get_tool_traces()` (for direct test
  assertions) and appends the existing `StepTrace(type="tool", ...)` to the
  same `steps` list `/api/execute` returns, with the full `ToolTraceRecord`
  nested (redacted) inside `StepTrace.detail`. A `status="skipped"` tool
  trace maps to the public `StepTrace.status="ok"` (the step deliberately
  did not run; it did not fail).

---

## 6. Request Limits, Deduplication, and Execution Deadlines (Phase 17)

Deterministic, pre-flight guardrails that protect **cost** (LLM token
spend), **latency** (buyer-facing response time), and **reliability** (no
single slow/duplicate call can degrade the whole request). None of these
are enforced with an LLM call - all are plain Python checks, applied as
early as possible so an invalid request never reaches the expensive parts
of the pipeline.

### Configured limits (`Settings`, see `.env.example` for env var names)

| Setting                              | Default | Protects                                             |
|---------------------------------------|---------|-------------------------------------------------------|
| `max_prompt_length`                   | 4000    | LLM token cost; checked *before* `triage_planner` runs |
| `max_claims`                          | 8       | Evidence Bundle / `report_writer` payload size         |
| `max_tool_calls`                      | 3       | Tool-execution cost/latency (already ≤3 whitelisted tools) |
| `max_total_tool_execution_seconds`    | 20.0    | Cumulative tool latency across one request             |
| `llm_request_timeout_seconds`         | 300.0   | Overall `/api/execute` wall-clock time; configurable via `LLM_REQUEST_TIMEOUT_SECONDS`, defaults to the assignment's own 300s "no more than 5 minutes to get an answer" ceiling |

These are conservative *defaults* appropriate for a university project's
real, mocked tool latencies - not tuned for a production SLA. They are
all overridable via environment variables/`Settings` without code changes.

### Enforcement points

- **Prompt length**: `agent.request_limits.check_prompt_length` runs in
  `run_agent_pipeline` before the first (`triage_planner`) LLM call. A
  violation raises `RequestLimitError`, mapped by `POST /api/execute` to
  `{"status": "error", "error": "<human-readable message>", "response":
  null, "steps": []}` - no LLM call is ever made.
- **Claims cap**: `plan_validator.validate_plan(..., max_claims=...)`
  deterministically truncates `TriagePlan.claims` to the configured limit
  (keeping the first N), recording `"additional_claims_truncated"` in
  `missing_information` so the drop is never silent.
- **Tool-calls cap**: `plan_validator.validate_plan(..., max_tool_calls=...)`
  caps `TriagePlan.tool_calls` the same way validate_plan already did in
  earlier phases (default 3; only three tools are ever whitelisted, so
  this is mostly a defensive ceiling).
- **Tool-call deduplication**: `plan_validator._dedupe_by_tool` already
  removes duplicate calls to the same tool name; because
  `evidence_collector` always derives each tool's actual input
  deterministically from `PropertyDetails`/resolved coordinates - never
  from the planner's free-form `ToolCall.arguments` - two planner-proposed
  calls to the same tool are always equivalent and only ever execute once.
- **Shared evidence across claims**: `evidence_mapper.find_relevant_claim_ids`
  links one deduplicated tool result to *every* plausible claim (not just
  the first match). `ToolEvidence.claim_ids: list[str]` carries the full
  set; `ToolEvidence.claim_id` is kept, unchanged, as the first entry for
  backward compatibility.
- **Total tool-execution time budget**: `evidence_collector.collect_evidence`
  tracks cumulative wall-clock time actually spent executing tool calls. If
  the next planned call would start after the budget is already spent, it
  is recorded as `status="skipped"` (never attempted) with a clear
  `limitations` entry - the whole request is never aborted, only the
  remaining not-yet-useful calls are skipped.
- **Overall request deadline**: `POST /api/execute` wraps the entire
  pipeline call in `asyncio.wait_for(..., timeout=settings.llm_request_timeout_seconds)`.
  On timeout, it returns `{"status": "error", "error": "Request exceeded
  the time limit of Ns.", "response": null, "steps": [...]}` using
  whatever LLM steps had already completed - never an unhandled hang.

### Normal-path LLM usage is unchanged

None of the above adds an LLM call: the normal path is still exactly one
`triage_planner` call followed by deterministic tool execution and exactly
one `report_writer` call.
