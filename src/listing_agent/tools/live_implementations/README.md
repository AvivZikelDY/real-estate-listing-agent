# Live tool implementations

Real implementations of the three tools defined in `docs/nir_handoff.md`,
backed by live government and OpenStreetMap data. Selected when
`TOOLS_MODE=live` (see `src/listing_agent/tools/dependency.py`); the `Mock*`
implementations are used otherwise.

> **Maintenance note:** this document is updated on request only. It is not
> refreshed automatically after every change, so treat it as a description
> of the design rather than a guaranteed-current changelog.

---

## The shared contract

All three tools return `ToolResult[...]` from `tools/schemas.py` and **never
raise**. The key distinction, which the whole design rests on:

| | meaning |
|---|---|
| `status="ok"` | the tool ran and produced a well-formed answer — **including "we looked and found nothing useful"** |
| `status="error"` | the tool itself broke (data source unreachable, corrupt file) |
| `status="skipped"` | the tool intentionally did nothing (only `price_signal`, on rentals) |

A business "no answer" lives in the output's typed `outcome` field, never in
`status`. That is what lets `report_writer` tell *"we checked, and here's
what we honestly found"* apart from *"the check itself broke."*

---

## `geo_resolver` → `LiveGeoResolver`

**What it does.** Takes a free-text address as a human wrote it and decides
whether it points at exactly one real place.

**Data source.** govmap `POST /search-service/autocomplete`. **Live** — 1
call (~0.3s), or 3 with neighbourhood lookup.

### Why a snapshot could never work here (unlike price_signal/nearby_services)

`price_signal` and `nearby_services` need aggregate data over a *finite*
area, so an offline extract is at least structurally possible there (both
keep one, for tests/reference - neither is read by the live request path
today). `geo_resolver` needs a lookup over an *unbounded* key space — every
address anyone might type, including misspellings. A pre-fetched gazetteer
would answer `not_found` for anything not on the list, silently turning
real addresses into failures - so this tool was never snapshot-based to
begin with.

### Flow

1. Fold `city_hint` into the query text (govmap has no city field — its
   search is one free-text box).
2. `autocomplete(query)`.
3. Drop results whose `type` is not address-like (see below).
4. Decide `resolved` / `ambiguous` / `not_found`.
5. Convert the winner's Web Mercator point to WGS84.
6. Best-effort neighbourhood lookup (2 more calls).

### How the outcome is decided

By the **score gap**, not the result count. govmap returns 4 matches where
the top is 9× the runner-up (a clear answer plus noise) *and* 6 matches with
identical scores in six different cities (no answer at all) — counting
results misclassifies both.

| | rule | real example |
|---|---|---|
| `resolved` | one result, or top ≥ 2× runner-up | `שבזי 20 תל אביב` → 4725 vs 515 |
| `ambiguous` | several, none dominant | `הרצל תל אביב` → 3948 / 3581 / 3269 |
| `not_found` | nothing address-like | `כתובת לא קיימת 999` |

`resolution_quality` comes from the match `type`: `address` → `exact`,
`street` → `approximate`, `neighborhood`/`settlement` → `low_confidence`.

**Only address-like types are accepted** (`_ADDRESSABLE_TYPES`). This is
load-bearing: govmap indexes roads in the same search, and
`"כתובת לא קיימת 999"` matched two `transportation` records named "999" plus
Highway 999 — reported as an *ambiguous address* until the filter was added.

**Neighbourhood lookup takes a detour.** govmap has no "neighbourhood at
this point" endpoint — `layers-catalog/entitiesByPoint` returns HTTP 400 for
every payload shape the public reference clients document. So we find a
nearby parcel and read the `neighborhood` field off its transactions. It's
best-effort: failure downgrades the answer (no neighbourhood) but never
fails the resolution, since the coordinates are already correct.

---

## `price_signal` → `LivePriceSignal`

