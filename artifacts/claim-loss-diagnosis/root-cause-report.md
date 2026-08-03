# Root-cause diagnosis: claim loss on the Gonen 14 listing

Date: 2026-08-02
Diagnostic runner: `scripts/diagnose_claim_loss.py`
LLM call: **1 authorized real LLMod planner call was made** for this diagnosis.

---

## 1. Executive finding

**Primary classification: claim-cap prioritization failure**, with a
**contributing prompt weakness** (compound-claim extraction).

The real LLM correctly produced **12 claims** including a `nearby_amenities`
claim covering `מתחם גני ילדים מעבר לכביש` (kindergarten complex) and
`המון חנייה ברחוב` (abundant street parking), a rent-to-purchase compound
claim, and the asking-price claim. `plan_validator._cap_claims` then dropped
the last 4 claims (indices 8–11 in raw order) to hit `MAX_CLAIMS=8`. The
discarded claims were, in order: parquet-delivery commitment, the entire
`nearby_amenities` claim, the rent-to-purchase claim, **and the asking-price
claim itself**. Because the `nearby_amenities` claim was dropped by the cap
*before* `_ensure_nearby_services_for_known_claims` runs, the Phase 19
deterministic recovery could not fire; no `nearby_services` call was ever
synthesized, no `geo_resolver` dependency was auto-inserted, and the buyer
would never be told the listing already provided a checkable kindergarten
claim.

The LLM did **not** fail to extract the information. The prompt did **not**
force compound sentences to be split into atomic claims — that is a
contributing prompt weakness — but even without atomization, the specific
kindergarten and rent-to-purchase claims were **present in the LLM output**.
They disappeared solely because the cap discards in raw list order without
any prioritization.

---

## 2. Pipeline loss map

Legend: `Y` = present, `N` = absent, `n/a` = stage does not apply.

| Expected information | in input | in raw LLM | after parsing | after normalization | before cap | after cap | linked to a tool |
|---|---|---|---|---|---|---|---|
| Kindergarten complex (`מתחם גני ילדים`) | Y | Y (inside compound claim c10) | Y | Y | Y | **N (dropped)** | N |
| Abundant street parking (`המון חנייה ברחוב`) | Y | Y (inside compound claim c10) | Y | Y | Y | **N (dropped)** | N |
| Safe room (`ממ״ד`, c2) | Y | Y | Y | Y (canonical=`condition`, out-of-scope) | Y | Y | N (out-of-scope for the three-tool contract) |
| Floor 5/6 (c5) | Y | Y (category=null) | Y | Y (canonical=null) | Y | Y | N (no tool covers floor) |
| Delivery state (c7) | Y | Y (`דירה חדשה למסירה…`) | Y | Y | Y | Y | N (no tool covers delivery status) |
| Parquet-after-delivery commitment (c9) | Y | Y | Y | Y | Y | **N (dropped)** | N |
| Rental period (c11) | Y | Y (compound with purchase-option in one claim) | Y | Y (category=null) | Y | **N (dropped)** | N |
| Later purchase option (c11) | Y | Y (compound as above) | Y | Y | Y | **N (dropped)** | N |
| Asking price (c12) | Y | Y (`category=price`) | Y | Y | Y | **N (dropped)** | N |
| Address (c1, c3) | Y | Y (2 separate address claims) | Y | Y | Y | Y | N (no tool proposed — see §5) |

---

## 3. First point of loss (per missing claim)

| Missing claim | First point of loss | Reason |
|---|---|---|
| Kindergarten complex | **cap (`_cap_claims`)** | Present in raw LLM as c10 (index 9). Cap keeps indices 0–7, drops index 9. |
| Street parking | **cap (`_cap_claims`)** | Same c10 as above. |
| Parquet-after-delivery | **cap (`_cap_claims`)** | c9 (index 8). |
| Rental period / rent-to-purchase | **cap (`_cap_claims`)** | c11 (index 10). |
| Asking-price claim | **cap (`_cap_claims`)** | c12 (index 11). Note: the value itself is still in `property_details.asking_price` — but the *claim* representing it disappears from the report-writer's claims list. |

