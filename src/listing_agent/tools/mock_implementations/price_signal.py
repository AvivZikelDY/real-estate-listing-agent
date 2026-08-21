"""Deterministic mock `price_signal` implementation.

Uses a small hardcoded purchase transaction dataset (ILS per sqm), keyed
by normalized neighborhood name (typically derived from `geo_resolver`'s
output alongside `normalized_address`). Never a professional or certified
appraisal. Rental listings are always skipped — this tool only speaks to
purchase pricing.
"""

import statistics

from src.listing_agent.tools.schemas import PriceSignalInput, PriceSignalOutput, ToolResult

_SOURCE = "mock_dataset"

_APPRAISAL_LIMITATION = (
    "This is a data-driven price signal only, not a professional or certified appraisal."
)
_DATASET_LIMITATION = (
    "Comparison figures come from a small hardcoded mock transaction sample and may not "
    "reflect current market conditions."
)

# A small hardcoded dataset of recent purchase transactions (ILS per sqm),
# keyed by normalized neighborhood name.
_PURCHASE_TRANSACTIONS_ILS_PER_SQM: dict[str, list[float]] = {
    "old north (hatzafon hayashan)": [38_000, 40_500, 42_000, 39_000, 41_000],
    "florentin": [27_000, 28_500, 26_000, 29_000],
    "ramat aviv": [33_500, 35_000, 34_000, 36_000, 34_500],
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


class MockPriceSignal:
    """Deterministic price_signal mock backed by a small local dataset."""

    name = "price_signal"

    async def execute(self, input_model: PriceSignalInput) -> ToolResult[PriceSignalOutput]:
        if input_model.listing_type == "rental":
            return ToolResult(
                status="skipped",
                reason="price_signal does not apply to rental listings and was skipped.",
            )

        missing_fields = [
            field_name
            for field_name, value in (
                ("asking_price", input_model.asking_price),
                ("area_sqm", input_model.area_sqm),
            )
            if value is None
        ]
        if missing_fields:
            return ToolResult(
                status="ok",
                data=PriceSignalOutput(
                    outcome="insufficient_data",
                    missing_fields=missing_fields,
                    source=_SOURCE,
                    limitations=[
                        "price_signal could not compute a comparison: missing "
                        f"{', '.join(missing_fields)}.",
                        _APPRAISAL_LIMITATION,
                    ],
                ),
            )

        lookup_key = input_model.neighborhood or input_model.normalized_address
        comparisons = _PURCHASE_TRANSACTIONS_ILS_PER_SQM.get(_normalize(lookup_key))
        if not comparisons:
            return ToolResult(
                status="ok",
                data=PriceSignalOutput(
                    outcome="no_comparable_data",
                    source=_SOURCE,
                    limitations=[
                        f"No comparison transactions available for '{lookup_key}'.",
                        _APPRAISAL_LIMITATION,
                    ],
                ),
            )

        asking_price_per_sqm = input_model.asking_price / input_model.area_sqm
        median = statistics.median(comparisons)
        deviation_percent = ((asking_price_per_sqm - median) / median) * 100

        return ToolResult(
            status="ok",
            data=PriceSignalOutput(
                outcome="compared",
                asking_price_per_sqm=round(asking_price_per_sqm, 2),
                median_comparison_price_per_sqm=round(median, 2),
                reference_range_low_per_sqm=round(min(comparisons), 2),
                reference_range_high_per_sqm=round(max(comparisons), 2),
                deviation_percent=round(deviation_percent, 2),
                comparison_count=len(comparisons),
                source=_SOURCE,
                limitations=[_APPRAISAL_LIMITATION, _DATASET_LIMITATION],
            ),
        )
