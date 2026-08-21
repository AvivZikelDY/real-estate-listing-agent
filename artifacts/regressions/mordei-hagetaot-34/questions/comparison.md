# Mordei HaGetaot 34 — Buyer-Question Routing (two real planner calls)

Two authorized real LLMod `triage_planner` calls (one per buyer question),
stopped before tool execution. No MCP, no Nir tools, no external provider,
no `report_writer`. No production code changed.

## Frozen nearby-services contract (verified in production)

`NearbyServiceCategory` (`src/listing_agent/tools/schemas.py:113`):

```
supermarket, school, park, pharmacy, daycare, public_transport, hospital
```

- `public_transport` **is** supported. Aliases
  (`tool_input_builder._NEARBY_CATEGORY_ALIASES`): `public_transport`,
  `transport`, `תחבורה ציבורית`.
- `shelter` / `מקלט` is **not** a category and has no alias.
- `light_rail`, `bus_stops`, `הקו הסגול` are **not** aliases (no
  listing-specific special case exists, by design).

## Comparison

```
Question          | Category resolved  | Supported | Tool Plan                                   | Result
shelter (מקלט)     | none (shelter)     | no        | geo_resolver + nearby_services([public_transport])* | PASS_WITH_LIMITATION
public transport   | public_transport   | yes       | geo_resolver + nearby_services([public_transport])  | PASS
```

\* In the shelter run the planner *also* extracted the listing's own light-rail
claim and proposed `services=["shelter","public_transport"]`. Deterministic
normalization dropped the unsupported `shelter` and kept `public_transport`,
so the final call is `["public_transport"]` — **never** an all-category
fan-out. The `public_transport` tool therefore comes from a genuinely
supported *listing* claim the LLM surfaced, not from the shelter intent.

## Key deterministic invariants (both runs)

- No empty-category synthesis.
- No all-category fan-out (never school/park/daycare/pharmacy/hospital/
  supermarket).
- Narrow public-transport question → exactly `["public_transport"]`.
- Unsupported `shelter` correctly yields no `shelter` tool.
- `price_signal`: **NOT NEEDED** in both runs (asking price present, but no
  comparative listing claim and no comparative buyer question). No price
  regression.

## Price invariant

```
Run              | asking price | buyer price q | comparative claim | price_signal
shelter          | yes          | no            | no                | NOT NEEDED
public transport | yes          | no            | no                | NOT NEEDED
```

## Failure classification

- **Deterministic regression:** none. All deterministic guarantees held.
- **Tool-contract limitation:** `shelter`/`מקלט` is outside Nir's frozen
  nearby contract → correctly unsupported. Not fixed here (do not expand the
  contract).
- **Extraction observation (shelter run):** the planner included the
  listing's `public_transport` claim under a shelter-only question. This is
  an LLM extraction/relevance choice, not a deterministic broadening — the
  deterministic layer narrowed the planner's `["shelter","public_transport"]`
  down to `["public_transport"]`. Single stochastic observation; not turned
  into a regression.

## Verdict

BUYER_QUESTION_ROUTING_PASS_WITH_LIMITATION
