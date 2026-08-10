"""Tests for `NirPriceSignal` — the govmap-backed real implementation.

Covers requirements 6-11 of `docs/nir_handoff.md` ("Tests Nir must satisfy").

Every test builds its own tiny snapshot fixture on disk rather than reading
the committed `comparables_snapshot.json`. That keeps assertions exact (the
expected median is hand-computed, not "> 0") and means regenerating the
real snapshot can never break the suite.
"""

import json
from pathlib import Path

import pytest

from src.listing_agent.tools.base import BaseTool
from src.listing_agent.tools.nir_implementations.price_signal import (
    DEFAULT_SNAPSHOT_PATH,
    NirPriceSignal,
)
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import PriceSignalInput

_ADDRESS = "רוטשילד 30 תל אביב"


def _write_snapshot(tmp_path: Path, deals: list[dict], aliases: list[str] | None = None) -> Path:
    """Write a one-neighbourhood snapshot fixture and return its path.

    Mirrors the real file's structure exactly, so these tests exercise the
    same loading and alias-matching code the committed snapshot goes
    through.
    """
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-10",
                "source": "test fixture",
                "deal_type": 2,
                "neighborhoods": {
                    "lev hair": {
                        "display_name": "Lev HaIr",
                        "aliases": aliases if aliases is not None else ["lev hair", "לב העיר"],
                        "deals": deals,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _deal(price: float, area: float, date: str = "2026-01-15T00:00:00.000Z") -> dict:
    """One snapshot row. Defaults keep every fixture deal recent."""
    return {"price": price, "area_sqm": area, "rooms": 3, "deal_date": date, "street": "רוטשילד"}


# --- Requirement 6: a valid purchase listing produces a real comparison ----


@pytest.mark.asyncio
async def test_purchase_listing_returns_compared_with_correct_math(tmp_path: Path) -> None:
    """The arithmetic is asserted exactly, not just "a number came back"."""
    # Five deals, all 100 sqm, priced so price-per-sqm is exactly
    # 40k, 45k, 50k, 55k, 60k. Median is therefore exactly 50,000.
    deals = [_deal(price * 100, 100) for price in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    # Listing: 5,500,000 for 100 sqm -> 55,000/sqm, i.e. 10% above the median.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_compared_result_always_discloses_it_is_not_an_appraisal(tmp_path: Path) -> None:
    """The appraisal disclaimer is mandatory on every result (handoff:167)."""
    deals = [_deal(p * 100, 100) for p in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_500_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    limitations_text = " ".join(result.data.limitations).lower()
    assert "not a professional or certified appraisal" in limitations_text
    # And that the figures are a point-in-time extract, not live data.
    assert "point-in-time" in limitations_text


@pytest.mark.asyncio
async def test_underpriced_listing_reports_negative_deviation(tmp_path: Path) -> None:
    """A listing below the local median must read as a negative deviation."""
    deals = [_deal(p * 100, 100) for p in (40_000, 45_000, 50_000, 55_000, 60_000)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    # 4,000,000 for 100 sqm -> 40,000/sqm, 20% below the 50,000 median.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=4_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.deviation_percent == pytest.approx(-20.0)


# --- Requirement 7: missing listing fields ---------------------------------


@pytest.mark.asyncio
async def test_missing_area_reports_insufficient_data(tmp_path: Path) -> None:
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_missing_asking_price_reports_insufficient_data(tmp_path: Path) -> None:
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=None,
            area_sqm=90,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "insufficient_data"
    assert result.data.missing_fields == ["asking_price"]


@pytest.mark.asyncio
async def test_both_fields_missing_lists_both(tmp_path: Path) -> None:
    """`missing_fields` must list exactly what is missing — both, here."""
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_unknown_neighborhood_reports_no_comparable_data(tmp_path: Path) -> None:
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address="1 Nowhere St, Reykjavik",
            neighborhood="Nowhereville",
            listing_type="purchase",
            asking_price=1_000_000,
            area_sqm=50,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "no_comparable_data"
    assert result.data.comparison_count == 0
    assert result.error is None


@pytest.mark.asyncio
async def test_too_few_comparables_reports_no_comparable_data(tmp_path: Path) -> None:
    """A median over 2 sales is not a signal, so it must not be published."""
    tool = NirPriceSignal(
        snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100), _deal(5_100_000, 100)])
    )

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "no_comparable_data"


@pytest.mark.asyncio
async def test_implausible_price_per_sqm_rows_are_discarded(tmp_path: Path) -> None:
    """Corrupt rows must be dropped, not allowed to distort the median."""
    # Three sane deals plus two absurd ones (1 sqm mis-keys). If the absurd
    # rows survived, the median would be wildly different.
    deals = [_deal(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    deals += [_deal(5_000_000, 1), _deal(100, 100)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_stale_deals_are_excluded_from_the_comparison(tmp_path: Path) -> None:
    """Old sales must not drag the median (the Neve Tzedek 2019 problem)."""
    recent = [_deal(p * 100, 100, "2026-03-01T00:00:00.000Z") for p in [60_000] * 10]
    stale = [_deal(p * 100, 100, "2019-05-01T00:00:00.000Z") for p in [10_000] * 10]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, recent + stale))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_rental_listing_is_skipped(tmp_path: Path) -> None:
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_rental_is_skipped_even_when_everything_else_is_missing(tmp_path: Path) -> None:
    """Skipping must win over the insufficient-data check, not the reverse."""
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, [_deal(5_000_000, 100)]))

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


@pytest.mark.asyncio
async def test_rental_is_skipped_even_when_the_snapshot_is_unreadable(tmp_path: Path) -> None:
    """The rental check must not depend on the data source being healthy."""
    tool = NirPriceSignal(snapshot_path=tmp_path / "does_not_exist.json")

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            listing_type="rental",
            asking_price=6_500,
            area_sqm=55,
        )
    )

    assert result.status == "skipped"