**What it does.** Compares a listing's price per m² against real recorded
sales near the same address, and reports the deviation from the local median.
A *signal*, never a valuation.

**Data source.** govmap real-estate endpoints. **Live** (~1.2–1.7s
end-to-end). There is no runtime snapshot fallback: a technical failure
(unreachable, timeout, rate limit) propagates as `status="error"`, exactly
like the other two tools.

### Flow — 4 HTTP calls

```
1. POST /search-service/autocomplete          address  -> point
2. GET  /real-estate/deals/{x},{y}/{radius}   point    -> parcel id
3. GET  /real-estate/street-deals/{id}        \ issued
4. GET  /real-estate/neighborhood-deals/{id}  / concurrently
```

### The single-parcel design

Step 2 walks a **radius ladder (5 → 10 → 25 → 100)** and stops at the first
rung that returns a parcel — the closest parcel to the address point is the
address's own building.

This is **correct by construction**: a parcel belongs to exactly one
neighbourhood, so the address's parcel belongs to the address's
neighbourhood. Two markets cannot be blended.

The earlier design swept a 300-unit radius and queried 10 parcels (~22
calls, ~2.7s), which straddled boundaries — `אילת 30` returned 5 parcels in
`צפון יפו` and 5 in `נווה צדק`, averaging two different markets into one
meaningless median. All the filtering that existed to repair that is gone.

The ladder is not optional: a fixed radius of 10 found a parcel for 9 of 10
test addresses but **nothing** for `דיזנגוף 200`, which needs 25.

### The runtime failure rule

- **Technical failure** (unreachable, timeout, rate limit) → typed
  `status="error"`. There is no runtime snapshot fallback: a real
  request either succeeds live or reports the failure honestly.
  `comparables_snapshot.json` (see "Regenerating the data files" below)
  is a regeneratable offline development extract used by tests/reference,
  not read by the live request path.
- **Empty live result** → **no fallback.** govmap saying "no recorded sales
  here" is a real answer; substituting data for a different area would turn
  an honest `no_comparable_data` into a confident wrong number.

### The maths

Everything reduces to `dealAmount / assetArea` per transaction. The pool is
then narrowed in four steps, each with a fallback:

1. **Residential only** — filtered in the client on `dealNatureDescription`.
   **Mandatory.** A real 500-deal sample held 406 apartments but also 29
   offices, 20 shops, 15 parking spaces and 14 storage units, including a
   500 m² office at ₪33.3M. A price-per-m² sanity band does *not* catch
   these — a parking space lands inside a plausible range.
2. **Sanity band** ₪3k–250k/m² — catches corrupt rows, e.g. a mis-keyed 1 m².
3. **Recency** — within 3 years of the newest deal in the set (relative, not
   wall-clock, so results stay deterministic). Falls back if under 8 remain.
4. **Size band** ±25% of the listing's area. Falls back to all sizes if
   under 5 remain, and says so.

