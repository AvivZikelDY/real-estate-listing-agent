# Evaluation & Regression Testing

This document describes the table-driven evaluation suite in
`tests/evaluation/` (scenario table: `tests/evaluation/scenarios.py`;
test runner: `tests/evaluation/test_evaluation_suite.py`).

The suite exists to catch regressions in the agent's *decision logic*
(goal detection, tool selection rules, missing-data handling, failure
handling, deduplication) independently of the specific wording an LLM
happens to produce on any given day.

## How it works

Every scenario is an `EvalScenario` entry that fully specifies one
end-to-end run:

- `input_prompt` — the raw text a user would type into the GUI.
- `plan` — the exact `TriagePlan` that `triage_planner` is assumed to
  have produced for this input. This is fed into a `FakeLLMClient`, so
  **no real LLM call is ever made**.
- `report` — the exact text `report_writer` is assumed to have produced,
  also fed through `FakeLLMClient`.
- `expected_goal` — the `buyer_goal` the plan should resolve to.
- `expected_tools` — tools that must be *attempted* (present in
  `EvidenceBundle.executed_tools`), regardless of whether they succeed.
- `forbidden_tools` — tools that must **never** be attempted at all.
- `expected_missing_information` — items that must appear in
  `EvidenceBundle.missing_information`.
- `expected_report_fragments` — substrings the final report must contain.
- `registry_factory` — which `ToolRegistry` to run against (the standard
  mock registry, or one with an injected tool failure).