# --- Requirement 10: internal failures never escape ------------------------


@pytest.mark.asyncio
async def test_missing_snapshot_file_returns_error_not_exception(tmp_path: Path) -> None:
    """A missing data file is a genuine technical failure -> status='error'.

    Critically it must NOT be reported as `no_comparable_data`, which would
    tell the reader we checked and found nothing when we never checked.
    """
    tool = NirPriceSignal(snapshot_path=tmp_path / "does_not_exist.json")

    # The assertion is as much that this line does not raise as anything else.
    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.status == "error"
    assert result.data is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_corrupt_snapshot_file_returns_error_not_exception(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text("{ this is not valid json ::::", encoding="utf-8")
    tool = NirPriceSignal(snapshot_path=path)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.status == "error"
    assert result.error is not None


@pytest.mark.asyncio
async def test_snapshot_missing_neighborhoods_key_returns_error(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"generated_at": "2026-08-10"}), encoding="utf-8")
    tool = NirPriceSignal(snapshot_path=path)

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.status == "error"


@pytest.mark.asyncio
async def test_malformed_deal_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    """One bad record in the file must not take down the whole lookup."""
    deals: list[dict] = [_deal(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    deals.append({"price": "not a number", "area_sqm": None})  # garbage row
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"
    assert result.data.comparison_count == 3


# --- Requirement 11: interface conformance ---------------------------------


def test_tool_name_matches_the_whitelisted_name() -> None:
    assert NirPriceSignal().name == "price_signal"


def test_tool_satisfies_the_base_tool_protocol() -> None:
    assert isinstance(NirPriceSignal(), BaseTool)


@pytest.mark.asyncio
async def test_tool_executes_through_the_registry(tmp_path: Path) -> None:
    """Registration and execution must work through the real `ToolRegistry`."""
    deals = [_deal(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    registry = ToolRegistry()
    registry.register(NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals)))

    result = await registry.execute(
        "price_signal",
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        ),
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"


# --- Lookup-key resolution -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "neighborhood",
    [
        "Lev HaIr",  # English, as the mock geo_resolver emits
        "lev hair",  # already normalised
        "לב העיר",  # Hebrew, as govmap and Nir's real geo_resolver return
        "  LEV   HAIR  ",  # sloppy whitespace and casing
    ],
)
async def test_neighborhood_aliases_all_resolve(tmp_path: Path, neighborhood: str) -> None:
    """Hebrew and English keys must reach the same data.

    This is the real-world mismatch: govmap returns Hebrew names, while the
    existing mock `geo_resolver` emits English ones. `price_signal` has to
    work with whichever resolver is active.
    """
    deals = [_deal(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood=neighborhood,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "compared"


@pytest.mark.asyncio
async def test_falls_back_to_address_when_no_neighborhood_given(tmp_path: Path) -> None:
    """With no neighbourhood, the address is matched against the aliases."""
    deals = [_deal(p * 100, 100) for p in (50_000, 52_000, 54_000)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, deals))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address="Rothschild 30, Lev HaIr, Tel Aviv",
            neighborhood=None,
            listing_type="purchase",
            asking_price=5_200_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.outcome == "compared"


# --- Size-band narrowing ---------------------------------------------------


@pytest.mark.asyncio
async def test_similar_sized_comparables_are_preferred(tmp_path: Path) -> None:
    """With enough size-matched sales, only those are used."""
    # 6 deals at ~100 sqm and 50,000/sqm, plus 6 penthouses at 300 sqm and
    # 90,000/sqm. A 100 sqm listing should be judged against the former.
    small = [_deal(50_000 * 100, 100 + i) for i in range(6)]
    large = [_deal(90_000 * 300, 300 + i) for i in range(6)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, small + large))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
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
async def test_falls_back_to_all_sizes_when_band_is_too_thin(tmp_path: Path) -> None:
    """Too few size matches -> widen, and say so rather than silently narrow."""
    # Only 2 deals near 100 sqm; the rest are much larger.
    small = [_deal(50_000 * 100, 100), _deal(50_000 * 100, 105)]
    large = [_deal(90_000 * 300, 300 + i) for i in range(6)]
    tool = NirPriceSignal(snapshot_path=_write_snapshot(tmp_path, small + large))

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="Lev HaIr",
            listing_type="purchase",
            asking_price=5_000_000,
            area_sqm=100,
        )
    )

    assert result.data is not None
    assert result.data.comparison_count == 8
    assert any("Too few similarly sized" in limitation for limitation in result.data.limitations)


