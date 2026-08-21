# Hapalmach 21 — Unsupported Nearby Fan-out Fix

## Regression
`plan_validator._ensure_nearby_services_for_known_claims` synthesized a
`nearby_services` call for any retained `nearby_amenities` claim, even when
the claim text resolved to **no** supported frozen category. The synthesized
call carried empty `arguments={}`, which `build_tool_input` then expanded to
the full default category set (supermarket, school, park, pharmacy, daycare,
public_transport, hospital). For Hapalmach 21 the two nearby claims —
robotic parking and "community centre / educational institutions" — resolve
to no frozen category, so the tool fanned out to all seven categories,
fabricating evidence categories no retained claim supported.

## First point of failure
`_ensure_nearby_services_for_known_claims` (synthesis boundary) in
`src/listing_agent/agent/plan_validator/validator.py`.

## Fix (general, deterministic)
Synthesis now requires at least one **supported** frozen category to resolve
from the linked claims' combined text:

```
categories = resolve_nearby_categories(claim_text=combined_text)
if not categories:
    return tool_calls          # do NOT synthesize; no geo added solely for it
create nearby_services(categories=categories)
```

The empty-category → all-categories default remains **only** for a genuinely
broad, *planner-proposed* "what's nearby" request (handled in
`build_tool_input`). That is the one intentional broad-nearby behavior in the
system, and it is preserved because the fix only touches the validator's
synthesis path — not the public tool-input default.

## Why it is not Hapalmach-specific
The rule keys off category resolution (`resolve_nearby_categories`), never on
address, claim id, or listing text. Any `nearby_amenities` claim whose text
resolves to zero supported categories — robotic parking, community centre,
generic education wording, or anything else — produces no synthesized call.
Any claim that resolves to ≥1 supported category (park, school, daycare, …)
still runs, and mixed unsupported+supported claims keep only the supported
categories.

## Hapalmach before/after tool plan
- Before: `geo_resolver` → `nearby_services(categories=[all 7 defaults])`
- After:  **No external evidence tools needed.** (0 tools)

`price_signal` was correctly NOT NEEDED both before and after (asking price +
sqm alone never trigger it).

## Verification
- `ruff check .` — clean.
- Full `pytest` — 496 passed, 1 skipped.
- Scenario diagnostics (planner + evidence, `--scenario all --fake-llm`) —
  all 10 PASS.
- Regression test no longer uses `xfail`.

No new LLM call, no MCP, no external provider, no report_writer; Nir's
frozen tool contract unchanged.
