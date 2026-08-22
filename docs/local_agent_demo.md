# Running the agent as a real interactive conversation

This is a runbook, not a new feature. It documents how to actually *run* the
existing agent locally — real `triage_planner`/`report_writer` LLM calls,
local (mock) tools, the existing GUI, and the existing stateful follow-up
context — so you can paste a listing, ask questions, and keep asking
follow-up questions in the same conversation.

Nothing here changes the architecture. It only turns on the real LLM
backend and documents the existing `TOOL_BACKEND` switch and the existing
`conversation_id` behavior.

## 1. Current runnable architecture

```text
Browser (gui/index.html + gui/static/app.js)
    ↓ POST /api/execute {prompt, conversation_id?}
FastAPI (src/listing_agent/main.py, src/listing_agent/api/routes.py)
    ↓
agent_orchestrator.run_agent_pipeline
    ↓
ConversationContext lookup (if conversation_id given)      -- agent/conversation
    ↓
triage_planner (LLM #1, real via LLModClient)               -- llm/triage_planner
    ↓
hydrate_plan_from_context (fills missing PropertyDetails)    -- agent/conversation
    ↓
plan_validator (incl. Phase 0 price-provenance guard)        -- agent/plan_validator
    ↓
collect_evidence_with_context                                -- agent/conversation/cache.py
    │   - reuses cached geo/nearby/price Tool Results when the
    │     canonical typed Tool Input is unchanged
    │   - otherwise calls ToolExecutor.execute
    ↓
ToolExecutor  (TOOL_BACKEND=mock -> LocalToolExecutor -> local ToolRegistry;
               TOOL_BACKEND=mcp  -> McpToolExecutor, not implemented yet)
    ↓
EvidenceBundle                                                -- agent/evidence_bundle_builder
    ↓
report_writer (LLM #2, real via LLModClient)                  -- llm/report_writer
    ↓
{status, error, response, steps}  -- unchanged 4-field contract
    ↓
ConversationContext saved (if conversation_id given)
```

Exactly two LLM calls happen per turn (`triage_planner`, `report_writer`),
regardless of whether `conversation_id` is used. The tool backend
(local today, MCP later) is selected once, in
`agent/tool_executor/dependency.py::get_tool_executor`, and nothing above
that seam (planner, validator, context/cache, evidence bundle, report
writer, API, GUI) changes when the backend changes.

## 2. Environment variables

Set these (values are yours — never commit real secrets):

```bash
# Real LLM (LLM_MODE=fake is the deterministic/offline default used by tests)
export LLM_MODE=llmod
export LLMOD_API_KEY=...
export LLMOD_BASE_URL=...
export LLMOD_MODEL=...

# Local tool backend for this demo (also the default if unset)
export TOOL_BACKEND=mock
export TOOLS_MODE=mock
```

Or put the same keys in a `.env` file at the repo root (see
`.env.example` for the full list, including request-limit tunables).
`Settings` (`src/listing_agent/config.py`) loads `.env` automatically.

## 3. Start command

```bash
uv run uvicorn app:app --reload
```

(`app.py` re-exports the FastAPI app from `src/listing_agent/main.py`.)

## 4. GUI URL

```text
http://127.0.0.1:8000/
```

The page shows a conversation indicator and a "New conversation" button
(`gui/static/app.js`): a `conversation_id` is generated client-side
(`sessionStorage`) the first time you submit a question and sent with
every subsequent `/api/execute` call in the same browser tab, until you
click "New conversation" or close the tab.

## 5. Demo listing

```text
דרך הטייסים 8, תל אביב
4 חדרים, 111 מ"ר
מחיר מבוקש: 4,700,000 ₪
```

This address, and a small local nearby-services/price dataset for it
(park, school, daycare, public_transport; Ramat Aviv price comparables),
were added to the existing mock tool implementations
(`src/listing_agent/tools/mock_implementations/`) specifically so this
demo has meaningful, deterministic local data. See
`tests/tools/test_mock_geo_resolver.py`,
`test_mock_nearby_services.py`, `test_mock_price_signal.py` for the exact
values.

## 6. Demo conversation (paste into the GUI, one at a time, same tab)

