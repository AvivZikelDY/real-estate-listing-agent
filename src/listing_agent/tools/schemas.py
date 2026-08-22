"""Pydantic v2 schemas shared by tool implementations.

These models formalize the tool contracts described in
`docs/contracts.md` §4 and `docs/tools.md`. They are used by the
`ToolRegistry`, by both mock implementations and the live
implementations, and by `evidence_collector`.

Design principle shared by all three tools: a tool's `ToolResult.status`
distinguishes *technical* outcomes (`"ok"` = the tool ran and produced a
well-formed answer; `"error"` = a genuine failure, e.g. an unhandled
upstream/data-source problem; `"skipped"` = the tool intentionally did
nothing for this input). A *business* outcome that is merely "no usable
answer" — an address that doesn't resolve, an area with zero nearby
services, insufficient listing data to compute a price comparison — is
still `status="ok"`, with the specifics captured in a typed `outcome`
field on the output model. This keeps `report_writer` able to tell "we
checked, and here's what we honestly found" apart from "the check itself
broke," without ever having to inspect a free-text `error` string to know
which case it is.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ToolResult[DataT: BaseModel](BaseModel):
    """The uniform envelope every tool's `execute()` returns.

    A tool must never raise an unhandled exception. Instead:
    - `status == "ok"` with `data` populated — the tool ran and produced a
      well-formed answer, including a "no usable answer" business outcome
      (see module docstring); the specifics live in `data`'s own typed
      outcome field, never inferred from `error`.
    - `status == "error"` with `error` set to a human-readable message —
      a genuine technical failure (e.g. an unhandled upstream error).
    - `status == "skipped"` with `reason` set when the tool intentionally
      does nothing for this input (e.g. `price_signal` on a rental
      listing).
    """

    status: Literal["ok", "error", "skipped"]
    data: DataT | None = None
    error: str | None = None
    reason: str | None = None


# --- geo_resolver ------------------------------------------------------------

GeoResolutionOutcome = Literal["resolved", "ambiguous", "not_found"]
GeoResolutionQuality = Literal["exact", "approximate", "low_confidence"]


class GeoResolverInput(BaseModel):
    """Input to `geo_resolver`."""

    address: str = Field(
        min_length=1,
        description="Raw address/location string as extracted by triage_planner.",
    )
    city_hint: str | None = Field(
        default=None,
        description="Optional city hint to help disambiguate the address (e.g. 'Tel Aviv').",
    )


class GeoResolverOutput(BaseModel):
    """Output of `geo_resolver`, wrapped in `ToolResult`.

    `outcome` is the typed business result:
    - `"resolved"`: a single, confident match was found. `normalized_address`,
      `lat`, `lon`, and `resolution_quality` are all populated.
    - `"ambiguous"`: more than one plausible match exists and none is
      confidently the "one" match. `candidates` lists the alternatives;
      `lat`/`lon`/`normalized_address` are not populated (no single answer
      to act on).
    - `"not_found"`: no match at all. `lat`/`lon`/`normalized_address` are
      not populated.
    """

    outcome: GeoResolutionOutcome
    normalized_address: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    resolution_quality: GeoResolutionQuality | None = None
    neighborhood: str | None = None
    candidates: list[str] = Field(default_factory=list)
    source: str
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolved_requires_coordinates(self) -> "GeoResolverOutput":
        if self.outcome == "resolved":
            missing = [
                name
                for name, value in (
                    ("normalized_address", self.normalized_address),
                    ("lat", self.lat),
                    ("lon", self.lon),
                    ("resolution_quality", self.resolution_quality),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"outcome='resolved' requires {', '.join(missing)} to be set."
                )
        return self


# --- nearby_services -----------------------------------------------------------

NearbyServiceCategory = Literal[
    "supermarket",
    "school",
    "park",
    "pharmacy",
    "daycare",
    "public_transport",
    "hospital",
]

_ALL_NEARBY_SERVICE_CATEGORIES: tuple[NearbyServiceCategory, ...] = (
    "supermarket",
    "school",
    "park",
    "pharmacy",
    "daycare",
    "public_transport",
    "hospital",
)


class ServiceCategorySummary(BaseModel):
    """Aggregated nearby-service evidence for a single category."""

    category: str
    count: int
    nearest_distance_m: float


class NearbyPlace(BaseModel):
    """A single nearby place found within the search radius."""

    name: str
    category: NearbyServiceCategory
    distance_m: float = Field(ge=0)
    source: str


class NearbyServicesInput(BaseModel):
    """Input to `nearby_services`."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    categories: list[NearbyServiceCategory] = Field(
        default_factory=lambda: list(_ALL_NEARBY_SERVICE_CATEGORIES),
        min_length=1,
        description="Which service categories to search for (at least one).",
    )
    radius_m: int = Field(default=500, gt=0)


class NearbyServicesOutput(BaseModel):
    """Output of `nearby_services`, wrapped in `ToolResult`.

    A location with zero matching places is still a successful, typed
    result (`places=[]`, `categories=[]`, `evidence_quality="low"`) —
    distinct from a technical failure, which is `ToolResult(status="error")`
    with no `data` at all.
    """

    places: list[NearbyPlace] = Field(default_factory=list)
    categories: list[ServiceCategorySummary] = Field(default_factory=list)
    evidence_quality: Literal["low", "medium", "high"]
    limitations: list[str] = Field(default_factory=list)


# --- price_signal --------------------------------------------------------------

PriceComparisonOutcome = Literal["compared", "insufficient_data", "no_comparable_data"]


class PriceSignalInput(BaseModel):
    """Input to `price_signal`.

    `neighborhood` is retained as an optional field: the current mock
    dataset (and possibly the live comparables source) key comparison
    data by neighborhood, typically derived from `geo_resolver`'s output
    alongside `normalized_address`. It is not part of the tool's required
    contract — only `normalized_address`, `asking_price`, and the optional
    area/room fields are.
    """

    normalized_address: str = Field(min_length=1)
    listing_type: Literal["purchase", "rental", "unknown"] = "unknown"
    asking_price: float | None = Field(default=None, gt=0)
    area_sqm: float | None = Field(default=None, gt=0)
    rooms: float | None = Field(default=None, gt=0)
    neighborhood: str | None = None


class PriceSignalOutput(BaseModel):
    """Output of `price_signal`, wrapped in `ToolResult`.

    `outcome` is the typed business result:
    - `"compared"`: a comparison was computed. `asking_price_per_sqm`,
      `median_comparison_price_per_sqm`, `deviation_percent`, and the
      reference range are all populated.
    - `"insufficient_data"`: the listing itself is missing a field needed
      to compute a comparison (`asking_price` and/or `area_sqm`).
      `missing_fields` lists exactly what is missing.
    - `"no_comparable_data"`: the listing has enough data, but no
      comparison transactions are available for this address/area.
    """

    outcome: PriceComparisonOutcome
    asking_price_per_sqm: float | None = None
    median_comparison_price_per_sqm: float | None = None
    reference_range_low_per_sqm: float | None = None
    reference_range_high_per_sqm: float | None = None
    deviation_percent: float | None = None
    comparison_count: int = 0
    missing_fields: list[str] = Field(default_factory=list)
    source: str
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _compared_requires_figures(self) -> "PriceSignalOutput":
        if self.outcome == "compared":
            missing = [
                name
                for name, value in (
                    ("asking_price_per_sqm", self.asking_price_per_sqm),
                    ("median_comparison_price_per_sqm", self.median_comparison_price_per_sqm),
                    ("deviation_percent", self.deviation_percent),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"outcome='compared' requires {', '.join(missing)} to be set.")
        return self
