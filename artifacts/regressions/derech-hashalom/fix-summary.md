# Fix summary: multi-entity `size_sqm` claim-dedup regression (דרך השלום)

Date: 2026-08-09
Diagnostic runner: `scripts/pre_tool_triage.py` internals (`run_pre_tool_triage`),
replayed via a `FakeLLMClient` seeded with the exact 15 raw claims captured in
`artifacts/regressions/derech-hashalom/real-planner-run.json` (no new real LLM
call - see "Provenance" below).

Saved artifacts:
- `artifacts/regressions/derech-hashalom/after-fix.json` (generated, post-fix)
- `artifacts/regressions/derech-hashalom/pre-tool-triage.json` (unchanged -
  original pre-fix artifact, kept as evidence of the original failure)
- `artifacts/regressions/derech-hashalom/real-planner-run.json` (unchanged)
- `artifacts/regressions/derech-hashalom/fix-summary.md` (this file)

## Provenance

| Field | Value |
|---|---|
| llm_calls (this fix task) | 0 real |
| planner_mode | fake (FakeLLMClient replaying the already-captured 15 raw claims) |
| tools_executed | false |
| mcp_called | false |
| external_provider_called | false |
| report_writer_called | false |

## Root cause

`_dedupe_claims`'s `_REDUNDANT_WITH_PROPERTY_DETAILS` rule treated **any**
second-or-later claim sharing a canonical "listing-fact" category
(`address`, `price`, `rooms`, `size_sqm`) as a redundant restatement of the
single typed `PropertyDetails` field for that category - regardless of
whether the claim's own text actually named the same value. For a
single-property listing this coincidentally worked; the moment a listing
legitimately describes a second entity/state (a future/replacement unit, a
balcony, a comparison unit) with its *own* distinct size/price/room facts in
the same category, the rule silently dropped it with no
`missing_information` marker and no priority demotion.

## Old rule

```python
if canonical in _REDUNDANT_WITH_PROPERTY_DETAILS:
    if canonical in seen_property_detail_categories:
        continue  # dropped - by category alone
    seen_property_detail_categories.add(canonical)
```

## New rule

For `price` / `rooms` / `size_sqm` (categories with a comparable *numeric*
`PropertyDetails` field), a claim is only treated as a redundant duplicate
when its own text contains a number that actually matches the
`PropertyDetails` value (`_claim_restates_property_detail_value`, backed by
a general, listing-independent `_extract_numbers` regex - no keyword/entity
list, no listing-specific text or address logic). A same-category claim
naming a *different* value now always survives dedup, however many other
claims already matched.

`address` is left unchanged (still category-only, first-wins): it has no
comparable numeric value, and this is exactly the behavior the existing
Gonen 14 regression (`test_gonen_14_duplicate_address_claim_does_not_survive`)
already locks in and must keep passing.

```python
if canonical in _REDUNDANT_WITH_PROPERTY_DETAILS:
    if canonical in _NUMERIC_PROPERTY_DETAIL_FIELDS:
        if _claim_restates_property_detail_value(canonical, claim.text, property_details):
            if canonical in seen_property_detail_categories:
                continue  # true duplicate of PropertyDetails value - dropped
            seen_property_detail_categories.add(canonical)
        # else: distinct value -> never dropped by this rule
    else:
        if canonical in seen_property_detail_categories:
            continue  # address: unchanged, category-only
        seen_property_detail_categories.add(canonical)
```

## Temporal/entity classification

No new public schema field, no new `PropertyDetails` field, and no new
`ClaimSource`/category value was introduced. The existing `PropertyDetails`
numeric fields (`sqm`, `rooms`, `asking_price`) already carry the
"current-property fact" state; a claim's own text is compared directly
against that value instead of introducing a `CURRENT`/`FUTURE_OR_REPLACEMENT`
tag. This is the smallest correct model boundary: it fixes the captured
invariant without expanding the entity model, and is inherently
address/listing-independent (pure numeric-value comparison).

## Before / after: Derech Hashalom claim retention

Both before and after the fix, the final **retained** set (8 claims, capped
by `MAX_CLAIMS`) is unchanged - the balcony claim was already going to be
capped by claim-cap capacity even once it correctly survives dedup (a
separate, pre-existing, acceptable mechanism; see Step 8/derech-hashalom
evaluation-summary.md). What changed is *why* it was removed:

| | before | after |
|---|---|---|
| `"82 מ\"ר"` | removed: `capped_max_claims` | removed: `capped_max_claims` (unchanged) |
| `"פלוס 12 מ\"ר מרפסת"` | removed: **`deduplicated`** (bug) | removed: `capped_max_claims` (correct - no longer silently deduplicated) |

`deduplicated_count` before: 14/15 (1 wrongly removed by dedup).
`deduplicated_count` after: 15/15 (0 wrongly removed by dedup) - all 7
removals downstream of dedup are now uniformly attributed to
`capped_max_claims`.

## Current vs. future property separation

`PropertyDetails.sqm = 60.0` (current apartment) is untouched, as before.
The future replacement unit's own `"82 מ\"ר"` and its `"12 מ\"ר מרפסת"`
balcony claim are both distinct `size_sqm`-category claims that survive
`_dedupe_claims` (neither restates 60); both are still subject to the same
`MAX_CLAIMS` cap as before, which is out of scope for this fix.

## Cases A-E (synthetic, address-independent)

All added to `tests/agent/test_multi_entity_claim_dedup_regression.py`:

- **A** (current 70 sqm vs. future 95 sqm): both survive. PASS
- **B** (current 70 sqm duplicate, two phrasings): only 1 `size_sqm` claim
  survives. PASS
- **C** (current 60 / future 82 / future 12 balcony): all three distinct.
  PASS
- **D** (current floor 3 vs. future floor 5): floor has no
  `PropertyDetails` field and was never on the `_REDUNDANT_WITH_PROPERTY_DETAILS`
  path to begin with - both survive unconditionally (documented, no new
  dedup rule needed). PASS
- **E** (Gonen-style duplicate `address` claims): still deduplicated to 1 -
  the pre-existing, unchanged category-only address rule. PASS

## Gonen regression

`tests/agent/test_claim_retention_regression.py` - all tests still pass
unchanged (10 tests, including the duplicate-address-claim assertion that
depends on the now-preserved category-only address dedup).

## Existing 10 scenarios / full suite

`pytest -q`: **476 passed, 1 skipped** (the skip is pre-existing/unrelated).
`ruff check .`: all checks passed.

## Remaining unrelated stochastic extraction gaps (not addressed here)

Per Step 10, these were never a deterministic-pipeline defect and are out
of scope for this task:

- shelter distance (`מקלט במרחק 183 מ'`) never extracted by the one real
  LLM call;
- ~2.5-year permit estimate never extracted;
- phase A/B project-stage claim never extracted;
- current floor (`קומה 3/3`) never extracted;
- the pre-existing `evidence_mapper` linkage design that marks all 5
  nearby claims as "addressed" once a single `nearby_services(park)` call
  returns, even though only 1 of the 5 actually resolves to a queried
  category (documented as a non-blocking limitation in
  `evaluation-summary.md`, unrelated to claim dedup).
