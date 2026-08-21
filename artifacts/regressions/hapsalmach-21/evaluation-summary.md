# Hapalmach 21 — Focused Pre-Tool Triage Evaluation

Single authorized real LLMod planner call. Diagnostic stopped before tool
execution. No MCP, no Nir tools, no external provider, no report_writer.

## Environment

| field | value |
|---|---|
| planner mode | real |
| LLM calls | 1 |
| tools executed | false |
| MCP called | false |
| external provider called | false |
| report_writer called | false |
| produced claims | 10 |
| produced tool_calls | **0** (planner proposed none) |
| retained claims | 8 |
| validated tool_calls | 2 (geo_resolver + nearby_services — **synthesized by plan_validator**) |

## Property Context (as `PropertyDetails` actually contains)

| field | value |
|---|---|
| listing_type | purchase |
| raw_address | `הפלמ"ח 21` |
| asking_price | 4,290,000 |
| rooms | 4 |
| sqm | 112 |

Notes: `sqm=112` (the header figure), not the "101 built + 11 balcony"
split — the diagnostic reports what `PropertyDetails` holds and does not
turn the built/balcony distinction into a requirement. `city_hint` did not
resolve (address string was just `הפלמ"ח 21`, without a city token).

## Relevant Claims Found (nearby + comparative price only)

Two nearby claims were extracted; **no** comparative price claim, **no**
buyer question.

- `חניה רובוטית וחניון משותף עם שער חשמלי` — canonical `nearby_amenities`
  (planner-labelled), text resolves to no frozen category → **supported: no**,
  tool: none.
- `קרוב למרכז קהילתי בית ציון ומוסדות חינוך.` — canonical `nearby_amenities`,
  text resolves to no frozen category → **supported: no**, tool: none.

`ביצרון` / `שדרות ההשכלה` were **not extracted** as claims by the planner
(they remained inside an unextracted sentence) → nearby-related extraction
gap; correctly not mapped.

## Nearby Decisions

```
חניה רובוטית ...            → normalized: nearby_amenities → supported: no → tool: none
מרכז קהילתי בת ציון        → (inside claim-1) not a frozen category → supported: no → tool: none
מוסדות חינוך               → (inside claim-1) no generic alias (school aliases are בית ספר/בתי ספר) → supported: no → tool: none
ביצרון / שדרות ההשכלה      → NOT extracted by planner → no claim → no tool
```

Per-claim classification is semantically safe: community centre was **not**
mapped to school/park; "מוסדות חינוך" was **not** arbitrarily forced to
daycare/school; Bitzaron/Haskala were not mapped.

## Price Decision

- asking price found: **yes** (4,290,000)
- comparative listing claim found: **no**
- comparative buyer question found: **no**
- `price_signal`: **NOT NEEDED** ✅ (asking price + sqm alone did not trigger
  it — the invariant holds; no price regression)

## Final Tool Plan (exact production plan)

```
1. geo_resolver            input: {address: "הפלמ\"ח 21", city_hint: null}
2. nearby_services         depends_on: geo_resolver
                           categories: [supermarket, school, park, pharmacy,
                                        daycare, public_transport, hospital]   ← ALL 7
```

`nearby_services` is dispatched against the **full default category set**
even though both linked nearby claims are classified `supported: no`. This
is a deterministic contradiction between the focused classification
(tool: none) and the production tool plan.

## Missing Relevant Inputs

None affecting a relevant nearby/price check. (`price_signal` is correctly
not planned; `geo_resolver` has an address.)

## Extraction Gaps (nearby/price only)

- `EXTRACTION_GAP`: `ביצרון` and `שדרות ההשכלה` were not extracted as nearby
  place claims in this single real call. Stochastic; not turned into a
  regression by itself.
- Planner labelled in-building "robotic parking" as `nearby_amenities`
  (a proximity service it is not). Stochastic mislabel; noted, not
  regressed on its own.

## Acceptance Matrix

| # | Requirement | Actual behavior | Result | Notes |
|---|---|---|---|---|
| 1 | sufficient location context extracted | address + city area present | PASS | raw_address `הפלמ"ח 21` |
| 2 | asking price extracted if available | 4,290,000 captured | PASS | |
| 3 | extracted nearby claim(s) shown | both nearby claims shown | PASS | |
| 4 | supported nearby mapping semantically justified | none claimed supported | PASS | no false positives |
| 5 | unsupported nearby concepts get no invented categories (per claim) | both marked tool:none | PASS | at the claim level |
| 6 | community centre not falsely supported | `supported: no` | PASS | |
| 7 | education wording handled safely | `מוסדות חינוך` unsupported, not forced | PASS | |
| 8 | Bitzaron/Haskala not falsely mapped | not extracted → not mapped | PASS | extraction gap only |
| 9 | no comparative price claim invented | none invented | PASS | |
| 10 | no price_signal from asking price alone | NOT NEEDED | PASS | invariant holds |
| 11 | geo_resolver only added when required | added for nearby_services dependency | PASS | |
| 12 | nearby_services receives only supported categories | receives **all 7** default categories | **FAIL** | deterministic fan-out |
| 13 | supports_claim_ids link to retained relevant claims | claim-0, claim-1 | PASS | |
| 14 | no fabricated evidence | no tool executed | PASS | all 7 categories are *requested*, not evidence |

## Regression

`REGRESSION_CAPTURED` — deterministic, reproduced without any LLM call.

- Point of loss: `plan_validator._ensure_nearby_services_for_known_claims`
  synthesizes a `nearby_services` call for `nearby_amenities` claims whose
  combined text resolves to **no** supported category (empty `arguments={}`),
  which `build_tool_input` then expands to the full default category set.
- Captured in:
  `tests/agent/test_hapsalmach_nearby_category_regression.py`
  - `test_current_faulty_state_is_reproducible` (green — pins observed state)
  - `test_nearby_services_never_fans_out_to_all_categories`
    (`xfail(strict=True)` — encodes the desired invariant; XPASSes once a
    general fix lands, forcing removal of the marker).

No production fix implemented in this task (per instructions). Next step:
`/fix-from-regression` in a fresh session.

## Final verdict

REGRESSION_CAPTURED
