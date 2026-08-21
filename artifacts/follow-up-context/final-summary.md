# Follow-up context & evidence reuse - demo summary

This demonstrates the 5-turn Definition-of-Done sequence for one
conversation about a single listing ("HaYarkon 120, Tel Aviv"), all
through the real `run_agent_pipeline` orchestrator, a `FakeLLMClient`
(deterministic, offline, no paid/real LLM calls) queued with the plan
each turn's planner would produce, and the mock `LocalToolExecutor`. Raw
data: `multi-turn-demo.json`.

| Turn | Question (he) | Reused | Executed | Tool calls saved |
|---|---|---|---|---|
| 1 | יש פארק קרוב? | (none - first turn) | geo_resolver, nearby_services | 0 |
| 2 | כמה רחוק? | geo_resolver, nearby:park | (none) | 2 |
| 3 | ומה לגבי בתי ספר? | geo_resolver | nearby_services (school only) | 1 |
| 4 | האם המחיר הוגן? | geo_resolver | price_signal | 1 |
| 5 | ומה אם המחיר יהיה 4.4 מיליון? | geo_resolver | price_signal (new price -> new cache key) | 1 |

## What this proves

- **Turn 1**: a brand-new conversation runs the full plan - `geo_resolver`
  then `nearby_services(["park"])`. Both successful results are cached.
- **Turn 2**: the follow-up "How far?" carries no property facts and no
  explicit topic. `triage_planner` resolves it to "park" using the
  compact follow-up context (`KNOWN PROPERTY` / `PREVIOUS BUYER QUESTION`
  / `PREVIOUS RELEVANT FOCUS: park`), and both the resolved geo and the
  cached park result are reused - **zero new tool calls**.
- **Turn 3**: a genuinely new topic ("what about schools?") reuses geo but
  executes `nearby_services` again - only for the missing `school`
  category, never re-requesting `park`.
- **Turn 4**: the Phase 0 price-provenance guard allows `price_signal`
  because the current prompt itself contains explicit comparative-price
  intent ("הוגן" = "fair"). Geo is reused; a fresh price comparison runs
  and is cached.
- **Turn 5**: the buyer proposes a different price (4.4M vs the original
  4.7M). The canonical `price_signal` cache key is built from every
  semantic field of `PriceSignalInput`, so a changed `asking_price`
  naturally produces a cache **miss** - the previous price result is never
  reused, and `price_signal` executes again with the new figure. No
  separate invalidation logic was needed for this.

Total external tool calls across all 5 turns: 6 (2 in turn 1, 0 in turn
2, 1 each in turns 3-5) instead of 10 if every turn had re-run its full
plan from scratch - a concrete, measurable demonstration of "minimal tool
usage" and "reuse of already collected external information".