- `semantic_assertion` — an optional extra callable for checks that don't
  fit the table shape (e.g. "`nearby_services` was never attempted at
  all", not just "not forbidden").

Each scenario is run through the **real** `agent_orchestrator`
(`run_agent_pipeline`), with only the LLM client and tool registry
swapped for offline/deterministic doubles. This means the suite
exercises the actual `plan_validator`, `evidence_collector`,
`evidence_bundle_builder`, and `report_writer`-wiring code — only the
two LLM calls themselves are canned.

## Required scenarios

1. **`listing_only_fast_triage`** — a listing with no address and no
   buyer question. Verifies fast triage requires no tools at all, and
   that a missing address is recorded as missing information.
2. **`family_suitability`** — a family-suitability question with a known
   address, full rooms/sqm. Verifies `geo_resolver` and
   `nearby_services` both run, and `price_signal` is correctly excluded.
3. **`price_validation_complete_fields`** — a purchase listing with
   asking price, sqm, and address, asking "is this a fair price".
   Verifies `geo_resolver` and `price_signal` both run.
4. **`price_validation_missing_sqm`** — same as above but `sqm` is
   missing. Verifies `price_signal` is correctly skipped and `sqm` is
   recorded as missing information (the report must ask for it instead
   of guessing).
5. **`rental_listing_price_question`** — a rental listing asking about
   price. Verifies `price_signal` is **never** selected for rentals,
   even when the buyer explicitly asks about price.
6. **`subjective_beautiful_apartment_claim`** — a listing containing
   only subjective claims ("beautiful", "charming"). Verifies subjective
   claims never trigger tool calls and are not reported as verified.
7. **`unknown_address`** — an address `geo_resolver` cannot resolve.
   Verifies the pipeline is resilient to a resolvable-but-unknown
   address: `geo_resolver` is attempted and fails, dependent tools
   (`nearby_services`, which needs coordinates) are never attempted, and
   the failure becomes a limitation rather than an unhandled exception.
8. **`nearby_services_failure`** — a genuine tool outage (as opposed to
   "no data for this address", which the real mock reports as a
   successful empty result). Verifies a hard tool failure still produces
   a complete Evidence Bundle with a limitation, and the rest of the
   pipeline (including `price_signal`, which is independent) is
   unaffected.
9. **`below_market_price_claim`** — a listing that claims to be "below
   market price". Verifies that a *seller's claim* about price is never
   auto-confirmed; only the actual `price_signal` tool result may be
   cited as supporting evidence, and the claim itself is treated as
   unverified pending that evidence.
10. **`duplicate_tool_calls_from_planner`** — a planner output that
    (incorrectly) proposes the same tool call more than once. Verifies
    `plan_validator` deduplicates so the tool actually executes only
    once.

## Cross-cutting invariants

Beyond the ten required scenarios, every scenario in the table is also
checked against these invariants (each implemented as its own
parametrized test function, run across all scenarios):

- **At most two normal LLM calls, in the correct order.** Every scenario
  must produce exactly the `triage_planner` → `report_writer` sequence
  in `StepTracer`, never more.
- **No forbidden tool ever executes**, and no forbidden tool ever has an
  entry in `EvidenceBundle.tool_results` — not just "not expected to
  succeed."
- **Every expected tool's result appears in the Evidence Bundle** — the
  bundle is the single source of truth `report_writer` sees; if a tool
  ran, its result must be traceable there.
- **Failed or skipped tools always produce a limitation string** —
  silent tool failures are not acceptable.
- **Failed/skipped tool evidence never carries fabricated `output`
  data** — this is the structural guarantee that the final report can
  never present a failed check as supporting evidence: if there's no
  `output`, there's nothing for `report_writer` to cite as "supported."
- **`POST /api/execute` always returns exactly the four required
  top-level keys** (`status`, `error`, `response`, `steps`), verified
  end-to-end through `TestClient` with dependency overrides, for every
  scenario in the table.

## Listing + user-question pipeline diagnostics

`scripts/inspect_listing_pipeline.py` is a manual/CI diagnostic and
evaluation runner for the **planner → validation → (optional) tool
execution → evidence** portion of the pipeline. It complements the
table-driven suite above by printing a full, human-readable trace of
*why* the pipeline made each decision, and by exercising the effect a
free-text buyer question has on goal detection, claim relevance, and
tool selection. It always **stops before `report_writer`** and never
calls it.

Scenario fixtures live in `scripts/listing_pipeline_scenarios.py`
(10 scenarios: listing-only triage, family suitability, market-price
questions, question-derived nearby claims, walking-time/quality/
building-attribute questions that current tools cannot answer, missing
price information, a narrowed park-only question, and a general
opinion question). Each scenario has an expected goal, expected/
forbidden tools, and expected answerability, used both by the CLI's
own invariant checks and by `tests/scripts/test_inspect_listing_pipeline.py`.

Two stages are supported:

- `--stage planner` — runs `run_triage_planner` and `validate_plan`
  only; prints claims, the final validated tool plan, and the exact
  typed inputs `build_tool_input()` would produce. Never touches tool
  execution.
- `--stage evidence` — additionally runs the validated plan through
  `LocalToolExecutor` against the **mock** tool registry (forced to
  `tools_mode="mock"` regardless of ambient settings), then
  `collect_evidence` and `build_evidence_bundle`. Never uses the live
  tools or any external provider.

Example commands:

```bash
# Planner-only trace for every scenario, deterministic Fake LLM
python scripts/inspect_listing_pipeline.py --stage planner --scenario all --fake-llm

# Full evidence trace (including mock tool execution) for every scenario
python scripts/inspect_listing_pipeline.py --stage evidence --scenario all --fake-llm

# One scenario against the real LLMod.ai planner (single LLM call)
python scripts/inspect_listing_pipeline.py --stage planner --scenario 2 --real-llm

# Ad-hoc listing + question, JSON output
python scripts/inspect_listing_pipeline.py \
  --stage evidence --listing "..." --question "..." --fake-llm --json
```

Notes on real-LLM usage:

- `--real-llm` uses the real `LLModClient` and real environment
  configuration; it fails clearly (exit code 2) if credentials are
  missing rather than silently falling back to the Fake LLM.
- Exactly one planner LLM call is made per scenario; `report_writer` is
  never invoked in either stage.
- Running `--scenario all` with `--real-llm` requires the explicit
  `--allow-paid-batch` flag; otherwise real mode is restricted to one
  selected scenario at a time.

## Known limitations

- **No real LLM behavior is tested.** `FakeLLMClient` returns
  pre-configured, hand-authored responses. This suite validates the
  deterministic pipeline logic (validation, tool orchestration, evidence
  assembly) — it does **not** test whether a real LLM, given the system
  prompts, will reliably produce a well-formed `TriagePlan` or a
  faithful report. That requires separate, non-deterministic evaluation
  against the real LLMod.ai API, which is out of scope here.
- **No real tool data is tested.** All scenarios run against
  `MockGeoResolver`/`MockNearbyServices`/`MockPriceSignal` (or the
  injected-failure double). Real-world geocoding, POI, and price-comps
  accuracy is validated separately, against the live implementations, per
  `docs/tools.md`.
- **No adversarial or prompt-injection testing.** Scenarios use
  well-formed, good-faith listing text. Robustness against malicious or
  malformed input is not covered here.
- **No load or performance testing.** The suite checks correctness of a
  single request at a time, not latency, concurrency, or throughput.
- **Report fragment checks are substring-based**, not semantic. They
  catch obviously wrong or missing content but cannot verify that a real
  LLM's phrasing conveys the same meaning as the canned text used here.