**Turn 1** (paste the full listing above together with the question):

```text
דרך הטייסים 8, תל אביב, 4 חדרים, 111 מ"ר, מחיר מבוקש 4,700,000 ₪.
האם יש פארק קרוב?
```

**Turn 2** (no listing text needed — same conversation):

```text
כמה רחוק?
```

**Turn 3**:

```text
ומה לגבי בתי ספר?
```

**Turn 4**:

```text
האם המחיר הוגן?
```

**Turn 5**:

```text
ומה אם המחיר יהיה 4,400,000 ₪?
```

## 7. Expected internal behavior

| Turn | Question | Reused | Executed | New external tool calls |
|------|----------|--------|----------|--------------------------|
| 1 | האם יש פארק קרוב? | — | geo_resolver, nearby:park | 2 |
| 2 | כמה רחוק? | geo_resolver, nearby:park | — | 0 |
| 3 | ומה לגבי בתי ספר? | geo_resolver | nearby:school | 1 |
| 4 | האם המחיר הוגן? | geo_resolver | price_signal | 1 |
| 5 | ומה אם המחיר 4.4 מיליון? | geo_resolver | price_signal (new input) | 1 |

This is not exposed in the public API response (the four-field contract
never changes), but is visible two ways during local development:

- **Server console/log**: `agent_orchestrator` logs one INFO line per
  turn: `conversation_id=... reused=[...] executed=[...]
  tool_calls_saved=N`. Visible by default under `uvicorn`'s standard
  logging.
- **`context_reuse` internal trace step**: recorded by `StepTracer` on
  every stateful turn (`agent/module_names.py::CONTEXT_REUSE`), inspectable
  from Python/tests via `StepTracer.get_steps()`, but intentionally
  filtered out of `/api/execute`'s public `steps` (which only ever
  contains the two LLM steps).

## 8. Reset (start a fresh conversation)

- In the GUI: click **"New conversation"**.
- Over HTTP: simply send a request with no `conversation_id`, or a new
  random one — a different id never reuses another conversation's
  context (enforced by `InMemoryContextStore`, keyed by id).

Context lives only in the running process's memory
(`InMemoryContextStore`): restarting the server clears every
conversation. This is intentional — see `docs/follow_up_context_plan.md`.

## 9. API-only fallback (no GUI)

Useful when GUI debugging gets in the way, e.g. during MCP integration
work. Uses the exact same `/api/execute` contract and the same
`conversation_id` field the GUI sends — no new endpoint.

```bash
CID="demo-$(date +%s)"

# Turn 1
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"דרך הטייסים 8, תל אביב, 4 חדרים, 111 מ\\\"ר, מחיר מבוקש 4,700,000 ₪. האם יש פארק קרוב?\", \"conversation_id\": \"$CID\"}"

# Turn 2 (same $CID, no listing text)
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"כמה רחוק?\", \"conversation_id\": \"$CID\"}"

# Turn 3
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"ומה לגבי בתי ספר?\", \"conversation_id\": \"$CID\"}"

# Turn 4
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"האם המחיר הוגן?\", \"conversation_id\": \"$CID\"}"

# Turn 5
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"ומה אם המחיר יהיה 4,400,000 ₪?\", \"conversation_id\": \"$CID\"}"
```

Each response is the exact `{status, error, response, steps}` shape; the
`reused`/`executed`/`tool_calls_saved` detail described in §7 is only in
the server's own logs/internal trace, not in these HTTP responses.

## 10. Switching to MCP later

One env var, at the `ToolExecutor` seam only
(`agent/tool_executor/dependency.py`):

```bash
export TOOL_BACKEND=mcp
export MCP_SERVER_URL=...
export MCP_REQUEST_TIMEOUT_SECONDS=10
```

`McpToolExecutor` currently raises `NotImplementedError` on `execute()` —
it exists only to prove the seam and configuration validation, not to
talk to a real MCP server yet (that is future work, out of scope here). No other module changes when this switch flips: the
planner, validator, `ConversationContext`/cache, evidence bundle,
report writer, API, and GUI all depend only on the `ToolExecutor`
interface, never on `LocalToolExecutor` or `McpToolExecutor` directly.
