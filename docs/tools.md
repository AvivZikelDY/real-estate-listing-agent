# Tool Integrations

Technical reference for the three deterministic tools the agent can call:
`geo_resolver`, `nearby_services`, and `price_signal`. This document
describes the current system, not its development history.

## Available tools

- `geo_resolver` — resolves an address to coordinates/normalized form.
- `nearby_services` — finds nearby points of interest by category.
- `price_signal` — compares an asking price against comparable
  transactions.

## Which implementation runs — `TOOLS_MODE`

The `TOOLS_MODE` environment variable (`Settings.tools_mode`, see
`src/listing_agent/tools/dependency.py`) selects the whole tool set for a
process, never per-request:

- `TOOLS_MODE=mock` (the default, used by the entire pytest suite) uses
  the small, hardcoded `Mock*` implementations under
  `src/listing_agent/tools/mock_implementations/`.
- `TOOLS_MODE=live` uses the real `Live*` implementations under
  `src/listing_agent/tools/live_implementations/`:

```
src/listing_agent/tools/live_implementations/geo_resolver.py      -> LiveGeoResolver
src/listing_agent/tools/live_implementations/nearby_services.py   -> LiveNearbyServices
src/listing_agent/tools/live_implementations/price_signal.py      -> LivePriceSignal
```

`LiveGeoResolver` resolves against live GovMap; `LiveNearbyServices`
queries OpenStreetMap live via Overpass (with mirror fallback on
transient failures); `LivePriceSignal` compares against live GovMap
transaction data, with rental listings skipped and technical failures
kept distinguishable from a genuine "no comparable data" business
outcome. All three are live-only for a real runtime request — none of
them reads a snapshot file to answer at request time; the offline
snapshot files (`comparables_snapshot.json`, `poi_snapshot.json`) and
their generator scripts exist only as regeneratable development
extracts/fixtures.

Tests for both implementations live under:
```
tests/tools/
```

A standalone runner for exercising the live implementations directly
against real upstreams (never through pytest, since it makes real HTTP
calls) is `scripts/run_live_tool_tests.py`.

## The tool contract

All shapes are Pydantic v2 models defined in
`src/listing_agent/tools/schemas.py`. Every tool's `execute()` returns the
same envelope type, `ToolResult[OutputModel]`:

```python
class ToolResult(BaseModel, Generic[DataT]):
    status: Literal["ok", "error", "skipped"]
    data: DataT | None = None
    error: str | None = None
    reason: str | None = None
```

**No tool may raise an unhandled exception from `execute()`.** Every
failure comes back as a normal return value:
- `status="ok"` + `data=<OutputModel>` — the tool ran and produced a
  well-formed answer. **This includes "no usable business answer" cases**
  (address not found, zero nearby services, insufficient listing data for
  a price comparison) — those are captured in a typed `outcome` field on
  the output model, not turned into `status="error"`. Only a genuine
  technical failure (e.g. an upstream data source is unreachable or
  errors out) is `status="error"`.
- `status="error"` + `error="<human-readable message>"` on a genuine
  technical failure.
- `status="skipped"` + `reason="<human-readable message>"` when the tool
  intentionally does nothing for this input (only applies to
  `price_signal` on rental listings — see below).

### Optional: signalling a precise failure reason

A tool may simply return `ToolResult(status="error", error="...")` as
above — the executor layer classifies it as a generic technical failure.
For a more precise signal (so the orchestration layer can decide whether
a single automatic retry is worthwhile), a tool may instead **raise** one
of the typed exceptions from `src/listing_agent/agent/tool_executor/errors.py`
from inside `execute()`: `ToolTimeoutError`, `ToolRateLimitedError`,
`ToolProviderUnavailableError` (all retried once automatically), or
`ToolInvalidInputError`, `ToolNotFoundError`, `ToolAmbiguousResultError`,
`ToolInsufficientDataError`, `ToolInternalError` (never retried). Plain
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
| `source` | `str` | Identifies the data source (e.g. a provider name or `"mock_dataset"`). |
| `limitations` | `list[str]` | Any caveats about this result. |

**Outcome/error behavior:**
- `outcome="resolved"`: a single, confident match — `normalized_address`,
  `lat`, `lon`, and `resolution_quality` must all be set. Still
  `status="ok"`.
- `outcome="ambiguous"`: more than one plausible match — `candidates`
  lists them. Still `status="ok"`.
- `outcome="not_found"`: no match at all. Still `status="ok"`.
- `status="error"` is reserved for a genuine technical failure (e.g. the
  data source is unreachable). A network/parsing exception must never
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
| `evidence_quality` | `"low" \| "medium" \| "high"` | Assessment of how much this result can be trusted. |
| `limitations` | `list[str]` | **Must always include** a note that distances are straight-line (as-the-crow-flies) estimates, not walking distances or walking times, plus any data-source caveats. |

