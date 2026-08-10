"""Nir's real `price_signal` implementation, backed by govmap deal data.

WHAT THIS TOOL DOES
-------------------
Given a listing's asking price and floor area, it compares the implied
price per square metre against real, recorded second-hand transactions in
the same Tel Aviv neighbourhood, and reports how far above or below the
local median the listing sits.

It is a *signal*, never a valuation — see `_APPRAISAL_LIMITATION`.

WHERE THE DATA COMES FROM
-------------------------
Real transactions published by the Israeli government via the govmap API.
Right now they are read from a pre-fetched snapshot committed next to this
file rather than fetched live; the reason, and the one-line switch to go
live, are marked `SWAP:` inside `execute()`.

THE RESULT CONTRACT (docs/nir_handoff.md)
-----------------------------------------
`ToolResult.status` describes whether the *tool* worked; the typed
`outcome` field describes what it *found*. They are independent, and
conflating them is the most common way to get this wrong:

    status="skipped"  -> rental listing; this tool only speaks to purchases
    status="ok"  + outcome="compared"           -> a real comparison
    status="ok"  + outcome="insufficient_data"  -> the listing lacks price/area
    status="ok"  + outcome="no_comparable_data" -> we have no sales for this area
    status="error"                              -> the tool itself broke

"We looked and found nothing useful" is a successful run (`status="ok"`),
not an error. Only a genuine technical failure is `status="error"`.
"""

import json
import logging
import statistics
from pathlib import Path
from typing import Any

from src.listing_agent.tools.nir_implementations.govmap_client import (
    ComparableDeal,
    # Unused until the SWAP block in `execute()` is uncommented. Kept
    # imported so going live is a one-line change and so the live client
    # stays visibly part of this tool's design.
    GovmapClient,  # noqa: F401
)
from src.listing_agent.tools.schemas import PriceSignalInput, PriceSignalOutput, ToolResult

logger = logging.getLogger(__name__)

# Default location of the committed snapshot. Tests inject their own path
# instead, so they never depend on the real file's contents.
DEFAULT_SNAPSHOT_PATH = Path(__file__).parent / "comparables_snapshot.json"

_SOURCE = "govmap.gov.il (Israeli government real-estate transactions)"

# Required by the contract on every single result, whatever the outcome.
_APPRAISAL_LIMITATION = (
    "This is a data-driven price signal only, not a professional or certified appraisal."
)

# --- Comparison tuning -----------------------------------------------------

# Sanity band on price per sqm, in ILS. A backstop against corrupt rows
# (a mis-keyed area of 1 sqm, say), NOT a substitute for the residential
# filter in `govmap_client.py` — a parking space can sit squarely inside a
# plausible per-sqm range, so only the deal's declared nature excludes it.
# The upper bound is deliberately generous: observed Tel Aviv centre deals
# already reach ~87,000/sqm, and clipping real luxury sales would bias the
# median downward.
_MIN_PLAUSIBLE_PRICE_PER_SQM = 3_000.0
_MAX_PLAUSIBLE_PRICE_PER_SQM = 250_000.0

# Prefer comparables of a similar size: a 40 sqm studio and a 200 sqm
# penthouse have genuinely different per-sqm economics in the same street.
_AREA_BAND = 0.25  # +/- 25%

# Below this many size-matched comparables the band is too narrow to trust,
# so we fall back to the full neighbourhood set and say so in `limitations`.
_MIN_BAND_COMPARABLES = 5

# Below this many comparables in total, a median is not meaningful enough
# to publish as a price signal.
_MIN_TOTAL_COMPARABLES = 3

# Only compare against reasonably recent sales. Some areas have thin deal
# flow, so the raw data reaches back years — and a 2019 sale is a poor
# guide to a 2026 asking price. Observed effect: including them dropped
# Neve Tzedek's 10th-percentile from ~40,000 to ~14,600 ILS/sqm, which
# would have made almost any current listing look wildly overpriced.
_MAX_DEAL_AGE_YEARS = 3

# If the recency window leaves fewer than this, widen back to all deals
# rather than report nothing — stale comparables beat no comparables, as
# long as the date span is disclosed (and it always is).
_MIN_RECENT_COMPARABLES = 8


