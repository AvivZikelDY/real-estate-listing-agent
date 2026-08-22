---

description: Fix a captured real-listing regression without overfitting
agent: agent
------------

# Fix a captured listing-agent regression

Continue the existing Python 3.12 FastAPI real-estate listing-agent project.

A real-listing failure has already been diagnosed and converted into a
deterministic regression test.

Your task is to implement the smallest general production fix that makes the
new regression pass without weakening existing behavior.

Do not diagnose the failure from scratch unless the supplied diagnosis is
inconsistent with the current repository state.

Do not implement MCP.
Do not implement live tool logic.
Do not add another production LLM call.
Do not change public API contracts.
Do not modify expected fixtures merely to match unstable real-LLM wording.
Do not special-case one address, listing, scenario ID, or exact claim text.

---

# Step 1 — locate the captured regression

Before editing, inspect:

* the newest failure diagnosis under `artifacts/`;
* the newly added failing regression;
* `docs/evaluation.md`;
* `docs/contracts.md`;
* `.github/copilot-instructions.md`;
* the relevant production modules.

If the current failure is the Gonen 14 claim-retention case, also read:

```text
artifacts/claim-loss-diagnosis/root-cause-report.md
artifacts/claim-loss-diagnosis/claims-before-limit.json
artifacts/claim-loss-diagnosis/claims-after-limit.json
artifacts/claim-loss-diagnosis/validation-decisions.json
```

Identify:

1. the first production stage where behavior becomes incorrect;
2. the semantic invariant expressed by the regression;
3. the smallest shared production boundary where the issue should be fixed.

Present a short implementation plan before editing.

---

# Step 2 — preserve the architecture

The production flow must remain conceptually:

```text
listing + optional buyer question
→ triage_planner
→ deterministic normalization
→ deterministic claim selection / validation
→ validated tool plan
→ ToolExecutor
→ Evidence Collector
→ Evidence Bundle
→ report_writer
```

Do not:

* create another planner;
* create another ranking LLM call;
* move business rules into diagnostic scripts;
* make the evidence layer re-interpret the raw listing;
* let tools decide which claims should be checked.

Reuse existing production models and helpers.

---

# Step 3 — general fix rules

The fix must express a general semantic rule.

Examples of acceptable general fixes:

* canonical category normalization;
* buyer-goal-based filtering;
* deterministic argument normalization;
* claim deduplication;
* claim prioritization;
* transaction-structure preservation;
* dependency ordering;
* tool-call synthesis for a known retained claim;
* evidence linkage through `supports_claim_ids`;
* typed claim provenance;
* missing-information propagation.

Examples of unacceptable fixes:

```text
if address == "גונן 14": ...
```

```text
if claim_id == "claim-10": ...
```

```text
if "מתחם גני ילדים מעבר לכביש" in listing: ...
```

```text
MAX_CLAIMS = 30
```

as the only solution to a prioritization bug.

---

# Step 4 — claim-retention policy

When the failure involves too many claims, use a deterministic order:

```text
normalize
→ classify
→ deduplicate
→ prioritize
→ cap
```

Do not cap by raw LLM list position alone.

Use the existing models to prioritize conceptually:

1. claims directly relevant to the buyer question;
2. claims checkable by an available tool;
3. material transaction-structure or future-commitment claims;
4. factual claims requiring seller/document/physical verification;
5. unique listing facts not already represented by structured PropertyDetails;
6. subjective marketing claims.

Within equal-priority groups, preserve deterministic stable order.

Listing facts already safely represented by typed fields such as:

* address;
* asking price;
* rooms;
* area;

must not consume multiple high-value claim slots through redundant claim
entries.

Do not incorrectly deduplicate distinct facts merely because they share one
broad category.

For example:

```text
יש ממ"ד
```

and:

```text
פרקט יותקן לאחר המסירה
```

are distinct commitments even if their legacy category representation is
similar.

---

# Step 5 — Pre-Tool Triage requirements

After the fix, the production plan must retain enough information to build the
project's Pre-Tool Triage Output.

For every important listing assertion, the system should be able to determine
one of:

```text
LISTING_FACT
CHECKABLE_WITH_TOOL
REQUIRES_SELLER_OR_DOCUMENT
REQUIRES_CONTRACT_REVIEW
SUBJECTIVE
OUT_OF_SCOPE
MISSING_INFORMATION
```

Reuse existing enums and fields where possible instead of introducing
near-duplicates.

Important information must not disappear merely because no current tool can
verify it.

