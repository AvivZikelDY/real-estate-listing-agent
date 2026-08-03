"""Placeholder for Nir's real `price_signal` implementation.

Per `docs/nir_handoff.md`: Nir owns this file's real logic and data source.
This placeholder always raises `NotImplementedError` and must never be
selected while `TOOLS_MODE=mock` (see `src/listing_agent/tools/dependency.py`).
"""

from src.listing_agent.tools.schemas import PriceSignalInput, PriceSignalOutput, ToolResult


class NirPriceSignal:
    """TODO(Nir): implement the real price_signal comparison.

    Expected input (`PriceSignalInput`):
        normalized_address: str, listing_type: "purchase" | "rental" |
        "unknown", asking_price: float | None, area_sqm: float | None,
        rooms: float | None, neighborhood: str | None (optional, typically
        from `geo_resolver`'s output; may be used as a comparables lookup
        key or ignored in favor of `normalized_address`).

    Expected output (`ToolResult[PriceSignalOutput]`):
        On success, always `ToolResult(status="ok", data=PriceSignalOutput(...))`
        with `outcome` set to exactly one of:
        - "compared": `asking_price_per_sqm`,
          `median_comparison_price_per_sqm`, `deviation_percent`, and the
          reference range must all be set.
        - "insufficient_data": the listing itself is missing `asking_price`
          and/or `area_sqm`. `missing_fields` must list exactly what is
          missing.
        - "no_comparable_data": enough listing data exists, but no
          comparison transactions are available for this address/area.
        A business "no comparison possible" result (insufficient_data /
        no_comparable_data) is still `status="ok"` — it is not a technical
        failure.
        `limitations` must always disclose that this is not a professional
        or certified appraisal, plus any data source caveats.
        Rental listings must be skipped: return
        `ToolResult(status="skipped", reason=<message>)`.
        On a genuine technical failure, return
        `ToolResult(status="error", error=<message>)` — never raise
        from `execute()` once implemented.

    Do not call an LLM or use a database. See `docs/nir_handoff.md` for the
    full contract and required tests.
    """

    name = "price_signal"

    async def execute(self, input_model: PriceSignalInput) -> ToolResult[PriceSignalOutput]:
        raise NotImplementedError(
            "NirPriceSignal is a placeholder. TODO(Nir): implement the real "
            "price_signal comparison against a real Tel Aviv purchase "
            "transaction data source. See docs/nir_handoff.md for the "
            "exact input/output contract and required tests. This class "
            "must never be selected while TOOLS_MODE=mock."
        )
