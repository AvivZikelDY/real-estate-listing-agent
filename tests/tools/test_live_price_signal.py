"""Tests for `LivePriceSignal` — the govmap-backed real implementation.

Covers requirements 6-11 of `docs/tools.md` ("required tests").

`price_signal` is live-only: every test injects a stub `GovmapClient`, so
nothing here touches the network, but there is no offline snapshot to fall
back to anymore. A stub that raises therefore means a genuine technical
failure (`status="error"`), not a degraded-but-successful answer.
"""

import pytest

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.live_implementations.govmap_client import ComparableDeal
from src.listing_agent.tools.live_implementations.price_signal import LivePriceSignal
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import PriceSignalInput

_ADDRESS = "רוטשילד 30 תל אביב"


class _DeadGovmap:
    """A govmap client that always fails to reach the network.

    With no fallback left, this is used to prove technical failures become
    `status="error"` and never escape as a raw exception.
    """

    async def __aenter__(self) -> "_DeadGovmap":
        raise ConnectionError("govmap is unreachable (test stub)")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _LiveGovmap:
    """A govmap client returning canned deals, with no network access."""

    deals: list[ComparableDeal] = []

    async def __aenter__(self) -> "_LiveGovmap":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def find_comparable_deals(self, address: str) -> list[ComparableDeal]:
        type(self).last_address = address
        return self.deals


def _live(deals: list[ComparableDeal]) -> type[_LiveGovmap]:
    """Build a fresh live-stub class so tests never share state."""
    return type("_Live", (_LiveGovmap,), {"deals": deals, "last_address": None})


def _comparable(price: float, area: float, date: str = "2026-01-15T00:00:00.000Z"):
    return ComparableDeal(price=price, area_sqm=area, rooms=3, deal_date=date, street="רוטשילד")


# --- Requirement 6: a valid purchase listing produces a real comparison ----