For example, transaction terms such as:

```text
השכרה לשנה / שנתיים ולאחר מכן רכישה על ידי השוכר
```

may have no current tool but are materially relevant and should survive into
the buyer-facing evidence/report path.

---

# Step 6 — tool-selection invariants

Keep these rules:

## geo_resolver

Run only when a retained tool-checkable claim requires resolved location.

## nearby_services

Run when a retained nearby-amenities claim or relevant buyer question requires
one or more supported categories.

Nearby categories must be normalized deterministically.

Examples:

```text
גן / גן ילדים / kindergarten
→ daycare

בית ספר / school
→ school

פארק / park
→ park
```

## price_signal

Do not run merely because asking price and area are present.

Run only for a retained comparative price claim or a buyer price-validation
question.

Evidence must support the comparative assertion, not merely the listing's
asking-price fact.

---

# Step 7 — preserve unsupported but important information

Claims outside the current three-tool contract should not automatically become
meaningless `unsupported_claim_type` entries.

Where appropriate, distinguish:

```text
REQUIRES_SELLER_OR_DOCUMENT
```

for facts such as:

* safe room;
* floor;
* kitchen specification;
* bathroom upgrade;
* promised future installation;
* delivery status.

Use:

```text
REQUIRES_CONTRACT_REVIEW
```

or the closest existing typed concept for:

* rent-then-purchase;
* purchase option;
* future price terms;
* rental-credit terms;
* optional versus binding purchase obligations.

Do not invent facts not present in the listing.

---

# Step 8 — regression-first verification

The newly captured regression must fail before the production fix and pass
after the fix.

Then run all previous scenarios.

At minimum:

```bash
ruff check .
pytest tests/evaluation
pytest tests/scripts
pytest
```

Also run the focused unit-test files related to the changed production
modules.

Do not remove an old assertion simply because the new implementation breaks
it. Determine whether the old assertion or the new behavior is semantically
correct.

---

# Step 9 — real-listing verification

When valid LLMod credentials exist and the current task explicitly authorizes
paid verification, run the affected listing once through the real planner.

Do not repeatedly call the real LLM until it produces a desired output.

The deterministic layer should absorb supported model variance.

For the real run print:

1. extracted property facts;
2. raw claim count;
3. normalized/deduplicated claim count;
4. retained claims;
5. removed claims and reasons;
6. buyer goal;
7. final validated tool plan;
8. `supports_claim_ids`;
9. prepared typed tool inputs;
10. claims requiring seller/document verification;
11. transaction-structure information;
12. remaining missing information.

If Evidence mode is used, use mock tools only.

Never call:

* MCP;
* an external data provider;
* `report_writer`;

unless the current task explicitly requests those stages.

---

# Step 10 — produce Pre-Tool Triage Output

For the corrected scenario, print a final section:

```text
PRE-TOOL TRIAGE OUTPUT
```

using the contract documented for this project.

It must include:

* property facts;
* important atomic claims;
* verification decision for each claim;
* planned tools;
* exact prepared tool inputs;
* items requiring seller/document verification;
* items requiring contractual clarification;
* unsupported aspects;
* missing information;
* suggested buyer follow-up questions.

This output is the acceptance surface for testing the pipeline without
invoking the live tool implementations.

---

# Step 11 — save artifacts

Save sanitized results under a scenario-specific directory, for example:

```text
artifacts/regressions/<scenario-id>/
```

Include:

```text
diagnosis-summary.md
before.txt
after.txt
retained-claims.json
removed-claims.json
pre-tool-triage.json
final-summary.md
```

Do not store:

* API keys;
* authorization headers;
* environment values;
* hidden chain of thought;
* raw provider credentials.

---

# Required completion report

Return:

1. Regression being fixed.
2. First point of failure.
3. Files modified.
4. General production rule implemented.
5. Why the fix is not overfit to this listing.
6. Before/after claim selection.
7. Before/after tool plan.
8. Pre-Tool Triage Output.
9. New regression result.
10. All previous evaluation results.
11. Full pytest result.
12. Ruff result.
13. Real-LLM verification result, if authorized and performed.
14. Remaining limitations.
15. Explicit confirmation:

    * no MCP called;
    * no external provider called;
    * no extra production LLM call added.
16. Final verdict:

```text
REGRESSION_FIXED
```

or:

```text
REGRESSION_NOT_FIXED
```

Stop after completing this fix.

Do not proceed to MCP integration automatically.
