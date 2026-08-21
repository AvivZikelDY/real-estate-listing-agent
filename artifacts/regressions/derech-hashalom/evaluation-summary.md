# Real listing evaluation: "דרך השלום" (urban-renewal / betterment listing)

Date: 2026-08-09
Diagnostic runner: `scripts/pre_tool_triage.py`
LLM call: **1 authorized real LLMod planner call was made** for this listing
(`diagnostics.llm_calls == 1` in `pre-tool-triage.json`). No retry.

Saved artifacts:
- `artifacts/regressions/derech-hashalom/pre-tool-triage.json` (generated)
- `artifacts/regressions/derech-hashalom/real-planner-run.json` (reconstructed
  raw pre-validation state, from the same one call - no second LLM call made)
- `artifacts/regressions/derech-hashalom/evaluation-summary.md` (this file)

---

## Environment / provenance

| Field | Value |
|---|---|
| planner_mode | real |
| llm_calls | 1 |
| tools_executed | false |
| mcp_called | false |
| external_provider_called | false |
| report_writer_called | false |

`scripts/pre_tool_triage.py` was extended (diagnostic-only) to expose these
five fields explicitly in `diagnostics` so future artifacts self-document
their provenance without relying on `llm_mode` alone. No `--file`/`--question`
CLI change was needed - both were already supported.

---

## Property facts (structured, `PropertyDetails`)

```json
{
  "listing_type": "purchase",
  "raw_address": "דרך השלום\nמקלט במרחק 183 מ'\nדירה, תל חייים, תל אביב יפו",
  "asking_price": 2890000.0,
  "rooms": 2.5,
  "sqm": 60.0
}
```

`PropertyDetails` has no `floor` field (matches the existing Gonen 14
precedent, where floor is only ever a claim, never structural).

## Current vs. future property state

```text
CURRENT PROPERTY (per PropertyDetails)
sqm: 60.0        <- correct, NOT overwritten by the 82 sqm replacement figure
rooms: 2.5       <- correct
asking_price: 2,890,000  <- correct
raw_address: contains "דרך השלום" / "תל חייים" / "תל אביב יפו"  <- correct

FUTURE / REPLACEMENT PROPERTY (only ever a raw claim, never structural)
"82 מ"ר"              -> category size_sqm, capped_max_claims (survived dedup)
"פלוס 12 מ"ר מרפסת"   -> category size_sqm, DROPPED by _dedupe_claims
                          ("deduplicated" - treated as a duplicate of
                          PropertyDetails.sqm, which it is NOT)
"קומה 5"               -> category None, capped_max_claims
"חניה" / "מחסן" / "תפנה לרחוב עמישב" -> category None, capped_max_claims
```

**CURRENT/FUTURE STATE SEPARATION: PARTIAL**

- The structured `PropertyDetails.sqm`/`rooms`/`asking_price` correctly stay
  pinned to the CURRENT apartment's numbers; the 82 sqm/floor-5 replacement
  figures never overwrite them. This half of the check **passes**.
- However, atomization silently discarded the qualifying context: the raw
  LLM claim is the bare `"82 מ"ר"` / `"פלוס 12 מ"ר מרפסת"` / `"קומה 5"` -
  none of them retain the "future replacement apartment" framing the
  original text supplied (`"דירת תמורה:"` became its own separate,
  low-information claim, `id=claim-7`, disconnected in text from the
  numbers that follow it). Any consumer reading only the `"82 מ"ר"` claim
  in isolation cannot tell it is not the current apartment's area.
- The `"12 מ"ר מרפסת"` (balcony) claim - a fact that is **not a duplicate of
  anything** - was actively dropped by `_dedupe_claims` purely because it
  shares canonical category `"size_sqm"` with the already-seen `"82 מ"ר"`
  claim. This is a genuine deterministic-layer defect (see "Regression"
  below), not merely an LLM atomization weakness.

---

## Claims: raw -> deduplicated -> prioritized -> retained -> removed

```text
raw (produced by the one real LLM call): 15
deduplicated:                             14   (1 removed: "פלוס 12 מ"ר מרפסת")
prioritized:                              14   (reordered only)
retained (after MAX_CLAIMS=8 cap):         8
removed total:                             7   (1 dedup + 6 capped)
```

Retained (8), in final priority order:
1. `מיקומים נהדר גובל בגבעותיים,` - nearby_amenities - CHECKABLE_WITH_TOOL
2. `פארק וולפסון,` - nearby_amenities - CHECKABLE_WITH_TOOL
3. `רכבת קלה,` - nearby_amenities - CHECKABLE_WITH_TOOL
4. `ביצרון,` - nearby_amenities - CHECKABLE_WITH_TOOL
5. `יצחק שדה ועוד.` - nearby_amenities - CHECKABLE_WITH_TOOL
6. `התוכנית הופקדה!` - transaction_structure - REQUIRES_CONTRACT_REVIEW
7. `היזם "אורון נדל"ן" - הסכם חתום.` - transaction_structure - REQUIRES_CONTRACT_REVIEW
8. `דירת תמורה:` - (no category) - REQUIRES_SELLER_OR_DOCUMENT