@pytest.mark.asyncio
async def test_purchase_listing_returns_compared_with_correct_math() -> None:
    """The arithmetic is asserted exactly, not just "a number came back"."""
    # Five deals, all 100 sqm, priced so price-per-sqm is exactly
    # 40k, 45k, 50k, 55k, 60k. Median is therefore exactly 50,000.
    deals = [_comparable(p * 100, 100) for p in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = LivePriceSignal(client_factory=_live(deals))

    # Listing: 5,500,000 for 100 sqm -> 55,000/sqm, i.e. 10% above the median.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_500_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"
    assert result.data.asking_price_per_sqm == pytest.approx(55_000)
    assert result.data.median_comparison_price_per_sqm == pytest.approx(50_000)
    assert result.data.deviation_percent == pytest.approx(10.0)
    assert result.data.comparison_count == 5
    # n=5, so percentiles land on the extremes.
    assert result.data.reference_range_low_per_sqm == pytest.approx(40_000)
    assert result.data.reference_range_high_per_sqm == pytest.approx(60_000)
    assert result.error is None


@pytest.mark.asyncio
async def test_compared_result_always_discloses_it_is_not_an_appraisal() -> None:
    """The appraisal disclaimer is mandatory on every result (handoff:167)."""
    deals = [_comparable(p * 100, 100) for p in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = LivePriceSignal(client_factory=_live(deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_500_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    limitations_text = " ".join(result.data.limitations).lower()
    assert "not a professional or certified appraisal" in limitations_text


@pytest.mark.asyncio
async def test_underpriced_listing_reports_negative_deviation() -> None:
    """A listing below the local median must read as a negative deviation."""
    deals = [_comparable(p * 100, 100) for p in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = LivePriceSignal(client_factory=_live(deals))

    # 4,000,000 for 100 sqm -> 40,000/sqm, 20% below the 50,000 median.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=4_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.deviation_percent == pytest.approx(-20.0)


# --- The live path, with no fallback ----------------------------------------


@pytest.mark.asyncio
async def test_live_data_is_used() -> None:
    deals = [_comparable(p * 100, 100) for p in (60_000, 62_000, 64_000, 66_000, 68_000)]
    tool = LivePriceSignal(client_factory=_live(deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=6_400_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"
    assert result.data.median_comparison_price_per_sqm == pytest.approx(64_000)
    assert "live" in result.data.source


@pytest.mark.asyncio
async def test_live_lookup_receives_the_normalized_address() -> None:
    """The client locates the address's own parcel, so it needs the address."""
    stub = _live([_comparable(p * 100, 100) for p in (60_000, 62_000, 64_000)])
    tool = LivePriceSignal(client_factory=stub)

    await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=6_400_000,
            area_sqm=100,
        )
    )

    assert stub.last_address == _ADDRESS


@pytest.mark.asyncio
async def test_unreachable_govmap_returns_error_not_a_degraded_answer() -> None:
    """>>> No fallback left. <<< A technical failure is a genuine error."""
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.status == "error"
    assert result.data is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_unexpected_exception_from_client_returns_error_not_exception() -> None:
    """Any client failure, not just connection errors, becomes `status="error"`."""

    class _Exploding(_LiveGovmap):
        async def find_comparable_deals(self, address: str) -> list[ComparableDeal]:
            raise RuntimeError("boom")

    tool = LivePriceSignal(client_factory=_Exploding)

    # The assertion is as much that this line does not raise as anything else.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.status == "error"
    assert result.error is not None


@pytest.mark.asyncio
async def test_empty_live_result_reports_no_comparable_data() -> None:
    """govmap answering "no recorded sales here" is a real, successful answer."""
    tool = LivePriceSignal(client_factory=_live([]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "no_comparable_data"
    assert result.data.comparison_count == 0


@pytest.mark.asyncio
async def test_rental_is_skipped_before_any_network_call() -> None:
    """The rental check must short-circuit before any data source is touched."""
    stub = _live([_comparable(5_000_000, 100)])
    tool = LivePriceSignal(client_factory=stub)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="rental",
            asking_price=6_500,
            area_sqm=55,
        )
    )

    assert result.status == "skipped"
    assert stub.last_address is None


# --- Requirement 7: missing listing fields ---------------------------------


@pytest.mark.asyncio
async def test_missing_area_reports_insufficient_data() -> None:
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=3_600_000,
            area_sqm=None,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "insufficient_data"
    assert result.data.missing_fields == ["area_sqm"]
    assert result.error is None


@pytest.mark.asyncio
async def test_missing_asking_price_reports_insufficient_data() -> None:
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=None,
            area_sqm=90,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "insufficient_data"
    assert result.data.missing_fields == ["asking_price"]


@pytest.mark.asyncio
async def test_both_fields_missing_lists_both() -> None:
    """`missing_fields` must list exactly what is missing — both, here."""
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=None,
            area_sqm=None,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "insufficient_data"
    assert result.data.missing_fields == ["asking_price", "area_sqm"]


# --- Requirement 8: no comparable data -------------------------------------


@pytest.mark.asyncio
async def test_too_few_comparables_reports_no_comparable_data() -> None:
    """A median over 2 sales is not a signal, so it must not be published."""
    deals = [_comparable(5_000_000, 100), _comparable(5_100_000, 100)]
    tool = LivePriceSignal(client_factory=_live(deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "no_comparable_data"


@pytest.mark.asyncio
async def test_implausible_price_per_sqm_rows_are_discarded() -> None:
    """Corrupt rows must be dropped, not allowed to distort the median."""
    # Three sane deals plus two absurd ones (1 sqm mis-keys). If the absurd
    # rows survived, the median would be wildly different.
    deals = [_comparable(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    deals += [_comparable(5_000_000, 1), _comparable(100, 100)]
    tool = LivePriceSignal(client_factory=_live(deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "compared"
    assert result.data.comparison_count == 3
    assert result.data.median_comparison_price_per_sqm == pytest.approx(52_000)


@pytest.mark.asyncio
async def test_stale_deals_are_excluded_from_the_comparison() -> None:
    """Old sales must not drag the median (the Neve Tzedek 2019 problem)."""
    recent = [_comparable(p * 100, 100, "2026-03-01T00:00:00.000Z") for p in [60_000] * 10]
    stale = [_comparable(p * 100, 100, "2019-05-01T00:00:00.000Z") for p in [10_000] * 10]
    tool = LivePriceSignal(client_factory=_live(recent + stale))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=6_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    # Only the 10 recent deals survive, so the median is 60,000 — not the
    # 35,000 you would get by averaging both eras together.
    assert result.data.comparison_count == 10
    assert result.data.median_comparison_price_per_sqm == pytest.approx(60_000)
    assert any("older than" in limitation for limitation in result.data.limitations)


# --- Requirement 9: rentals are skipped ------------------------------------


@pytest.mark.asyncio
async def test_rental_listing_is_skipped() -> None:
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="rental",
            asking_price=6_500,
            area_sqm=55,
        )
    )

    assert result.status == "skipped"
    assert result.data is None
    assert result.error is None
    assert result.reason is not None


@pytest.mark.asyncio
async def test_rental_is_skipped_even_when_everything_else_is_missing() -> None:
    """Skipping must win over the insufficient-data check, not the reverse."""
    tool = LivePriceSignal(client_factory=_DeadGovmap)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="rental",
            asking_price=None,
            area_sqm=None,
        )
    )

    assert result.status == "skipped"
    assert result.data is None


# --- Requirement 11: interface conformance ---------------------------------


def test_tool_name_matches_the_whitelisted_name() -> None:
    assert LivePriceSignal().name == "price_signal"


def test_tool_satisfies_the_base_tool_protocol() -> None:
    assert isinstance(LivePriceSignal(), BaseTool)


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry() -> None:
    """Registration and execution must work through the real `ToolRegistry`."""
    deals = [_comparable(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    registry = ToolRegistry()
    registry.register(LivePriceSignal(client_factory=_live(deals)))

    result = await registry.execute(
        "price_signal",
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        ),
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"


# --- Size-band narrowing ---------------------------------------------------


@pytest.mark.asyncio
async def test_similar_sized_comparables_are_preferred() -> None:
    """With enough size-matched sales, only those are used."""
    # 6 deals at ~100 sqm and 50,000/sqm, plus 6 penthouses at 300 sqm and
    # 90,000/sqm. A 100 sqm listing should be judged against the former.
    small = [_comparable(50_000 * 100, 100 + i) for i in range(6)]
    large = [_comparable(90_000 * 300, 300 + i) for i in range(6)]
    tool = LivePriceSignal(client_factory=_live(small + large))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.comparison_count == 6
    # ~50,000, not the ~70,000 you would get by including the penthouses.
    assert result.data.median_comparison_price_per_sqm == pytest.approx(49_600, rel=0.02)
    assert any("within 25%" in limitation for limitation in result.data.limitations)


@pytest.mark.asyncio
async def test_falls_back_to_all_sizes_when_band_is_too_thin() -> None:
    """Too few size matches -> widen, and say so rather than silently narrow."""
    # Only 2 deals near 100 sqm; the rest are much larger.
    small = [_comparable(50_000 * 100, 100), _comparable(50_000 * 100, 105)]
    large = [_comparable(90_000 * 300, 300 + i) for i in range(6)]
    tool = LivePriceSignal(client_factory=_live(small + large))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.comparison_count == 8
    assert any("Too few similarly sized" in limitation for limitation in result.data.limitations)
