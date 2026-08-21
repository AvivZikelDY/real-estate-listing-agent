# Handoff to Nir — Tools Implementation

**Status: integrated.** Nir's real implementations have landed and been
merged with Aviv's conversation/cache feature branch on
`integration/tools-and-context`. The sections below are kept largely as
originally written (they remain the accurate contract/ownership
reference), with status notes added where the original text described
future/in-progress work that is now done.

## What you own

You own **only** the real implementations and data sources for these three
tools:

- `geo_resolver`
- `nearby_services`
- `price_signal`

You do not need to touch, and should not modify, anything else in this
repository: the FastAPI app, the API routes, the agent orchestration, the
LLM modules, the GUI, or the tests outside `tests/tools/`.

## Where your code goes

Your real implementations are in place and wired into the app (no longer
`NotImplementedError` placeholders):

```
src/listing_agent/tools/nir_implementations/geo_resolver.py      -> NirGeoResolver
src/listing_agent/tools/nir_implementations/nearby_services.py   -> NirNearbyServices
src/listing_agent/tools/nir_implementations/price_signal.py      -> NirPriceSignal
```

`NirGeoResolver` resolves against live GovMap; `NirNearbyServices` queries
an OSM/Overpass-derived snapshot (with mirror fallback on transient
failures); `NirPriceSignal` compares against live GovMap transaction data,
with rental listings skipped and technical failures kept distinguishable
from a genuine "no comparable data" business outcome.

Which implementation actually runs is controlled by the `TOOLS_MODE`
environment variable (see `src/listing_agent/tools/dependency.py`):
- `TOOLS_MODE=mock` (the default, used by the entire pytest suite) uses
  Aviv's `Mock*` stand-ins under `src/listing_agent/tools/mock_implementations/`.
  **Your `Nir*` classes are never selected in this mode.**
- `TOOLS_MODE=nir` uses your real `Nir*` classes, backed by live
  GovMap/Overpass calls.

Your tests live under:
```
tests/tools/
```

## The contract you must satisfy

All shapes are Pydantic v2 models defined in
`src/listing_agent/tools/schemas.py`. Every tool's `execute()` returns the
same envelope type, `ToolResult[YourOutputModel]`:

```python
class ToolResult(BaseModel, Generic[DataT]):
    status: Literal["ok", "error", "skipped"]
    data: DataT | None = None
    error: str | None = None
    reason: str | None = None
```

**Never raise an unhandled exception from `execute()`.** Every failure
must come back as a normal return value:
- `status="ok"` + `data=<YourOutputModel>` — the tool ran and produced a
  well-formed answer. **This includes "no usable business answer" cases**
  (address not found, zero nearby services, insufficient listing data for
  a price comparison) — those are captured in a typed `outcome` field on
  your output model, not turned into `status="error"`. Only a genuine
  technical failure (e.g. your upstream data source is unreachable or
  errors out) is `status="error"`.
- `status="error"` + `error="<human-readable message>"` on a genuine
  technical failure.
- `status="skipped"` + `reason="<human-readable message>"` when the tool
  intentionally does nothing for this input (only applies to
  `price_signal` on rental listings — see below).

### Optional: signalling a precise failure reason (Phase 14)

You may still simply return `ToolResult(status="error", error="...")` as
above — the executor layer will classify it as a generic technical
failure. If you want to give the orchestration layer a more precise
signal (so it can decide whether a single automatic retry is worthwhile),
you may instead **raise** one of the typed exceptions from
`src/listing_agent/agent/tool_executor/errors.py` from inside `execute()`:
`ToolTimeoutError`, `ToolRateLimitedError`, `ToolProviderUnavailableError`
(all retried once automatically), or `ToolInvalidInputError`,
`ToolNotFoundError`, `ToolAmbiguousResultError`, `ToolInsufficientDataError`,
`ToolInternalError` (never retried). This is optional — plain
`ToolResult(status="error", ...)` remains fully supported and is treated
as `internal_error` (never retried).