Removed (7):
| text | canonical_category | reason |
|---|---|---|
| `הזדמנויות יוצרות עם החלל לעסקת השבחה...` (marketing/opportunity claim) | none | capped_max_claims |
| `82 מ"ר` | size_sqm | capped_max_claims |
| `פלוס 12 מ"ר מרפסת` | size_sqm | **deduplicated** (incorrectly - see Regression) |
| `חניה` | none | capped_max_claims |
| `מחסן` | none | capped_max_claims |
| `תפנה לרחוב עמישב` | none | capped_max_claims |
| `קומה 5` | none | capped_max_claims |

**Important**: the subjective "opportunity" marketing claim was itself capped
(not artificially prioritized above material facts) - prioritization did
*not* let subjective wording displace a checkable/contract-review claim.
That half of the cap behavior is correct.

**Important, separate finding (extraction-level, not deterministic-pipeline):**
the raw LLM output (all 15 claims - see `real-planner-run.json`) never
produced claims for: the shelter distance (`מקלט במרחק 183 מ'`), the 3
air-directions/quiet/faces-Amishev-street claims, the current floor
(`קומה 3/3`), the ~2.5-year permit estimate, or the phase A/B project-stage
claim. These are missing at the LLM-extraction stage of this one real call,
before `plan_validator` ever runs - not a cap/dedup/prioritization decision.
This is a real, buyer-relevant loss, but it is a stochastic single-call LLM
extraction outcome, not a reproducible deterministic-code defect, so it is
recorded here as an important limitation rather than turned into a
regression (a regression must reproduce a deterministic failure without any
LLM dependency).

---

## Planning / urban-renewal claims

| Claim | Retained? | Category | Verification decision | Notes |
|---|---|---|---|---|
| Developer "אורון נדל״ן" + signed agreement | Yes (merged into one claim) | transaction_structure | REQUIRES_CONTRACT_REVIEW | Correctly NOT marked subjective; correctly routed to contract review, not silently dropped. |
| Plan deposited (`התוכנית הופקדה`) | Yes | transaction_structure | REQUIRES_CONTRACT_REVIEW | Correct - an externally-factual planning-status claim, not treated as verified fact nor as subjective. |
| ~2.5 years to permit | **No** (never extracted by the LLM) | - | - | Lost at extraction, not at validation. See limitation above. |
| Phase A executed / phase B now | **No** (never extracted by the LLM) | - | - | Lost at extraction, not at validation. |
| Replacement apartment (82 sqm, balcony, parking, storage, floor 5, faces Amishev) | Partially (82 sqm + floor 5 claims existed but were capped; balcony claim was wrongly deduplicated away) | size_sqm / none | capped / capped / **deduplicated (bug)** | See Regression. |

No urban-renewal-specific tool was invented; the absence of such a tool did
not itself cause a fabricated call - the tool plan below never proposes
anything beyond the three whitelisted tools.

---

## Shelter claim (`מקלט במרחק 183 מ'`)

```text
claim extracted?        NO (never produced by the one real LLM call)
category?                n/a
verification decision?   n/a
tool selected?            n/a (no shelter category exists in
                          NearbyServiceCategory: supermarket, school, park,
                          pharmacy, daycare, public_transport, hospital)
reason?                   Extraction-level loss for this one real call.
                          Separately, even if extracted, the current frozen
                          `nearby_services` category contract has no
                          "shelter" concept, so it correctly could not have
                          been auto-converted into park/school/daycare -
                          the frozen contract was not violated either way.
```

## Nearby claims

| Claim | Category assigned | Category actually resolved into the tool call | Notes |
|---|---|---|---|
| בורדל בגבעתיים ("borders Givatayim") | nearby_amenities | none (no alias match) | Correctly not forced into any category. |
| פארק וולפסון (Wolfson Park) | nearby_amenities | **park** | Correctly resolved - `_NEARBY_CATEGORY_ALIASES` matches "פארק". |
| רכבת קלה (light rail) | nearby_amenities | none (no alias match; not force-mapped to `public_transport`) | Correct per contract - the alias table has no Hebrew "light rail" keyword, only "תחבורה ציבורית"/"transport", so it is (correctly) not force-mapped. |
| ביצרון (Bitzaron, neighborhood name) | nearby_amenities | none | Correctly not forced into any category. |
| יצחק שדה (Yitzhak Sadeh, street/area name) | nearby_amenities | none | Correctly not forced into any category. |