Then `median`, `p10`/`p90` as the reference range (percentiles, so one
outlier can't stretch it), and:

```
deviation_percent = (asking_price_per_sqm − median) / median × 100
```

### Outcomes

| `outcome` | when |
|---|---|
| `compared` | a real comparison was computed |
| `insufficient_data` | listing lacks `asking_price` and/or `area_sqm` |
| `no_comparable_data` | unknown area, or fewer than 3 usable sales |

Rentals return `status="skipped"` — checked **first**, before anything else,
so it holds regardless of what other fields are present.

---

## `nearby_services` → `LiveNearbyServices`

**What it does.** Finds schools, parks, supermarkets, pharmacies, daycares,
transport stops and hospitals within a radius of the resolved coordinates.

**Data source.** OpenStreetMap via Overpass, queried **live** on every
request, with mirror fallback (a different Overpass server, not a
snapshot). There is no runtime offline snapshot: a transport failure
across all mirrors propagates as `status="error"`, exactly like the other
two tools.

### About `poi_snapshot.json`

Overpass is a free, donation-funded query engine and is occasionally
overloaded or slow. `poi_snapshot.json` (2,949 places across Tel Aviv) and
its generator (`build_poi_snapshot.py`) are a regeneratable offline
development/test extract, kept for fast, deterministic tests and as a
reference point - **the live request path never reads it.** The live
client (`overpass_client.py`) is fully written and is exercised every time
the snapshot is regenerated.

### Notes

- **Distances are straight-line.** Not walking distance, not walking time.
  Disclosed in `limitations` on *every* result, including empty ones.
- **`evidence_quality` requires breadth, not just volume.** 20 bus stops is
  a well-mapped bus route, not a well-served area — that scores `medium`.
  `high` needs ≥15 places across ≥4 categories.
- **A point outside the bounding box is flagged explicitly**, so "we hold no
  data here" never reads as "there is nothing here".
- An empty result is `status="ok"` with `evidence_quality="low"` — not an
  error.

---

## Shared modules

| file | purpose |
|---|---|
| `coordinates.py` | Web Mercator ↔ WGS84, WKT `POINT` parsing, haversine |
| `govmap_client.py` | async govmap client (`ComparableDeal`) |
| `overpass_client.py` | async Overpass client with mirror fallback (`OsmPlace`) |
| `build_snapshot.py` | regenerates `comparables_snapshot.json` |
| `build_poi_snapshot.py` | regenerates `poi_snapshot.json` |

Both clients return the **same types** the snapshot files rehydrate into
(so tests exercising the offline extracts stay honest about the live
shape), but only the generator scripts read the snapshot files - the live
runtime request path never does.

### Regenerating the data files

```bash
python -m src.listing_agent.tools.live_implementations.build_snapshot
python -m src.listing_agent.tools.live_implementations.build_poi_snapshot
```

Both refuse to overwrite good data with an empty result.

---

## API gotchas worth knowing

**Coordinates are EPSG:3857 Web Mercator, not ITM/EPSG:2039.** Every public
reference client mislabels this. `POINT(3870871.7 3771605.2)` decodes as
Web Mercator to Rothschild 30 exactly; as ITM it is off by three orders of
magnitude (the true ITM is `178697, 663505`). `test_coordinates.py` pins a
real captured point so nobody "corrects" it back.

Web Mercator metres are inflated by `1/cos(latitude)` ≈ **1.18×** at Tel
Aviv, so a radius of 10 is ~8.5 real metres.

**`/real-estate/deals/{x},{y}/{r}` returns no deals** despite its path — only
parcel metadata (`polygon_id`, `dealscount`, `streetNameHeb`, …). It also
caps at 100 rows regardless of radius.

**`dealscount` is a string.** `"0"` is truthy in Python.

**The deal endpoints accept only gush–helka parcel IDs.** Neighbourhood layer
IDs and names both return HTTP 500.

**Typed errors must be imported lazily.** Importing
`agent.tool_executor.errors` at module level runs that package's
`__init__.py`, which imports `tools.dependency`, which imports this package
back — a circular import that fails at startup. Both clients import them
inside the request method instead.

---

## Known gaps

- **`_throttle` under-throttles.** Concurrent `asyncio.gather` pairs read the
  clock before either writes it back, so both pass as one slot. Largely moot
  now that a lookup is 4 calls rather than 22.
- **`rooms` is in `PriceSignalInput` and unused.** It is 99.9% populated in
  the real data; comparables are matched on area instead, since area is the
  denominator of the metric. Best use would be as the fallback when the ±25%
  area band collapses.
- **`PriceSignalInput` carries no `lat`/`lon`**, so the live path re-resolves
  a point `geo_resolver` already had — one wasted call out of four.
- **Neighbourhood names are Hebrew.** The mock `geo_resolver` emits English;
  the snapshot carries aliases for both, but the two resolvers should agree.
