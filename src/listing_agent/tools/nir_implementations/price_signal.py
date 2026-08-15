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
Real transactions published by the Israeli government via the govmap API,
queried LIVE on each request: four HTTP calls, ~0.7s, against the 8s
`LocalToolExecutor` ceiling.

The comparables are the deals recorded for the address's OWN land parcel's
street and neighbourhood. That is correct by construction — the parcel
belongs to exactly one neighbourhood, so there is no way for two
neighbourhoods to be blended into one median. See
`GovmapClient.find_polygon_for_address`.

If govmap is unreachable, the tool degrades to a committed offline extract
(`comparables_snapshot.json`) rather than failing, and says so in
`limitations`. An EMPTY live answer is not a failure and does not trigger
the fallback — see `_fetch_comparables`.

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
    GovmapClient,
)
from src.listing_agent.tools.schemas import PriceSignalInput, PriceSignalOutput, ToolResult

logger = logging.getLogger(__name__)

# Default location of the committed snapshot. Tests inject their own path
# instead, so they never depend on the real file's contents.
DEFAULT_SNAPSHOT_PATH = Path(__file__).parent / "comparables_snapshot.json"

_SOURCE = "govmap.gov.il (Israeli government real-estate transactions, live)"
_FALLBACK_SOURCE = "govmap.gov.il (Israeli government real-estate transactions, offline extract)"

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

    def __init__(
        self,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
        client_factory: type[GovmapClient] | None = GovmapClient,
    ) -> None:
        """
        Args:
            snapshot_path: Committed comparables file used as the fallback.
                Injectable so tests use a fixture, never the real file.
            client_factory: Builds the live govmap client. Injectable so
                tests can supply a stub and never touch the network; pass
                `None` to disable the live path entirely and read only the
                snapshot.
        """
        self._snapshot = ComparablesSnapshot(snapshot_path)
        self._client_factory = client_factory

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
            # Live first, committed snapshot only if govmap is unreachable.
            deals, used_fallback = await self._fetch_comparables(input_model)

            # --- 4. Nothing to compare against -----------------------------
            # Note this is a successful run: we checked honestly and the
            # area has no usable sales.
            if not deals:
                return self._no_comparable_data(input_model, used_fallback)

            # --- 5. Compute the signal -------------------------------------
            return self._compare(input_model, deals, used_fallback)

        except Exception as exc:
            # Last-resort safety net. A missing/corrupt snapshot file, or
            # any unforeseen bug, becomes a structured error rather than an
            # exception escaping into the agent pipeline.
            logger.exception("price_signal failed for %r", input_model.normalized_address)
            return ToolResult(
                status="error",
                error=f"price_signal could not complete: {type(exc).__name__}: {exc}",
            )

    # --- data sourcing -----------------------------------------------------

    async def _fetch_comparables(
        self, input_model: PriceSignalInput
    ) -> tuple[list[ComparableDeal], bool]:
        """Get comparable transactions, live if possible.

        THE FALLBACK RULE, and why it is drawn where it is:

          * A TECHNICAL failure — govmap unreachable, timing out, rate
            limiting — falls back to the committed snapshot. Stale data
            beats no answer, and the staleness is disclosed.
          * An EMPTY live result does NOT fall back. govmap answering "there
            are no recorded sales here" is a real answer, and quietly
            substituting snapshot data for a different area would turn an
            honest "no data" into a confident wrong number.

        Returns:
            A `(deals, used_fallback)` tuple. `used_fallback` drives the
            disclosure in `limitations`, so a reader always knows whether
            they are looking at live records or a point-in-time extract.
        """
        if self._client_factory is not None:
            try:
                async with self._client_factory() as client:
                    # Four calls, ~0.7s. No neighbourhood filtering needed:
                 # the client locates the address's own land parcel, so
                    # the deals are its own neighbourhood by construction.
                    return await client.find_comparable_deals(
                        input_model.normalized_address
                    ), False
            except Exception:
                # Deliberately broad: ANY failure reaching govmap should
                # degrade to the snapshot rather than fail the tool. The
                # typed errors raised by the client are included here.
                logger.warning(
                    "Live govmap lookup failed for %r; falling back to the "
                    "committed snapshot",
                    input_model.normalized_address,
                    exc_info=True,
                )

        # Snapshot fallback, keyed by neighbourhood then address.
        return (
            self._snapshot.lookup(
                input_model.neighborhood,  # preferred key, from geo_resolver
                input_model.normalized_address,  # when no neighbourhood is known
            ),
            True,
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

    def _no_comparable_data(
        self, input_model: PriceSignalInput, used_fallback: bool
    ) -> ToolResult[PriceSignalOutput]:
        """We have the listing's numbers, but no sales to compare them to."""
        lookup_key = input_model.neighborhood or input_model.normalized_address
        limitations = [
            f"No comparable transactions available for '{lookup_key}'.",
            _APPRAISAL_LIMITATION,
        ]
        limitations.extend(self._source_limitations(used_fallback))
        return ToolResult(
            status="ok",
            data=PriceSignalOutput(
                outcome="no_comparable_data",
                comparison_count=0,
                source=self._source_name(used_fallback),
                limitations=limitations,
            ),
        )

    def _compare(
        self,
        input_model: PriceSignalInput,
        deals: list[ComparableDeal],
        used_fallback: bool = False,
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
            return self._no_comparable_data(input_model, used_fallback)

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
        limitations.extend(self._source_limitations(used_fallback))

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
                source=self._source_name(used_fallback),
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

    @staticmethod
    def _source_name(used_fallback: bool) -> str:
        """Name the data source honestly, per result."""
        return _FALLBACK_SOURCE if used_fallback else _SOURCE

    def _source_limitations(self, used_fallback: bool) -> list[str]:
        """Disclose which source actually answered.

        This is not decoration. When the live lookup fails we silently
        degrade to months-old data, and a reader comparing an asking price
        against it deserves to know that is what happened — otherwise a
        stale median reads exactly like a current one.
        """
        if not used_fallback:
            return []
        return [
            f"Live government data was unavailable, so this comparison uses an offline "
            f"extract taken on {self._snapshot.generated_at}. It covers a limited set of "
            "Tel Aviv neighbourhoods and may not reflect the most recent sales."
        ]
