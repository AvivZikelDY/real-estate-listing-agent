# Final Evaluation Results

This document summarizes the acceptance evaluation performed against the
deployed agent (and, where noted, against deterministic offline regression
tests) before submission. It reports only runs/tests that were actually
performed — it does not invent new live results.

Two kinds of evidence are used:

- **Live** — a real `POST /api/execute` call against the deployed Vercel
  instance (`LLM_MODE=llmod`, `TOOLS_MODE=live`, `TOOL_BACKEND=mock`).
- **Deterministic** — an offline `pytest` regression test
  (`LLM_MODE=fake`/`TOOLS_MODE=mock`), exercising the real
  `agent_orchestrator`/`plan_validator`/`evidence_collector`/
  `report_writer`-wiring code with only the LLM client and tool registry
  swapped for fakes/mocks.

Exact place counts/distances observed during a specific live run are not
repeated here as guarantees — live data sources may change between runs.

## Scenario results

| # | Scenario | What it evaluates | Observed behavior | Tool behavior | Groundedness | Latency (live) | Result |
|---|---|---|---|---|---|---|---|
| 1 | A1 — Listing A, no buyer question (default triage) | Autonomous default-triage selectivity | Planner recognized "קרובה לפארק"/"מחיר מציאה" claims; did not invent checks for elevator, A/C, solar heater, or "מתאימה למשקיעים" | Selective, explainable tool choice (not fixed to one required set) | Grounded buyer-facing report, no fabricated facts | ~live, within budget | **PASS** |
| 2 | A2 — Listing A + "האם הדירה מתאימה למשפחה?" | Explicit buyer goal materially changes planning | Family-relevant nearby evidence prioritized; no unsupported "good for families"/"safe area"/"good schools" claims made | `geo_resolver`/`nearby_services` executed as expected; `price_signal` also executed, driven by the listing's own "מחיר מציאה" claim (price provenance rule) | Report correctly hedged nearby-service evidence and limitations | ~live, within budget | **PASS_WITH_FINDING** (tool-efficiency/goal-focus note, not a blocking defect) |
| 3 | A3 (original) — Listing A + "האם המחיר הגיוני ביחס לאזור?" | Explicit comparative-price planning | Public `triage_planner` step showed only `geo_resolver`+`price_signal`, but the executed evidence bundle also contained an unrequested `nearby_services` call | Root cause: `plan_validator`'s nearby-synthesis repair fired regardless of `buyer_goal` | Discrepancy between declared plan and executed evidence (observability + goal-focus issue) | ~live, within budget | **FAIL_FIXED** → Stage 1.6 (`buyer_goal == "price_validation"` now excluded from synthesis) |
| 4 | A3 rerun (post Stage 1.6/1.7 fix) | Live confirmation of the Stage 1.6 fix | `nearby_services` fix confirmed (never executed); but `geo_resolver` returned "ambiguous" (planner address typo), so `price_signal` was legitimately skipped | `price_signal` skip was a genuine, correctly-typed upstream limitation, not a planner defect | New finding: `report_writer` described the skipped `price_signal` as "already checked in an earlier turn" in a fresh, stateless conversation with no earlier turn | 189s | **FAIL_FIXED** → Stage 1.8 (status-aware provenance wording added to `report_writer`) |
| 5 | Missing-data scenario (Listing B, sqm and house number absent) + "האם המחיר הגיוני ביחס לאזור?" | Knowing when *not* to act | No invented sqm, no invented house number, no `price_signal` (no sqm to compare), no `nearby_services` (goal-focused despite river/park mentions), report explicitly named the missing sqm as the blocker | Correctly declined to plan an unexecutable price check | Fully grounded — every limitation was real | ~live, within budget | **PASS** |
| 6 | Manual UI — "יש פארק קרוב?" | Basic nearby-services check via the GUI | Reported by the evaluator as behaving correctly (address resolved, park evidence returned) as the opening turn of a multi-turn manual session | `geo_resolver` + `nearby_services` | Consistent with deterministic design | manual UI, not logged/captured for this report | **PASS** (evaluator-observed; not independently re-verified live to conserve budget) |
| 7 | Manual UI follow-up — "כמה רחוק?" | Follow-up resolves to the previous turn's focus and reuses cached evidence | Reported as reusing the park evidence from turn 6 rather than re-running the tool | No tool re-executed | Deterministic reuse behavior is separately regression-tested (`tests/agent/conversation/test_context.py`, `build_compact_follow_up_context`) | manual UI | **PASS** (evaluator-observed + deterministic coverage) |
| 8 | Manual UI follow-up — "ומה עם סופר?" | Explicit new topic switches focus and only collects newly-required evidence | Reported as switching focus to supermarkets without re-fetching park evidence | New `nearby_services(supermarket)` call only | Deterministic focus-switch/incremental-evidence behavior is regression-tested | manual UI | **PASS** (evaluator-observed + deterministic coverage) |
| 9 | Manual UI follow-up — supermarket-distance question | Reuse of just-fetched evidence across a further follow-up | Reported as reusing the turn-8 supermarket evidence without a further tool run | No tool re-executed | Consistent with the always-`status="ok"` reused-evidence design (`conversation/cache.py`) | manual UI | **PASS** (evaluator-observed + deterministic coverage) |
| 10 | Manual UI — "האם מתאימה לסטודנטים?" (initial) | Persona-intent preservation across a follow-up | Final answer incorrectly discussed family suitability (schools/daycare) instead of the actual "students" question asked | `buyer_goal` misclassified as `family_suitability` by `triage_planner` (root cause A — LLM classification gap; no deterministic/code path injected "family") | Ungrounded — answered a question that was not asked | manual UI | **FAIL_FIXED** → Stage 4.2 (planner-prompt persona rule + report-writer persona-substitution guard) |
| 11 | A4 — Listing "ראובן רובין 6" + "האם מתאימה לסטודנטים?" (post-fix live verification) | Live confirmation of the Stage 4.2 persona fix | `buyer_goal = "general_question"` (correct); "family_suitability" did not appear anywhere; "students" preserved throughout; address "ראובן רובין 6" preserved verbatim (not corrupted to "רחוב רובין 6"); no unsupported student-suitability conclusion invented | `executed_tools = [geo_resolver, nearby_services]` — a side-effect of the (then-unfixed) validator synthesizing a nearby check for a retained park claim even though the planner itself proposed no tools | Grounded overall, but surfaced two new findings (see #14/#15) | 189s | **PASS_WITH_FINDING** |
| 12 | Deterministic — unresolvable/out-of-scope address handling | The agent never fabricates a geo-resolution for an address it cannot genuinely resolve | `geo_resolver` returns a typed `not_found`/`ambiguous` outcome rather than inventing coordinates or silently guessing a match (`tests/tools/test_live_geo_resolver.py`) | No downstream tool call proceeds on a fabricated resolution | Deterministic, regression-tested | offline | **PASS** |
| 13 | Deterministic — unsupported nearby-category claim handling | The agent never fabricates a nearby-services category for a claim outside the fixed 7-category list (e.g. "robotic parking", generic "educational institutions", a beach/sea view) | `_ensure_nearby_services_for_known_claims` synthesizes a call only when the claim text actually resolves to a supported category; otherwise no call is added | No fan-out to a default category set to "support" an unsupported claim | Deterministic, regression-tested (`tests/agent/test_plan_validator.py`) | offline | **PASS** |
| 14 | Deterministic — `general_question` must not synthesize an unrelated nearby check | Regression for finding #11's tool-behavior note | `buyer_goal="general_question"` + a retained `nearby_amenities` claim + empty planner `tool_calls` now yields `tool_calls == []` after validation (no synthesis) | Matches the `general_question` contract ("selects nothing on its own") | Deterministic, regression-tested | offline | **FAIL_FIXED** → Stage 4.4 |
| 15 | Deterministic — internal truncation-marker sanitization | Regression for finding #11's report-writer terminology note | The raw `additional_claims_truncated`/`additional_claims_truncated_detail: ...` internal diagnostic markers are stripped from the compact payload sent to `report_writer` (replaced with one plain-language item); real missing fields (e.g. `sqm`, `raw_address`) still pass through unchanged | N/A (deterministic payload construction) | Deterministic, regression-tested (`tests/llm/test_report_writer.py`) | offline | **FAIL_FIXED** → Stage 4.4 |

## Findings discovered during acceptance

The acceptance process was designed to stress-test orchestration and
grounding, not just happy-path behavior — and it did exactly that. Every
finding below was caught before submission and fixed with a small,
targeted, regression-tested change:

- **`price_validation` had unrelated nearby synthesis** — the deterministic
  plan repair could add a `nearby_services` call even when the buyer had
  explicitly narrowed the turn to a price question.
  → Fixed (Stage 1.6) and live-verified (Stage 1.7/1.8 reruns).
- **A skipped tool could be mislabeled as "reused from an earlier turn"** —
  a fresh, stateless request's legitimately-skipped `price_signal` was
  described with false "earlier turn" provenance wording.
  → Fixed with status-aware `report_writer` provenance logic (Stage 1.8).
- **"Students" could be silently reinterpreted as "family"** — an
  uncovered-persona suitability question was misclassified by the planner
  and faithfully (but wrongly) amplified by the report writer.
  → Fixed with explicit persona-mapping guidance and a report-writer
  persona-substitution guard (Stage 4.2), live-verified (Stage 4.3).
- **`general_question` could still synthesize an unrelated nearby check** —
  discovered live during the Stage 4.3 persona-fix verification itself.
  → Fixed deterministically (Stage 4.4), regression-tested.
- **An internal truncation marker could leak into the buyer-facing
  report** — also discovered live during Stage 4.3.
  → Fixed deterministically at the report-writer input boundary rather
  than relying on prompt wording alone (Stage 4.4), regression-tested.

Each of these was found by a structured, budget-conscious acceptance
process (live runs reserved for the primary question being tested, with
root-causing and fixing done offline/deterministically wherever possible),
and each has permanent regression coverage preventing a silent
reintroduction.

## Result counts

- **PASS**: 8 (#1, #5, #6, #7, #8, #9, #12, #13)
- **PASS_WITH_FINDING**: 2 (#2, #11)
- **FAIL_FIXED**: 5 (#3, #4, #10, #14, #15)
- **FAIL (unresolved)**: 0

All previously-identified issues were fixed and are covered by permanent
regression tests. No scenario in this table ended in an unresolved failure.