# --- The committed snapshot itself -----------------------------------------


def test_every_committed_bucket_holds_exactly_one_neighborhood() -> None:
    """Regression test for silent neighbourhood contamination.

    A govmap search radius straddles neighbourhood boundaries, so building
    a bucket from a seed address and trusting the seed's label mixes two
    markets into one median. That really happened: the Kerem HaTeimanim
    bucket came out 53% לב העיר, and Florentin's median was inflated by
    about a third (~38,700 -> ~52,200 ILS/sqm).

    `build_snapshot.py` now groups by the neighbourhood each deal actually
    reports. This test pins that invariant on the committed file, since the
    failure is invisible from the outside — the numbers just quietly drift.
    """
    snapshot = json.loads(DEFAULT_SNAPSHOT_PATH.read_text(encoding="utf-8"))

    for key, entry in snapshot["neighborhoods"].items():
        names = {d["neighborhood"] for d in entry["deals"]}
        assert len(names) == 1, f"bucket '{key}' mixes neighbourhoods: {sorted(names)}"
        # And the bucket's declared display name must be the one in the data.
        assert names.pop() == entry["display_name"]


def test_every_committed_bucket_is_reachable_by_its_own_display_name() -> None:
    """The Hebrew name the API reports must always resolve to its bucket.

    This is what `geo_resolver` will hand us once it is real, so a bucket
    that cannot be found by its own name is dead weight.
    """
    snapshot = json.loads(DEFAULT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    tool = NirPriceSignal()

    for key, entry in snapshot["neighborhoods"].items():
        found = tool._snapshot.lookup(entry["display_name"])
        assert found, f"bucket '{key}' is unreachable by its display name"


@pytest.mark.asyncio
async def test_default_committed_snapshot_produces_a_real_comparison() -> None:
    """Smoke test against the real committed data, with no fixture.

    Deliberately loose on the exact numbers (the snapshot is regenerated
    periodically) but strict that a well-formed listing in a covered
    neighbourhood yields a genuine comparison.
    """
    tool = NirPriceSignal()

    result = await tool.execute(
        PriceSignalInput(
            normalized_address=_ADDRESS,
            neighborhood="לב העיר",
            listing_type="purchase",
            asking_price=6_500_000,
            area_sqm=100,
        )
    )

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.outcome == "compared"
    assert result.data.comparison_count > 0
    assert result.data.median_comparison_price_per_sqm is not None
    # Tel Aviv centre realistically trades well above 20,000 ILS/sqm; this
    # would catch a units error or a corrupted regeneration.
    assert result.data.median_comparison_price_per_sqm > 20_000