The `nearby_services` synthesis and `price_comparison` derivation both
depend on a surviving claim of the right canonical category. Because the
cap removes both candidate claims (c10 `nearby_amenities`; c12 `price` —
which could have paired with a `price_comparison` derivation only if the
buyer had asked a comparative question, which they didn't), Phase 19
recovery has nothing to work with. **Phase 19 recovery is therefore not
itself faulty; it is starved of input by the cap.**

For claims that never appear in the raw LLM output (e.g. an "Anis Kitchens
supplier" claim as its own atomic claim), the first point of loss is
**LLM extraction** — the LLM folded kitchen, bathroom upgrades, and
unspecified upgrades into a single subjective `condition` claim (c8). This
is compound-claim extraction, not classification failure; the prompt does
not instruct the model to atomize.

---

## 4. Claim-cap analysis

| Metric | Value |
|---|---|
| Total claims produced by LLM | **12** |
| Retained after cap | **8** |
| Dropped by cap | **4** |
| Duplicate address claims consuming cap slots | **2** (c1 `גונן 14`, c3 `דירה, תל חייים, תל אביב יפו` — both `category=address`) |
| Subjective claims retained | c8 (kitchen/bathroom marketing prose) |
| Externally-checkable claim (nearby_amenities) dropped | **yes (c10)** |
| Price-value claim dropped | **yes (c12)** — although the price value survives in `property_details` |
| Prioritization applied | **none** — cap keeps the first 8 in raw list order |
| Cap position vs. Phase 19 synthesis | Cap fires **first**, so the dropped `nearby_amenities` claim is invisible to `_ensure_nearby_services_for_known_claims` |
| Was the drop recorded? | Yes — `missing_information` contains `additional_claims_truncated`, but the *content* of what was dropped is not preserved |

Concrete drops (post-cap, in original raw-order):
1. c9 `פרקט בחדרים שיורכב לאחר המסירה.` (factual/condition — checkable via document, but out of the three-tool contract)
2. **c10 `נוף ירוק ופתוח, מתחם גני ילדים מעובר לכביש והמון חנייה ברחוב.` (factual/nearby_amenities — tool-checkable)**
3. c11 `יש אופציה למכירה עם המסירה וגם פתוחים לאפשרות להשכרה לשנה או שנתיים ולאחר מכן רכישה על ידי השוכר.` (factual, no category — transaction structure)
4. **c12 `4,350,000 ₪` (factual/price — the asking price claim itself)**

---

## 5. Responsibility by layer

- **Input construction**: **fine.** The exact listing text (all lines, both
  Hebrew and punctuation preserved verbatim in raw response) was sent. The
  LLM did make one typo (`תל חייים` instead of `תל חיים`) and one
  spelling variant (`מעובר`/`חנייה`) that copy the user's Hebrew almost
  but not exactly verbatim — this is LLM output variance, not input loss.
- **Prompt** (`llm/triage_planner/prompts.py`): **contributing weakness.**
  Missing instructions:
  - Does not instruct the model to split compound sentences into atomic
    claims → the model produced compound c8 (kitchen+bathrooms+upgrades) and
    compound c10 (view+kindergarten+parking) and compound c11
    (rental+purchase option), consuming one cap slot for information that
    could have been three separate claims each.
  - Does not instruct the model to avoid duplicate address claims → c1
    and c3 are both `category=address`, both consume cap slots.
  - Does not instruct the model to respect a maximum number of claims →
    the model produced 12 with `MAX_CLAIMS=8`.
  - Does not instruct the model to prioritize verifiable claims over
    subjective ones → subjective c8 survives while verifiable c10
    (nearby_amenities) is dropped.
  Present instructions cover: `listing_type`, extraction/`null` for
  missing facts, `factual`/`subjective` typing, canonical categories,
  `buyer_goal` classification, tool-selection rules, no legal/appraisal
  language.
- **LLM output variance**: **partial.** The model correctly detected
  purchase, extracted all four listing facts, correctly classified 11 of 12
  claims into meaningful categories, correctly flagged c5 (`קומה 5/6`) and
  c11 (transaction structure) as having no matching canonical category
  rather than forcing one. It did merge compound sentences.
- **Deterministic planner** (`plan_validator`): **primary failure.**
  `_cap_claims` (`src/listing_agent/agent/plan_validator/validator.py:254-269`)
  drops in raw list order. It runs at line 318 of `validate_plan`, before
  `_ensure_nearby_services_for_known_claims` at line 323, so any
  externally-checkable claim past index 7 is invisible to Phase 19 recovery.
- **Validator (other rules)**: **fine.** `_drop_forbidden_price_signal`,
  `_ensure_geo_resolver_dependency`, `_ensure_price_comparison_claim_linkage`
  behaved exactly as designed. `nearby_services` synthesis is applicable in
  principle (`nearby_services_synthesis_applicable: true` in
  `validation-decisions.json`) — but only because the diagnostic computes
  this against pre-cap claims; the production validator computes it against
  post-cap claims and correctly returns false.
- **Tool-boundary layer**: **not invoked.** With zero tool calls proposed,
  `build_tool_input` / `LocalToolExecutor` / MCP had nothing to do. Not
  culpable.
- **Evidence layer**: **downstream consequence, not culpable.** With an
  empty tool plan there is no evidence to map. `EvidenceBundle` is empty
  except for `missing_information=["additional_claims_truncated"]`. The
  future report-writer would receive: purchase / address / rooms / sqm /
  price (from `property_details`), 8 claims (rooms, address×2, size_sqm,
  safe-room, floor, delivery-marketing, kitchen-marketing), no tool
  evidence, `additional_claims_truncated` as its only limitation marker.

---

## 6. Severity

| Issue | Severity |
|---|---|
| `_cap_claims` uses raw list order with no prioritization → drops a verifiable `nearby_amenities` claim while retaining subjective marketing claim | **Important but non-blocking before Nir integration** — Nir's real tools do not depend on which claims survive; but the agent's *usefulness on real listings* is materially reduced. |
| `_cap_claims` can drop the asking-price claim while `property_details.asking_price` still holds the number | **Important but non-blocking** — the value is not lost, only the claim entry linking that number to a tool. `report_writer` still has the price. |
| Cap fires before Phase 19 synthesis, so Phase 19 recovery can never help a claim dropped by the cap | **Important but non-blocking** — architectural ordering; not a defect the acceptance suite catches because the acceptance fixtures always fit under 8 claims. |
| Prompt does not require atomic claim decomposition | **Important but non-blocking** — same reasoning; contributes to overshooting `MAX_CLAIMS` on real listings. |
| `fast_claim_triage` goal + listing-side `nearby_amenities` claim → no tool call proposed by the LLM itself | **Accepted limitation** — Phase 19 synthesis is designed to catch exactly this case, and would work if the claim survived. |
| Duplicate address claims (c1, c3) consume 2 of 8 cap slots | **Important but non-blocking** — one address is enough to feed `geo_resolver`; the second slot is a pure waste. |
| Buyer would not be told about the hybrid rent-then-buy transaction structure | **Important but non-blocking** — factual claim c11 is dropped; there is no other channel to surface it. |
| LLM Hebrew typos (`חייים`, `מעובר`, `חנייה`) | **Display-only** — does not affect classification, category, or downstream logic. |
| Missing information does not name *which* claims were truncated | **Display-only** — `additional_claims_truncated` is a boolean marker only. |

None of the above is a **blocking** issue for handing the mock-side of the
pipeline to Nir: the tool contracts, executor, MCP boundary, and evidence
layer are unchanged, and the acceptance suite (which uses hand-authored
plans that always fit under 8 claims) is unaffected.

---

## 7. Recommended fixes (not implemented in this task)

Ordered by expected benefit vs. regression risk.

### R1 — Prioritize verifiable / tool-linkable claims before applying the cap
- **Target**: `src/listing_agent/agent/plan_validator/validator.py:254-269` (`_cap_claims`).
- **Smallest change**: sort claims by a stable, deterministic priority
  before applying the cap: (1) claims whose canonical category is in
  `TOOLS_BY_VERIFIABLE_CATEGORY` (i.e. `nearby_amenities`,
  `price_comparison`), (2) claims explicitly linked via
  `ToolCall.supports_claim_ids`, (3) `factual` listing-fact claims with a
  canonical category (`address`, `price`, `rooms`, `size_sqm`), (4) other
  factual claims, (5) subjective claims. Deduplicate by
  `(canonical_category, normalized_text)` so a second address claim does
  not consume a slot.
- **Expected benefit**: this specific listing would retain c10 (nearby)
  and c12 (price) inside the 8-cap; Phase 19 nearby synthesis would then
  fire and the buyer would learn about the kindergarten claim.
- **Regression risk**: **low if implemented as a stable sort** that only
  swaps the *dropped* tail. Existing scenarios all fit under 8 claims,
  so their retention set is unchanged.
- **Required tests**: new `test_cap_prioritizes_verifiable_claims`,
  `test_cap_deduplicates_repeated_address_claims`,
  `test_cap_never_drops_a_claim_below_max_when_a_dropped_claim_has_lower_priority`,
  plus a fresh table-driven scenario mirroring this Gonen 14 listing.

### R2 — Move `_ensure_nearby_services_for_known_claims` before `_cap_claims`
- **Target**: `validator.py:318-325` — swap the order.
- **Smallest change**: apply Phase 19 synthesis on the raw claim list (all
  12), then cap. The cap already keeps synthesized `ToolCall.supports_claim_ids`
  as a hint the prioritization in R1 can use to keep the linked claim.
- **Expected benefit**: even without R1, this alone would insert a
  `nearby_services` call when the LLM produced a `nearby_amenities` claim,
  regardless of whether the claim itself survives.
- **Regression risk**: **medium** — a synthesized call whose supporting
  claim is dropped by the cap creates a `supports_claim_ids` that
  references a claim that no longer exists in the plan. `find_relevant_claim_ids`
  handles this today (it filters against known ids in tier 1), but the
  behavior needs an explicit test.
- **Required tests**: new
  `test_nearby_synthesis_survives_when_supporting_claim_gets_capped` and
  a check that the four-key `/api/execute` shape is unchanged.

### R3 — Add explicit atomic-claim guidance to the planner prompt
- **Target**: `src/listing_agent/llm/triage_planner/prompts.py`.
- **Smallest change**: add three bullets: "Split compound sentences into
  atomic claims (one fact per claim). If a sentence mentions kindergarten
  and parking, produce two claims. If a sentence mixes a factual and a
  subjective statement, split them." + "Avoid duplicate address claims —
  produce at most one address claim." + "You must produce at most 8
  claims. Prefer factual/verifiable claims over marketing prose."
- **Expected benefit**: reduces cap pressure at the source; increases the
  chance the LLM itself hits `MAX_CLAIMS` cleanly.
- **Regression risk**: **medium** — a prompt change affects real-LLM
  behavior across all scenarios; existing scenario 9 still lives under
  the cap so should be unaffected. Requires a fresh real-LLM spot check on
  scenarios 1–10.
- **Required tests**: none at the prompt layer (real-LLM behavior is
  non-deterministic by design); a real-LLM verification pass is the check.

### R4 — Preserve *what* was truncated
- **Target**: `_cap_claims` in `validator.py`.
- **Smallest change**: instead of only appending `additional_claims_truncated`
  to `missing_information`, append `additional_claims_truncated: <n>
  claims (categories: <list>)`. Structurally could be a new
  `plan.truncation_info` field, but keeping the human-readable string keeps
  the `TriagePlan` schema stable.
- **Expected benefit**: downstream report-writer can tell the buyer
  something *was* trimmed and roughly what topic (nearby_amenities,
  transaction structure).
- **Regression risk**: **very low** — string-only change inside an existing
  field.
- **Required tests**: new `test_cap_records_truncated_topics_in_missing_information`.

### R5 — Never cap the asking-price claim while `property_details.asking_price` is set
- **Target**: `_cap_claims` in `validator.py`, or the R1 priority rule.
- **Smallest change**: a claim whose canonical category equals `price` and
  whose text mirrors the extracted `property_details.asking_price` is
  redundant with the property fact; deterministically deduplicate it
  *before* applying the cap.
- **Expected benefit**: frees a cap slot; the property fact already
  carries the number to any downstream module.
- **Regression risk**: **low** — deterministic deduplication only.
- **Required tests**: new
  `test_cap_deduplicates_price_claim_when_property_details_asking_price_is_set`.

### Consciously not recommended right now

- **Raising `MAX_CLAIMS` beyond 8** — it treats the symptom rather than the
  underlying prioritization gap; a large enough listing could still
  overshoot any fixed cap.
- **Adding another LLM call for atomization** — violates the
  two-normal-LLM-call architectural rule; not allowed by scope.
- **Splitting claims deterministically after the fact** — brittle Hebrew
  NLP heuristics; the prompt fix (R3) is the correct place.
- **Adding a new tool for building attributes** — outside the scope of the
  current three-tool contract and Nir's ownership.

---

## 8. Final diagnosis verdict

```
FAILURE_LOCATED
```

The first stage at which the kindergarten, street-parking, parquet,
rent-to-purchase, and asking-price claims are lost is the same:
`plan_validator._cap_claims`. The claims are all confirmed present in the
raw LLMod response saved at
`artifacts/claim-loss-diagnosis/raw-llm-response.json` (12 claims) and the
parsed `TriagePlan` at `artifacts/claim-loss-diagnosis/parsed-triage-plan.json`
(also 12 claims — parsing dropped nothing). The cap removes indices 8–11
in raw order, which for this specific listing happens to be exactly the
subset the buyer would find most useful.