Final `nearby_services` call: `categories=["park"]` only - matches the
one alias hit ("פארק וולפסון"). **Non-blocking observation**: all 5 nearby
claims are still listed in `supports_claim_ids` for that single call, even
though only the "park" category is actually queried - so evidence linking
will mark all 5 as "addressed" once the call returns, even though 4 of them
(light rail/Bitzaron/Givatayim/Yitzhak Sadeh) receive no actual evidence.
This is a pre-existing `evidence_mapper`/tool-plan linkage design (claims
are linked by canonical category, not by which categories were actually
resolved into the call) - out of scope for this listing-specific
evaluation, flagged as an important non-blocking limitation.

---

## Price

`price_signal` does **not** appear anywhere in the final tool plan (only
`geo_resolver` + `nearby_services`). Buyer goal was `fast_claim_triage` (no
buyer question was supplied), and no `price_comparison`-category claim was
produced (the listing never asserted "below market"/"bargain"). Confirms
`price_signal` is correctly absent - no regression here.

---

## Final tool plan (validated, not executed)

```json
[
  {
    "tool_name": "geo_resolver",
    "supports_claim_ids": ["claim-0","claim-1","claim-2","claim-3","claim-4"],
    "dependencies": [],
    "planner_arguments": {"address": "<raw multi-line address>"}
  },
  {
    "tool_name": "nearby_services",
    "supports_claim_ids": ["claim-0","claim-1","claim-2","claim-3","claim-4"],
    "dependencies": ["tool-call-1"],
    "planner_arguments": {"categories": ["park"]}
  }
]
```

No unsupported tool, no invented category, no `price_signal` without
comparison intent, `geo_resolver` present only because a location-dependent
tool needs it.

---

## Verification still required (grouped)

```text
CHECKABLE_WITH_TOOL:
  - מיקומים נהדר גובל בגבעותיים,
  - פארק וולפסון,
  - רכבת קלה,
  - ביצרון,
  - יצחק שדה ועוד.
REQUIRES_SELLER_OR_DOCUMENT:
  - דירת תמורה:
REQUIRES_CONTRACT_REVIEW:
  - התוכנית הופקדה!
  - היזם "אורון נדל"ן" - הסכם חתום.
SUBJECTIVE: (none retained)
OUT_OF_SCOPE: (none)
MISSING_INFORMATION:
  - additional_claims_truncated
  - additional_claims_truncated_detail: produced=15 deduplicated=14 retained=8 removed=6 removed_categories=['size_sqm', 'uncategorized']
```

## Follow-up topics (executable diagnostic's actual output, verbatim)

```text
- clarify in contract review: התוכנית הופקדה!
- clarify in contract review: היזם "אורון נדל"ן" - הסכם חתום.
- confirm with seller or request document: דירת תמורה:
```

These correctly preserve the signed-agreement and deposited-plan topics.
They do **not** preserve the permit-timeline, phase A/B, replacement-unit
specification, balcony/parking/storage, or future-floor topics - those
claims were removed by the cap (or, for permit/phase, never produced by the
LLM at all), so no follow-up template ever ran for them. This is a direct,
visible consequence of both the extraction gap and the (buggy) dedup/cap
outcome described above.

---

## Acceptance matrix