class ComparablesSnapshot:
    """Loads and queries the committed offline transaction snapshot.

    Kept deliberately small: its only jobs are to read the JSON file once,
    resolve a caller's neighbourhood name to a stored entry, and hand back
    `ComparableDeal` objects — the *same* type the live client produces, so
    the maths downstream cannot tell the two apart.
    """

    def __init__(self, path: Path = DEFAULT_SNAPSHOT_PATH) -> None:
        self._path = path
        # Parsed lazily on first use, then cached for the process lifetime.
        # The file is static, so re-reading it per request would be waste.
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        """Read and cache the snapshot file.

        Deliberately allowed to raise: a missing or corrupt data file is a
        genuine technical failure, and `execute()` converts it into
        `status="error"`. It must not masquerade as "no comparable data",
        which would tell the reader we checked and found nothing when in
        fact we never checked at all.
        """
        if self._data is None:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "neighborhoods" not in parsed:
                raise ValueError(
                    f"Snapshot at {self._path} is missing the 'neighborhoods' key."
                )
            self._data = parsed
        return self._data

    @property
    def generated_at(self) -> str:
        """The date the snapshot was fetched, disclosed in `limitations`."""
        return str(self._load().get("generated_at", "unknown"))

    @staticmethod
    def _normalize(text: str) -> str:
        """Collapse whitespace and case so lookups are forgiving.

        Mirrors the mock implementation's `_normalize`
        (`mock_implementations/price_signal.py:32`) so both behave alike.
        """
        return " ".join(text.strip().lower().split())

    def lookup(self, *keys: str | None) -> list[ComparableDeal]:
        """Find deals for the first key that matches a stored neighbourhood.

        Callers pass keys in priority order — normally `neighborhood` then
        `normalized_address` — and the first hit wins.

        Matching is alias-based, and the aliases live in the data file
        rather than in code. That matters because the two `geo_resolver`
        implementations disagree about language: govmap (and so Nir's real
        resolver) returns Hebrew names like "לב העיר", while the existing
        mock resolver emits English ones like "florentin". Both must
        resolve to the same entry.

        As a last resort a key is matched as a *substring* of an alias, so
        a full address like "Rothschild 30, Lev HaIr" still finds
        "lev hair".

        Returns an empty list when nothing matches — a business "no answer",
        which the caller turns into `outcome="no_comparable_data"`.
        """
        neighborhoods = self._load().get("neighborhoods", {})
        if not isinstance(neighborhoods, dict):
            return []

        for key in keys:
            if not key:
                continue
            needle = self._normalize(key)

            # Pass 1: exact match on the entry key or any of its aliases.
            for entry_key, entry in neighborhoods.items():
                aliases = {self._normalize(entry_key)}
                aliases.update(self._normalize(a) for a in entry.get("aliases", []))
                if needle in aliases:
                    return self._to_deals(entry)

            # Pass 2: substring match, for when the caller passed a full
            # address rather than a bare neighbourhood name.
            for entry_key, entry in neighborhoods.items():
                aliases = {self._normalize(entry_key)}
                aliases.update(self._normalize(a) for a in entry.get("aliases", []))
                if any(alias and alias in needle for alias in aliases):
                    return self._to_deals(entry)

        return []

    @staticmethod
    def _to_deals(entry: dict[str, Any]) -> list[ComparableDeal]:
        """Rehydrate stored rows into `ComparableDeal` objects.

        Rows that fail validation are skipped rather than raising, so one
        bad record in the file cannot break the whole lookup.
        """
        deals: list[ComparableDeal] = []
        for row in entry.get("deals", []):
            try:
                deals.append(ComparableDeal.model_validate(row))
            except Exception:
                logger.warning("Skipping malformed deal row in snapshot: %r", row)
        return deals