**Error behavior:** if the data source is unavailable, the tool returns
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
| `source` | `str` | Identifies the data source. |
| `limitations` | `list[str]` | **Must always include** a note that this is a data-driven signal only, not a professional or certified appraisal, plus any data-source caveats. |

**Outcome/error behavior:**
- **Rental listings are skipped**, not evaluated: if
  `listing_type == "rental"`, the tool returns
  `ToolResult(status="skipped", reason="...")`.
- If `asking_price` or `area_sqm` is missing, the tool returns
  `status="ok"` with `outcome="insufficient_data"` and `missing_fields`
  listing exactly what's missing — this is not a technical error.
- If no comparison data is available for the address/neighborhood, the
  tool returns `status="ok"` with `outcome="no_comparable_data"` — also
  not a technical error.
- `status="error"` is reserved for a genuine technical failure (e.g. the
  data source is unreachable or errors out).

## Required test coverage

`tests/tools/` (see the mock tests in `tests/tools/test_mock_*.py` and the
schema tests in `tests/tools/test_tool_schemas.py` for the shape/spirit of
these) covers, at minimum:

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
10. Any internal failure (e.g. the data source throwing) is caught
    inside `execute()` and returned as `status="error"` — a test forces
    this failure path and asserts no exception propagates out of
    `execute()`.
11. `name` on each tool class exactly matches `"geo_resolver"`,
    `"nearby_services"`, `"price_signal"` respectively, and each class
    satisfies `src.listing_agent.tools.base.BaseTool` (registered in a
    `ToolRegistry` in a test and called through the registry).

## Hard constraints

- No databases (SQL or NoSQL) — tools use files, in-memory data, or
  external APIs/computation instead.
- No LLM calls inside any tool. Tools are deterministic data/logic only.
- No authentication, no new HTTP frameworks, no new architecture — each
  tool is a plain Python class that gets called through `ToolRegistry`.
- Dependencies stay minimal and fit the existing stack (Python 3.12,
  `httpx` for any outbound HTTP calls, Pydantic v2 for data shapes).

## Stateful follow-up context/caching

The optional, in-memory "conversation follow-up" feature (see
`docs/contracts.md` §2 `agent_orchestrator` and
`docs/follow_up_context_plan.md`) lets a buyer ask follow-up questions
about the same listing without repeating tool calls. It is implemented
entirely **above** the `ToolExecutor` seam
(`src/listing_agent/agent/conversation/`) and changes nothing about the
tools themselves:

- The tools' input/output schemas (`GeoResolverInput`/`Output`,
  `NearbyServicesInput`/`Output`, `PriceSignalInput`/`Output`) are
  unaffected.
- `ToolRegistry`/`ToolExecutor` remain unaware that reuse/caching exists —
  whichever tool calls actually still need to run reach the tool exactly
  as if no caching existed, through the same `execute(input_model) ->
  ToolResult` interface, whether `TOOLS_MODE` is `mock` or `live`.
- Cache keys are derived from the exact typed input each tool receives
  (e.g. the geo cache key is built from `GeoResolverInput.model_dump()`),
  so city-hint canonicalization and address-normalization make the cache
  more stable across a conversation's turns, not less — verified with a
  live multi-turn conversation (5 turns, `TOOLS_MODE=live`):
  repeated/short follow-ups correctly reused cached
  `geo_resolver`/`nearby_services`/`price_signal` results, and a changed
  `asking_price` correctly invalidated only the price cache entry,
  without adding TTL/LRU/Redis/vector memory or a second LLM call.
- `build_tool_input` raises `MissingDependencyError` for `geo_resolver`
  itself when an address names no recognized city (in addition to
  `nearby_services`/`price_signal`, which can also raise this).
  `agent/conversation/cache.py` treats that as "not cacheable, let the
  normal skipped-evidence path report it" rather than letting the
  exception escape the request. `geo_resolver`'s own no-city refusal
  behavior (raising `MissingDependencyError`, distinct from its
  `outcome="not_found"` business result) is unchanged by caching.

## Known non-blocking limitations

- `PriceSignalInput` has no `lat`/`lon` field, so `LivePriceSignal` may
  perform its own address/coordinate lookup even on a turn where
  `geo_resolver`'s result is already cached and reused at the agent
  layer. This is a known optimization gap, not a defect.
- Transient Overpass mirror failures (502/500/timeout) are handled by the
  existing fallback-mirror logic; on the rare case both mirrors fail, the
  turn correctly returns `status="error"` and is never cached, so a later
  identical turn simply retries.
