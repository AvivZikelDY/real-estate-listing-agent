"""Placeholder for Nir's real `nearby_services` implementation.

Per `docs/nir_handoff.md`: Nir owns this file's real logic and data source.
This placeholder always raises `NotImplementedError` and must never be
selected while `TOOLS_MODE=mock` (see `src/listing_agent/tools/dependency.py`).
"""

from src.listing_agent.tools.schemas import (
    NearbyServicesInput,
    NearbyServicesOutput,
    ToolResult,
)


class NirNearbyServices:
    """TODO(Nir): implement the real nearby_services lookup.

    Expected input (`NearbyServicesInput`):
        lat: float, lon: float — resolved coordinates (typically from
            `geo_resolver`'s output).
        categories: list of the service categories to search for (at least
            one; see `NearbyServiceCategory` in `tools/schemas.py`).
        radius_m: int — search radius in meters (default 500).

    Expected output (`ToolResult[NearbyServicesOutput]`):
        On success: `ToolResult(status="ok", data=NearbyServicesOutput(
        places=[NearbyPlace(name, category, distance_m, source), ...],
        categories=[ServiceCategorySummary(category, count,
        nearest_distance_m), ...], evidence_quality="low"|"medium"|"high",
        limitations=[...]))`.
        `limitations` must always disclose that distances are straight-line
        estimates, not walking distances or walking times, plus any data
        source caveats.
        A location with zero matching places is still `status="ok"` with
        `places=[]`/`categories=[]` — that is not an error.
        On a genuine technical failure (e.g. upstream data source
        unavailable): return `ToolResult(status="error", error=<message>)`
        — never raise from `execute()` once implemented.

    Do not call an LLM or use a database. See `docs/nir_handoff.md` for the
    full contract and required tests.
    """

    name = "nearby_services"

    async def execute(
        self, input_model: NearbyServicesInput
    ) -> ToolResult[NearbyServicesOutput]:
        raise NotImplementedError(
            "NirNearbyServices is a placeholder. TODO(Nir): implement the "
            "real nearby_services lookup against a real Tel Aviv "
            "points-of-interest data source. See docs/nir_handoff.md for "
            "the exact input/output contract and required tests. This "
            "class must never be selected while TOOLS_MODE=mock."
        )