| # | Requirement | Actual behavior | Result | Notes |
|---|---|---|---|---|
| 1 | Current address preserved | `raw_address` includes "דרך השלום"/"תל חייים"/"תל אביב יפו" | PASS | |
| 2 | Price preserved | `asking_price = 2,890,000` | PASS | |
| 3 | 2.5 rooms preserved | `rooms = 2.5` | PASS | |
| 4 | Current 60 sqm preserved | `sqm = 60.0`, never overwritten by 82 | PASS | |
| 5 | Current floor 3/3 preserved | Never extracted as a claim at all (no `PropertyDetails.floor` field, and no floor-3/3 claim produced) | FAIL | Extraction-level loss, not a deterministic-pipeline bug. |
| 6 | Future 82 sqm not confused with current area | `PropertyDetails.sqm` stays 60; 82 sqm only ever a separate (capped) claim | PASS | Structured field never corrupted. |
| 7 | Future floor 5 not confused with current floor | `PropertyDetails` has no floor field to corrupt; floor-5 only ever a separate (capped) claim | PASS | |
| 8 | Urban-renewal agreement retained | `"היזם ... - הסכם חתום."` retained, REQUIRES_CONTRACT_REVIEW | PASS | |
| 9 | Deposited-plan claim retained | `"התוכנית הופקדה!"` retained, REQUIRES_CONTRACT_REVIEW | PASS | |
| 10 | Permit timeline retains uncertainty | Never extracted at all | FAIL | Extraction-level loss - not present to mis-classify as fact, but also not preserved as an estimate either. |
| 11 | Future replacement-apartment semantics retained | 82 sqm/floor-5 claims exist but lose the "future/replacement" qualifier once atomized, and the balcony fact was wrongly deduplicated away | PARTIAL | See Regression. |
| 12 | Shelter claim not fabricated through unsupported tool | Never extracted; no shelter category exists, none invented | PASS (by omission) | No fabrication occurred, but the claim itself is also missing. |
| 13 | Wolfson Park handled correctly | Resolved to `categories=["park"]` | PASS | |
| 14 | Light rail not forced into unsupported nearby category | No category resolved for "רכבת קלה" | PASS | |
| 15 | Subjective marketing deprioritized | The one subjective/marketing-flavored claim was capped, not retained ahead of contract-review/tool-checkable claims | PASS | |
| 16 | price_signal absent | Confirmed absent from tool plan | PASS | |
| 17 | Important claims survive MAX_CLAIMS | Signed agreement + deposited plan survive; balcony/parking/storage/floor-5/marketing do not | PARTIAL | Cap correctness is fine; the balcony loss is actually a dedup bug, not a cap-priority bug. |
| 18 | Prepared tool inputs grounded | `geo_resolver` input uses the real raw address; `nearby_services` input references `tool-call-1`'s unresolved lat/lon symbolically (never fabricated coordinates) | PASS | |
| 19 | Transaction/planning information does not disappear because no tool exists | Agreement + deposited-plan claims retained and routed to `REQUIRES_CONTRACT_REVIEW`, not dropped for lack of a tool | PASS | |
| 20 | No fabricated evidence | No tool was executed; all reported information is either a listing fact or an explicitly-unresolved dependency | PASS | |

---

## Problems found

**Blocking:**
- `_dedupe_claims` drops a second, textually-distinct `"size_sqm"`-category
  claim (the future replacement apartment's own 12 sqm balcony) purely
  because it shares a canonical category with an earlier `"size_sqm"`
  claim, even though it is not a duplicate of `PropertyDetails.sqm` or of
  the other claim's text at all. This is a general defect that will recur
  on any listing describing a second entity (a replacement/compensation
  unit, a comparable unit, etc.) with its own size/price/room facts.

**Important, non-blocking:**
- The real LLM extraction for this one call never produced claims for the
  shelter distance, air-directions/quiet/orientation claims, the current
  floor (3/3), the permit-timeline estimate, or the phase A/B project
  stage - a real information loss, but at the stochastic LLM-extraction
  stage, not the deterministic pipeline this task's regression mechanism
  targets.
- `supports_claim_ids` links all 5 `nearby_amenities` claims to the single
  `nearby_services` call even though only `"park"` was actually resolved
  into its arguments - a pre-existing `evidence_mapper` granularity
  limitation, not specific to this listing.
- Atomizing `"דירת תמורה: 82 מ"ר"` into a bare `"דירת תמורה:"` claim and a
  separate, unqualified `"82 מ"ר"` claim loses the textual link between
  the number and "this is the future replacement apartment" - independent
  of the dedup bug above.

**Accepted limitation:**
- No shelter/light-rail nearby-service category exists in the frozen tool
  contract; this task correctly does not invent one.

---

## Final verdict

```text
REGRESSION_NEEDED
```

Reason: the future-replacement-apartment balcony claim is silently and
incorrectly deduplicated as if it were a redundant restatement of the
*current* apartment's structured size, which is a general, deterministic
plan-validator defect (not specific to this listing's wording) - it will
recur on any multi-entity listing. Per Phase 18, a deterministic regression
was captured and the production fix was **not** implemented in this task.

```text
REGRESSION_CAPTURED
```

- Test file:
  `tests/agent/test_multi_entity_claim_dedup_regression.py::test_second_distinct_size_sqm_claim_is_not_dropped_as_a_duplicate`
- Failing semantic invariant: two claims sharing a "listing-fact" canonical
  category (`size_sqm` here; the same defect applies to `address`/`price`/
  `rooms`) but with distinct, non-duplicate text must both survive
  `validate_plan` - category alone must not imply redundancy with
  `PropertyDetails`.
- First point of failure: `src.listing_agent.agent.plan_validator.validator._dedupe_claims`
  (via its `_REDUNDANT_WITH_PROPERTY_DETAILS` rule), called from
  `validate_plan`.
- The test uses generic English placeholder text (no real address, no
  claim-index hack, no listing-text-specific production logic) and is
  marked `@pytest.mark.xfail(strict=True, ...)` so it fails for the
  diagnosed reason today, keeps the full pytest suite green
  (470 passed, 1 skipped, 1 xfailed), and will flip to an unexpected pass
  (breaking the build, by design) the moment a real fix lands - locking
  the fix in per the project's regression-fix-then-un-mark workflow.

No production fix was implemented in this task.