### `geo_resolver`

**Input schema** — `GeoResolverInput`:
| field   | type | notes |
|---------|------|-------|
| `address` | `str` | Raw address/location string as extracted by `triage_planner`. May be informally formatted. |
| `city_hint` | `str \| None` | Optional city hint to help disambiguate. |

**Output schema** — `GeoResolverOutput` (wrapped in `ToolResult(status="ok")`):
| field | type | notes |
|-------|------|-------|
| `outcome` | `"resolved" \| "ambiguous" \| "not_found"` | The typed business result. |
| `normalized_address` | `str \| None` | Cleaned/canonical form of the address. Set only when `outcome="resolved"`. |
| `lat` | `float \| None` | Resolved latitude (`-90..90`). Set only when `outcome="resolved"`. |
| `lon` | `float \| None` | Resolved longitude (`-180..180`). Set only when `outcome="resolved"`. |
| `resolution_quality` | `"exact" \| "approximate" \| "low_confidence" \| None` | Set only when `outcome="resolved"`. |
| `neighborhood` | `str \| None` | The Tel Aviv neighborhood name, if known. |
| `candidates` | `list[str]` | Alternative address strings. Set only when `outcome="ambiguous"`. |
| `source` | `str` | Identifies your data source (e.g. a provider name or `"mock_dataset"`). |
| `limitations` | `list[str]` | Any caveats about this result. |

**Outcome/error behavior:**
- `outcome="resolved"`: a single, confident match — `normalized_address`,
  `lat`, `lon`, and `resolution_quality` must all be set. Still
  `status="ok"`.
- `outcome="ambiguous"`: more than one plausible match — `candidates`
  lists them. Still `status="ok"`.
- `outcome="not_found"`: no match at all. Still `status="ok"`.
- `status="error"` is reserved for a genuine technical failure (e.g. your
  data source is unreachable). Never let a network/parsing exception
  escape `execute()`.

### `nearby_services`

**Input schema** — `NearbyServicesInput`:
| field | type | notes |
|-------|------|-------|
| `lat` | `float` | Latitude (`-90..90`), typically from `geo_resolver`'s output. |
| `lon` | `float` | Longitude (`-180..180`), typically from `geo_resolver`'s output. |
| `categories` | `list[NearbyServiceCategory]` | Which service categories to search for (at least one; defaults to all supported categories). |
| `radius_m` | `int` | Search radius in meters (default `500`, must be `>0`). |

**Output schema** — `NearbyServicesOutput` (wrapped in `ToolResult(status="ok")`):
| field | type | notes |
|-------|------|-------|
| `places` | `list[NearbyPlace]` | Individual matches: `name: str`, `category: str`, `distance_m: float`, `source: str`. |
| `categories` | `list[ServiceCategorySummary]` | One entry per service category found: `category: str`, `count: int`, `nearest_distance_m: float`. |
| `evidence_quality` | `"low" \| "medium" \| "high"` | Your assessment of how much you can trust this result. |
| `limitations` | `list[str]` | **Must always include** a note that distances are straight-line (as-the-crow-flies) estimates, not walking distances or walking times, plus any data-source caveats. |

**Error behavior:** if your data source is unavailable, return
`ToolResult(status="error", error="...")`. An empty/no-match result is
still `status="ok"` with `places=[]`, `categories=[]`, and
`evidence_quality="low"` — it is not an error.

### `price_signal`

**Input schema** — `PriceSignalInput`:
| field | type | notes |
|-------|------|-------|
| `normalized_address` | `str` | Canonical address, typically from `geo_resolver`'s output. |
| `listing_type` | `"purchase" \| "rental" \| "unknown"` | From the triage plan. |
| `asking_price` | `float \| None` | The listing's asking price (`>0` if present). |
| `area_sqm` | `float \| None` | The listing's size in square meters (`>0` if present). |
| `rooms` | `float \| None` | The listing's room count (`>0` if present). |
| `neighborhood` | `str \| None` | Optional comparables lookup key, typically from `geo_resolver`'s output. |