class NirPriceSignal:
    """Real `price_signal` tool backed by government transaction records."""

    # Must match the whitelisted tool name exactly — the registry looks the
    # tool up by this string (`tools/registry.py`).
    name = "price_signal"

    def __init__(self, snapshot_path: Path = DEFAULT_SNAPSHOT_PATH) -> None:
        # Injectable so tests can point at a fixture instead of the
        # committed file, and so a corrupt-file test is easy to write.
        self._snapshot = ComparablesSnapshot(snapshot_path)

    async def execute(self, input_model: PriceSignalInput) -> ToolResult[PriceSignalOutput]:
        """Run the price comparison.

        Never raises. Every failure path returns a `ToolResult` instead —
        the outermost `try` is the last-resort guarantee of that, because
        `evidence_collector` must be able to continue after any single tool
        misbehaves.
        """
        # --- 1. Rentals are out of scope, checked first ---------------------
        # Deliberately before every other check, including the missing-field
        # one: a rental is skipped regardless of what else it carries. This
        # tool compares against *purchase* transactions, so applying it to a
        # monthly rent would produce a meaningless number.
        if input_model.listing_type == "rental":
            return ToolResult(
                status="skipped",
                reason="price_signal does not apply to rental listings and was skipped.",
            )

        try:
            # --- 2. Does the listing carry enough to compare at all? -------
            # Both a price and an area are needed, since the entire
            # comparison is denominated in price per sqm.
            missing_fields = [
                field_name
                for field_name, value in (
                    ("asking_price", input_model.asking_price),
                    ("area_sqm", input_model.area_sqm),
                )
                if value is None
            ]
            if missing_fields:
                return self._insufficient_data(missing_fields)

            # --- 3. Fetch comparable transactions --------------------------
            #
            # SWAP: the live path is written and verified, but not enabled.
            #
            # It is fast enough: a measured 2.6-2.9s per address, against
            # the 8s `LocalToolExecutor` ceiling (local.py:36). The offline
            # snapshot is preferred anyway for determinism in tests and for
            # not making every request depend on a free public API being
            # up. See the module docstring of `govmap_client.py`.
            #
            # To go live: uncomment the two lines below, then delete the
            # snapshot line beneath them. Nothing else changes — both paths
            # return the same `ComparableDeal` objects.
            #
            # The scoping arguments are not optional extras: a search
            # radius straddles neighbourhood boundaries, and unscoped
            # results blend two markets into one median. Measured at
            # "אילת 30": unscoped median 52,500/sqm vs scoped 59,688 — a
            # 14% error. `include_own_street` then adds back sales on the
            # address's own street where it runs past the boundary.
            #
            # async with GovmapClient() as client:
            #     deals = await client.find_comparable_deals(
            #         input_model.normalized_address,
            #         only_neighborhood=input_model.neighborhood,
            #         include_own_street=True,
            #     )
            #
            # >>> OFFLINE LIMITATION <<< the snapshot below implements only
            # the neighbourhood half of that rule. Its buckets are keyed by
            # neighbourhood, so they cannot carry "the street of the
            # address" — there is no single address at build time. The
            # street clause only takes effect once the live path is on.
            deals = self._snapshot.lookup(
                input_model.neighborhood,  # preferred key, from geo_resolver
                input_model.normalized_address,  # fallback when no neighbourhood is known
            )

            # --- 4. Nothing to compare against -----------------------------
            # Note this is a successful run: we checked honestly and the
            # area has no usable sales.
            if not deals:
                return self._no_comparable_data(input_model)

            # --- 5. Compute the signal -------------------------------------
            return self._compare(input_model, deals)

        except Exception as exc:
            # Last-resort safety net. A missing/corrupt snapshot file, or
            # any unforeseen bug, becomes a structured error rather than an
            # exception escaping into the agent pipeline.
            logger.exception("price_signal failed for %r", input_model.normalized_address)
            return ToolResult(
                status="error",
                error=f"price_signal could not complete: {type(exc).__name__}: {exc}",
            )

    # --- result builders ---------------------------------------------------

    def _insufficient_data(self, missing_fields: list[str]) -> ToolResult[PriceSignalOutput]:
        """The listing itself lacks the fields needed for any comparison."""
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

    def _no_comparable_data(self, input_model: PriceSignalInput) -> ToolResult[PriceSignalOutput]:
        """We have the listing's numbers, but no sales to compare them to."""
        lookup_key = input_model.neighborhood or input_model.normalized_address
        return ToolResult(
            status="ok",
            data=PriceSignalOutput(
                outcome="no_comparable_data",
                comparison_count=0,
                source=_SOURCE,
                limitations=[
                    f"No comparable transactions available for '{lookup_key}'.",
                    _APPRAISAL_LIMITATION,
                    self._snapshot_limitation(),
                ],
            ),
        )

    def _compare(
        self, input_model: PriceSignalInput, deals: list[ComparableDeal]
    ) -> ToolResult[PriceSignalOutput]:
        """Turn a set of comparable transactions into the price signal.

        Assumes `asking_price` and `area_sqm` are both present — step 2 of
        `execute()` guarantees it.
        """
        # `asking_price`/`area_sqm` are Optional on the model but non-None
        # here; the local names keep the arithmetic readable.
        asking_price = input_model.asking_price
        area_sqm = input_model.area_sqm
        assert asking_price is not None and area_sqm is not None  # noqa: S101
        limitations: list[str] = []

        # Drop implausible per-sqm figures before anything else, so one
        # corrupt row cannot drag the median.
        usable = [
            d
            for d in deals
            if _MIN_PLAUSIBLE_PRICE_PER_SQM <= d.price_per_sqm <= _MAX_PLAUSIBLE_PRICE_PER_SQM
        ]
        if len(usable) < _MIN_TOTAL_COMPARABLES:
            # Too thin to publish a median. Honest "no answer", not an error.
            return self._no_comparable_data(input_model)

        # Drop stale sales next, before the size band, so recency is never
        # traded away to satisfy the size filter.
        usable, dropped_stale = self._recent_only(usable)
        if dropped_stale:
            limitations.append(
                f"Excluded {dropped_stale} transactions older than "
                f"{_MAX_DEAL_AGE_YEARS} years, which are poor guides to current pricing."
            )

        # Prefer similarly sized properties; fall back to the full set when
        # the size band leaves too few to be reliable.
        band = [
            d
            for d in usable
            if abs(d.area_sqm - area_sqm) <= _AREA_BAND * area_sqm
        ]
        if len(band) >= _MIN_BAND_COMPARABLES:
            comparables = band
            limitations.append(
                f"Compared against {len(band)} transactions within "
                f"{int(_AREA_BAND * 100)}% of the listing's {area_sqm:g} sqm."
            )
        else:
            comparables = usable
            limitations.append(
                f"Too few similarly sized transactions ({len(band)}) to compare on size; "
                f"used all {len(usable)} neighbourhood transactions instead, which mixes "
                "different property sizes."
            )

        # The comparison itself.
        prices_per_sqm = sorted(d.price_per_sqm for d in comparables)
        asking_price_per_sqm = asking_price / area_sqm
        median = statistics.median(prices_per_sqm)
        deviation_percent = ((asking_price_per_sqm - median) / median) * 100

        low, high = self._reference_range(prices_per_sqm)

        # Date range of the underlying sales: a reader deserves to know
        # whether "the market" here means this year or five years ago.
        dated = sorted(d.deal_date[:7] for d in comparables if d.deal_date)
        if dated:
            limitations.append(f"Transactions span {dated[0]} to {dated[-1]}.")

        limitations.append(_APPRAISAL_LIMITATION)
        limitations.append(self._snapshot_limitation())

        return ToolResult(
            status="ok",
            data=PriceSignalOutput(
                outcome="compared",
                asking_price_per_sqm=round(asking_price_per_sqm, 2),
                median_comparison_price_per_sqm=round(median, 2),
                reference_range_low_per_sqm=round(low, 2),
                reference_range_high_per_sqm=round(high, 2),
                deviation_percent=round(deviation_percent, 2),
                comparison_count=len(comparables),
                source=_SOURCE,
                limitations=limitations,
            ),
        )

    @staticmethod
    def _recent_only(deals: list[ComparableDeal]) -> tuple[list[ComparableDeal], int]:
        """Keep only deals within `_MAX_DEAL_AGE_YEARS` of the newest one.

        The cutoff is measured from the most recent deal in the data, NOT
        from today's date. Two reasons this is the right choice:

          * Determinism. A wall-clock cutoff would make the same snapshot
            and the same test produce different results as time passes,
            and would silently empty the whole dataset once the snapshot
            aged past the window.
          * It expresses the real intent — "recent relative to this body of
            evidence" — rather than an absolute calendar rule.

        Falls back to the full list when the window leaves too few to be
        meaningful, since stale comparables beat none at all (and the date
        span is always disclosed in `limitations`).

        Returns:
            A `(kept_deals, dropped_count)` tuple. `dropped_count` is 0
            when no filtering was applied or none was needed.
        """
        dated = [d for d in deals if d.deal_date]
        if not dated:
            # No dates at all: nothing to filter on, so keep everything.
            return deals, 0

        # ISO-8601 strings sort chronologically, so the max is the newest.
        newest_year = int(max(d.deal_date for d in dated if d.deal_date)[:4])
        cutoff_year = newest_year - _MAX_DEAL_AGE_YEARS

        recent = [
            d for d in deals if d.deal_date and int(d.deal_date[:4]) >= cutoff_year
        ]
        if len(recent) < _MIN_RECENT_COMPARABLES:
            return deals, 0
        return recent, len(deals) - len(recent)

    @staticmethod
    def _reference_range(sorted_prices: list[float]) -> tuple[float, float]:
        """The low/high band the listing is judged against.

        Uses the 10th and 90th percentiles rather than min/max, so a single
        outlier sale cannot stretch the range into meaninglessness. Falls
        back to min/max for small samples, where percentiles would just be
        the extremes anyway.

        Args:
            sorted_prices: Price-per-sqm values, ascending.
        """
        n = len(sorted_prices)
        if n < 5:
            return sorted_prices[0], sorted_prices[-1]
        # Nearest-rank percentiles: simple, and exact on the data we have.
        low_index = max(0, int(round(0.10 * (n - 1))))
        high_index = min(n - 1, int(round(0.90 * (n - 1))))
        return sorted_prices[low_index], sorted_prices[high_index]

    def _snapshot_limitation(self) -> str:
        """Disclose that the figures are a point-in-time extract.

        Required honesty while the offline path is active: the reader must
        not take a months-old snapshot for today's market. Delete this
        alongside the snapshot when the live path is enabled.
        """
        return (
            f"Comparison data is a point-in-time extract of government transaction "
            f"records taken on {self._snapshot.generated_at}, not a live query, so it "
            "may not reflect the most recent sales."
        )
