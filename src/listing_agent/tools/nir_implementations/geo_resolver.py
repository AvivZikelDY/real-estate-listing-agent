"""Placeholder for Nir's real `geo_resolver` implementation.

Per `docs/nir_handoff.md`: Nir owns this file's real logic and data source.
This placeholder always raises `NotImplementedError` and must never be
selected while `TOOLS_MODE=mock` (see `src/listing_agent/tools/dependency.py`).
"""

from src.listing_agent.tools.schemas import GeoResolverInput, GeoResolverOutput, ToolResult


class NirGeoResolver:
    """TODO(Nir): implement the real geo_resolver.

    Expected input (`GeoResolverInput`):
        address: str — a raw Tel Aviv address/location string as extracted
            by `triage_planner` (may be informally formatted, e.g. missing
            a house number or city suffix).
        city_hint: str | None — optional city hint to help disambiguate.

    Expected output (`ToolResult[GeoResolverOutput]`):
        On success, always `ToolResult(status="ok", data=GeoResolverOutput(...))`
        with `outcome` set to exactly one of:
        - "resolved": a single confident match. `normalized_address`, `lat`,
          `lon`, and `resolution_quality` must all be set.
        - "ambiguous": more than one plausible match. `candidates` should
          list the alternatives; leave `lat`/`lon`/`normalized_address`
          unset.
        - "not_found": no match at all.
        A business "no answer" result (ambiguous/not_found) is still
        `status="ok"` — it is not a technical failure.
        On a genuine technical failure (e.g. the upstream data source is
        unreachable), return `ToolResult(status="error", error=<message>)`
        — never raise from `execute()` once implemented.

    Do not call an LLM or use a database. See `docs/nir_handoff.md` for the
    full contract and required tests.
    """

    name = "geo_resolver"

    async def execute(self, input_model: GeoResolverInput) -> ToolResult[GeoResolverOutput]:
        raise NotImplementedError(
            "NirGeoResolver is a placeholder. TODO(Nir): implement the real "
            "geo_resolver against a real geocoding data source for Tel Aviv "
            "addresses. See docs/nir_handoff.md for the exact input/output "
            "contract and required tests. This class must never be selected "
            "while TOOLS_MODE=mock."
        )