**Output schema** — `PriceSignalOutput` (wrapped in `ToolResult(status="ok")`):
| field | type | notes |
|-------|------|-------|
| `outcome` | `"compared" \| "insufficient_data" \| "no_comparable_data"` | The typed business result. |
| `asking_price_per_sqm` | `float \| None` | `asking_price / area_sqm`. Set only when `outcome="compared"`. |
| `median_comparison_price_per_sqm` | `float \| None` | Median of comparable transactions. Set only when `outcome="compared"`. |
| `reference_range_low_per_sqm` | `float \| None` | Low end of the comparison range, when available. |
| `reference_range_high_per_sqm` | `float \| None` | High end of the comparison range, when available. |
| `deviation_percent` | `float \| None` | `(asking_price_per_sqm - median) / median * 100`. Set only when `outcome="compared"`. |
| `comparison_count` | `int` | Number of comparison transactions used (`0` when none). |
| `missing_fields` | `list[str]` | Which required listing fields were missing. Set only when `outcome="insufficient_data"`. |
| `source` | `str` | Identifies your data source. |
| `limitations` | `list[str]` | **Must always include** a note that this is a data-driven signal only, not a professional or certified appraisal, plus any data-source caveats. |

**Outcome/error behavior:**
- **Rental listings must be skipped**, not evaluated: if
  `listing_type == "rental"`, return
  `ToolResult(status="skipped", reason="...")`.
- If `asking_price` or `area_sqm` is missing, return `status="ok"` with
  `outcome="insufficient_data"` and `missing_fields` listing exactly
  what's missing — this is not a technical error.
- If no comparison data is available for the address/neighborhood, return
  `status="ok"` with `outcome="no_comparable_data"` — also not a
  technical error.
- `status="error"` is reserved for a genuine technical failure (e.g. your
  data source is unreachable or errors out).

## Tests Nir must satisfy

Your `pytest tests/tools/` suite (see the existing mock tests in
`tests/tools/test_mock_*.py` and the schema tests in
`tests/tools/test_tool_schemas.py` for the shape/spirit of these) must
cover, at minimum:

1. A known/resolvable address returns `status="ok"` with
   `outcome="resolved"` and correct `GeoResolverOutput` fields.
2. An unresolvable address returns `status="ok"` with `outcome="not_found"`
   (not a raised exception, and not `status="error"`).
3. An ambiguous address returns `status="ok"` with `outcome="ambiguous"`
   and a populated `candidates` list.
4. A location with nearby services returns `status="ok"` with non-empty
   `places`/`categories` and the straight-line-distance limitation
   present.
5. A location with no nearby services returns `status="ok"` with
   `places=[]`, `categories=[]`, and `evidence_quality="low"`.
6. A valid purchase listing (address + asking_price + area_sqm all
   present, with known comparison data) returns `status="ok"` with
   `outcome="compared"` and correct price-signal math.
7. A purchase listing missing `asking_price` or `area_sqm` returns
   `status="ok"` with `outcome="insufficient_data"` and the correct
   `missing_fields`.
8. A purchase listing with no comparison data for its
   address/neighborhood returns `status="ok"` with
   `outcome="no_comparable_data"`.
9. A rental listing returns `status="skipped"`, regardless of what other
   fields are present.
10. Any internal failure (e.g. your data source throwing) is caught
    inside `execute()` and returned as `status="error"` — write a test
    that forces this failure path and asserts no exception propagates out
    of `execute()`.
11. `name` on each of your classes exactly matches `"geo_resolver"`,
    `"nearby_services"`, `"price_signal"` respectively, and each class
    satisfies `src.listing_agent.tools.base.BaseTool` (register it in a
    `ToolRegistry` in a test and call `execute()` through the registry).

## Hard constraints (apply to your work too)

