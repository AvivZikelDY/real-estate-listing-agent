import pytest

from src.listing_agent.tools.mock_implementations import MockPriceSignal
from src.listing_agent.tools.schemas import PriceSignalInput

_ADDRESS = "1 Old North St, Tel Aviv"


@pytest.mark.asyncio
async def test_purchase_price_signal_returns_deterministic_comparison() -> None:
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Old North (HaTzafon HaYashan)",
            listing_type="purchase",
            asking_price=3_600_000,
            area_sqm=90,
        )
    )

    assert result.status == "ok"
    assert result.data.outcome == "compared"
    assert result.data.asking_price_per_sqm == pytest.approx(40_000)
    assert result.data.comparison_count == 5
    assert result.data.median_comparison_price_per_sqm > 0
    assert result.data.reference_range_low_per_sqm is not None
    assert result.data.reference_range_high_per_sqm is not None
    limitations_text = " ".join(result.data.limitations).lower()
    assert "not a professional" in limitations_text or "appraisal" in limitations_text


@pytest.mark.asyncio
async def test_price_signal_reports_insufficient_data_when_sqm_missing() -> None:
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Old North (HaTzafon HaYashan)",
            listing_type="purchase",
            asking_price=3_600_000,
            area_sqm=None,
        )
    )

    assert result.status == "ok"
    assert result.data.outcome == "insufficient_data"
    assert "area_sqm" in result.data.missing_fields
    assert result.error is None


@pytest.mark.asyncio
async def test_price_signal_reports_insufficient_data_when_asking_price_missing() -> None:
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Old North (HaTzafon HaYashan)",
            listing_type="purchase",
            asking_price=None,
            area_sqm=90,
        )
    )

    assert result.status == "ok"
    assert result.data.outcome == "insufficient_data"
    assert "asking_price" in result.data.missing_fields


@pytest.mark.asyncio
async def test_price_signal_reports_no_comparable_data_for_unknown_neighborhood() -> None:
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address="1 Nowhere St, Tel Aviv",
            neighborhood="Nowhereville",
            listing_type="purchase",
            asking_price=1_000_000,
            area_sqm=50,
        )
    )

    assert result.status == "ok"
    assert result.data.outcome == "no_comparable_data"
    assert result.data.comparison_count == 0


@pytest.mark.asyncio
async def test_rental_listing_is_skipped_not_evaluated() -> None:
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Old North (HaTzafon HaYashan)",
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
async def test_demo_listing_price_comparison_ramat_aviv() -> None:
    """The GUI demo listing (README.md "Try the GUI"):
    Derech HaTayasim 8, Ramat Aviv, 4,700,000 ILS / 111 sqm."""
    tool = MockPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address="Derech HaTayasim 8, Tel Aviv-Yafo",
            neighborhood="Ramat Aviv",
            listing_type="purchase",
            asking_price=4_700_000,
            area_sqm=111,
        )
    )

    assert result.status == "ok"
    assert result.data.outcome == "compared"
    assert result.data.asking_price_per_sqm == pytest.approx(4_700_000 / 111, rel=1e-6)
    assert result.data.deviation_percent is not None


@pytest.mark.asyncio
async def test_demo_listing_changed_price_gives_distinguishable_comparison() -> None:
    """A lower asking price (4.4M instead of 4.7M) for the same demo listing
    must produce a visibly different deviation, so the interactive demo can
    show turn 5's new tool call actually did something different."""
    tool = MockPriceSignal()

    higher = await tool.execute(
        PriceSignalInput(
            normalized_address="Derech HaTayasim 8, Tel Aviv-Yafo",
            neighborhood="Ramat Aviv",
            listing_type="purchase",
            asking_price=4_700_000,
            area_sqm=111,
        )
    )
    lower = await tool.execute(
        PriceSignalInput(
            normalized_address="Derech HaTayasim 8, Tel Aviv-Yafo",
            neighborhood="Ramat Aviv",
            listing_type="purchase",
            asking_price=4_400_000,
            area_sqm=111,
        )
    )

    assert higher.data.asking_price_per_sqm != lower.data.asking_price_per_sqm
    assert higher.data.deviation_percent != lower.data.deviation_percent
