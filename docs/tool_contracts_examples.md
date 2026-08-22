# Tool Contract Examples

One input example, one successful-outcome example, and one
incomplete/no-result example for each of the three tools. All shapes are
frozen in `src/listing_agent/tools/schemas.py` — see `docs/contracts.md`
§4 and `docs/tools.md` for the full field-by-field contract.

Reminder: `status="ok"` covers **both** "we found a good answer" and "we
honestly found no usable answer" — the specific business result lives in
each output's typed `outcome` field. `status="error"` is reserved for a
genuine technical failure (e.g. the data source itself is unreachable).

## `geo_resolver`

**Input:**
```json
{ "address": "HaYarkon 120, Tel Aviv", "city_hint": null }
```

**Successful outcome** (`outcome="resolved"`):
```json
{
  "status": "ok",
  "data": {
    "outcome": "resolved",
    "normalized_address": "HaYarkon St 120, Tel Aviv-Yafo",
    "lat": 32.0853,
    "lon": 34.7738,
    "resolution_quality": "exact",
    "neighborhood": "Old North (HaTzafon HaYashan)",
    "candidates": [],
    "source": "mock_dataset",
    "limitations": []
  }
}
```

**Incomplete / no-result outcome** (`outcome="not_found"`):
```json
{
  "status": "ok",
  "data": {
    "outcome": "not_found",
    "normalized_address": null,
    "lat": null,
    "lon": null,
    "resolution_quality": null,
    "neighborhood": null,
    "candidates": [],
    "source": "mock_dataset",
    "limitations": [
      "MockGeoResolver has no known coordinates for address '1 Nowhere St, Atlantis'."
    ]
  }
}
```

## `nearby_services`

**Input:**
```json
{ "lat": 32.0853, "lon": 34.7738, "categories": ["school", "supermarket"], "radius_m": 500 }
```

**Successful outcome:**
```json
{
  "status": "ok",
  "data": {
    "places": [
      { "name": "Gordon Elementary", "category": "school", "distance_m": 210.0, "source": "mock_dataset" }
    ],
    "categories": [
      { "category": "school", "count": 1, "nearest_distance_m": 210.0 }
    ],
    "evidence_quality": "medium",
    "limitations": [
      "Distances are straight-line (as-the-crow-flies) estimates, not walking distances or walking times."
    ]
  }
}
```

**Incomplete / no-result outcome** (no coverage for this location):
```json
{
  "status": "ok",
  "data": {
    "places": [],
    "categories": [],
    "evidence_quality": "low",
    "limitations": [
      "Distances are straight-line (as-the-crow-flies) estimates, not walking distances or walking times."
    ]
  }
}
```

## `price_signal`

**Input:**
```json
{
  "normalized_address": "HaYarkon St 120, Tel Aviv-Yafo",
  "listing_type": "purchase",
  "asking_price": 3600000,
  "area_sqm": 90,
  "rooms": 3,
  "neighborhood": "Old North (HaTzafon HaYashan)"
}
```

**Successful outcome** (`outcome="compared"`):
```json
{
  "status": "ok",
  "data": {
    "outcome": "compared",
    "asking_price_per_sqm": 40000.0,
    "median_comparison_price_per_sqm": 40500.0,
    "reference_range_low_per_sqm": 38000.0,
    "reference_range_high_per_sqm": 42000.0,
    "deviation_percent": -1.23,
    "comparison_count": 5,
    "missing_fields": [],
    "source": "mock_dataset",
    "limitations": [
      "This is a data-driven price signal only, not a professional or certified appraisal.",
      "Comparison figures come from a small hardcoded mock transaction sample and may not reflect current market conditions."
    ]
  }
}
```

**Incomplete / no-result outcome** (`outcome="insufficient_data"` — missing `area_sqm`):
```json
{
  "status": "ok",
  "data": {
    "outcome": "insufficient_data",
    "asking_price_per_sqm": null,
    "median_comparison_price_per_sqm": null,
    "reference_range_low_per_sqm": null,
    "reference_range_high_per_sqm": null,
    "deviation_percent": null,
    "comparison_count": 0,
    "missing_fields": ["area_sqm"],
    "source": "mock_dataset",
    "limitations": [
      "price_signal could not compute a comparison: missing area_sqm.",
      "This is a data-driven price signal only, not a professional or certified appraisal."
    ]
  }
}
```

For a rental listing, `price_signal` returns `{"status": "skipped", "reason": "price_signal does not apply to rental listings and was skipped."}` instead of a `data` payload.