- No databases (SQL or NoSQL) — use files, in-memory data, or external
  APIs/computation instead.
- No LLM calls inside any tool. Tools are deterministic data/logic only.
- No authentication, no new HTTP frameworks, no new architecture — your
  code is a plain Python class that gets called through `ToolRegistry`.
- Keep dependencies minimal; if you need a new third-party package, check
  with Aviv first since it must fit the existing stack (Python 3.12,
  httpx for any outbound HTTP calls, Pydantic v2 for data shapes).

## Stateful follow-up context/caching (integrated, does not affect you)

Aviv's optional, in-memory "conversation follow-up" feature (see
`docs/contracts.md` §2 `agent_orchestrator` and
`docs/follow_up_context_plan.md`) — which lets a buyer ask follow-up
questions about the same listing without repeating tool calls — is now
merged and verified against your real implementations, including under
`TOOLS_MODE=nir` with live GovMap/Overpass. It remains implemented
entirely **above** the `ToolExecutor` seam
(`src/listing_agent/agent/conversation/`) and still changes nothing about
your tools:

- Your tools' input/output schemas (`GeoResolverInput`/`Output`,
  `NearbyServicesInput`/`Output`, `PriceSignalInput`/`Output`) are
  completely unchanged by the merge.
- `ToolRegistry`/`ToolExecutor` remain unaware that reuse/caching exists —
  whichever tool calls actually still need to run reach you exactly as
  before, through the exact same `execute(input_model) -> ToolResult`
  interface, whether `TOOLS_MODE` is `mock` or `nir`.
- Cache keys are derived from the exact typed input each tool receives
  (e.g. the geo cache key is built from `GeoResolverInput.model_dump()`),
  so your city-hint canonicalization and address-normalization
  improvements make the cache *more* stable across a conversation's turns,
  not less — verified with a live multi-turn conversation (5 turns,
  `TOOLS_MODE=nir`): repeated/short follow-ups correctly reused cached
  `geo_resolver`/`nearby_services`/`price_signal` results, and a changed
  `asking_price` correctly invalidated only the price cache entry, without
  either of us needing to add TTL/LRU/Redis/vector memory or a second LLM
  call.
- One integration-time fix, in Aviv-owned code only: your
  `build_tool_input` now raises `MissingDependencyError` for `geo_resolver`
  itself when an address names no recognized city (previously only
  `nearby_services`/`price_signal` could raise this). Aviv's
  `agent/conversation/cache.py` and the diagnostic scripts were updated to
  treat that as "not cacheable, let the normal skipped-evidence path
  report it" rather than letting the exception escape the request. Your
  `geo_resolver`'s own no-city refusal behavior (raising
  `MissingDependencyError`, distinct from your `outcome="not_found"`
  business result) is unchanged.

## Known non-blocking limitations from the merge

- `PriceSignalInput` has no `lat`/`lon` field, so `NirPriceSignal` may
  perform its own address/coordinate lookup even on a turn where
  `geo_resolver`'s result is already cached and reused at the agent layer.
  This is a known optimization gap, not a defect — not addressed in this
  integration.
- Transient Overpass mirror failures (502/500/timeout) are handled by your
  existing fallback-mirror logic; on the rare case both mirrors fail, the
  turn correctly returns `status="error"` and is never cached, so a later
  identical turn simply retries.

## What Aviv provided while your implementations were pending

## How to submit your work

1. Implement inside `src/listing_agent/tools/nir_implementations/` only.
2. Add/extend tests under `tests/tools/`.
3. Run `pytest tests/tools/` and `ruff check src/listing_agent/tools` locally
   before handing off.
4. Open a PR (or hand off your branch/diff) touching only files under
   `src/listing_agent/tools/nir_implementations/*` and `tests/tools/*` —
   this keeps review scoped to your part of the system.

If anything in this document is unclear or seems to not match real data
availability (e.g. a data source doesn't actually expose a field), raise it
with Aviv rather than guessing — the contract may need a small, explicit
revision.
