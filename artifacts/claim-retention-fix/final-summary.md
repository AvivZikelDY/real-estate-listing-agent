# Claim-retention fix — final summary

## Regression fixed

`plan_validator._cap_claims` capped claims by raw LLM list order with no
prioritization, dropping the kindergarten (`nearby_amenities`),
rent-then-purchase (`transaction_structure`), parquet-after-delivery, and
asking-price claims for the Gonen 14 listing while a subjective marketing
claim and a duplicate address claim survived.

## First point of failure

`plan_validator._cap_claims` (called before `_ensure_nearby_services_for_
known_claims`), confirmed in `artifacts/claim-loss-diagnosis/root-cause-
report.md`.

## Files changed

- `src/listing_agent/agent/claim_categories.py` — added the
  `transaction_structure` canonical category/aliases and a new
  `verification_decision()` helper implementing the Pre-Tool Triage's
  7-value vocabulary (`LISTING_FACT` / `CHECKABLE_WITH_TOOL` /
  `REQUIRES_SELLER_OR_DOCUMENT` / `REQUIRES_CONTRACT_REVIEW` /
  `SUBJECTIVE` / `OUT_OF_SCOPE`), reusing the existing
  `TOOLS_BY_VERIFIABLE_CATEGORY` / `_LISTING_FACT_CATEGORIES` tables rather
  than duplicating them.
- `src/listing_agent/agent/plan_validator/validator.py` — added
  `_dedupe_claims`, `_prioritize_claims`, `_claim_priority`, a
  `_GOAL_RELEVANT_CATEGORIES` mapping, and `_truncation_summary`;
  `validate_plan` now runs dedupe -> prioritize -> cap (instead of raw-order
  cap only), and `_ensure_nearby_services_for_known_claims` now aggregates
  *every* retained `nearby_amenities` claim's text/ids instead of only the
  first one.
- `src/listing_agent/agent/tool_input_builder.py` — added the missing
  `"גני"/"גני ילדים"` (regular, non-final nun) daycare alias, and
  `_linked_nearby_claim_text` now combines every linked claim's text
  instead of only the first match.
- `src/listing_agent/llm/triage_planner/prompts.py` — added atomic-claim,
  no-duplicate-address, and `transaction_structure` guidance (a
  contributing fix per the root-cause report's R3, not the primary fix -
  the deterministic layer remains the actual safety mechanism).
- `tests/agent/test_claim_retention_regression.py` (new) — the Gonen 14
  regression (pre-fix failure proof + post-fix retention/tool-plan
  assertions) plus Phase 11 scenarios A-F.

## Why this is not Gonen-specific

No literal `"גונן"`/listing text, no exact claim-index checks, and no
address-specific branching anywhere in production code. The priority
function operates purely on `claim.type` and canonical category; the
dedup rule operates purely on canonical category + normalized text. Both
were exercised and passed on five independent synthetic scenarios
(A-F) using entirely different addresses, categories, and claim counts,
and on the real LLM's own (differently-worded, differently-atomized)
Gonen 14 output.

## Before / after (Gonen 14, exact diagnosed 12-claim fixture)

Before (raw-order cap): retained c1-c8 (2 duplicate address claims + 1
subjective marketing claim survive); dropped c9 (parquet), c10 (nearby
kindergarten/parking), c11 (transaction structure), c12 (asking price).
No `nearby_services` call.

After (dedupe -> prioritize -> cap): retained c10 (nearby_amenities), c2/
c5/c7/c9/c11 (seller/document facts incl. parquet and transaction
structure), c1/c4 (one address, rooms) = 8 claims. Removed: the duplicate
address (c3), the subjective marketing claim (c8), size_sqm (c6, redundant
with `PropertyDetails.sqm`), and price (c12, redundant with
`PropertyDetails.asking_price`). Tool plan: `geo_resolver` ->
`nearby_services(categories=["daycare"])`. No `price_signal` (no
comparative claim, no `price_validation` goal).

## Real-LLM verification (one authorized call)

The real LLMod planner, run once against the literal Gonen 14 listing text
with the updated prompt, produced exactly 8 already-atomized claims (3
`nearby_amenities`, 3 `transaction_structure`, 2 `condition`) - no
truncation was even needed. The fixed `validate_plan` retained all 8 and
produced the same semantically-correct tool plan:
`geo_resolver` -> `nearby_services(categories=["daycare"])`, with no
`price_signal`. See `gonen-real-llm-verification-run.json` and
`gonen-after.txt`.

## New scenarios (A-F)

All PASS - see `tests/agent/test_claim_retention_regression.py`.

## Existing scenarios (1-10)

All PASS - `tests/evaluation/test_evaluation_suite.py` (10/10) and both
`scripts/inspect_listing_pipeline.py --stage planner|evidence --scenario
all --fake-llm` runs (10/10 each).

## Testing

- `ruff check .` — all checks passed.
- `pytest` — 451 passed, 1 skipped (pre-existing, unrelated).
- `pytest tests/agent/test_plan_validator.py` — all passed (pre-existing
  suite untouched).
- `pytest tests/evaluation tests/scripts` — all passed.

## Confirmations

- No MCP called.
- No external provider called (only the existing, already-authorized
  `triage_planner` LLMod call - one real call, no retries).
- No extra production LLM call added (still exactly `triage_planner` +
  `report_writer`; `report_writer` was never called in this task).
- No public API contract changed.

## Remaining limitations

- `MockGeoResolver`'s fixed dataset has no coordinates for "גונן 14" itself,
  so this offline check could not fully resolve `nearby_services`'
  `lat`/`lon` - a mock-data limitation, not a defect in the fix.
- The planner prompt change is a *contributing* improvement, not a
  guarantee: the deterministic dedup/prioritize/cap layer remains the
  actual safety net for any wording the real LLM produces.
- `_GOAL_RELEVANT_CATEGORIES` (priority 1) is a small, fixed table; it does
  not yet cover every possible future `BuyerGoal`/category combination.

## Verdict

CLAIM_RETENTION_FIXED
