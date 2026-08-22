"""Offline generator for `comparables_snapshot.json`.

RUN THIS MANUALLY. It is never imported at request time.

    python -m src.listing_agent.tools.live_implementations.build_snapshot

WHY THIS EXISTS
---------------
`price_signal` reads a committed snapshot rather than calling govmap on
every request — for determinism in tests and to avoid depending on a free
public API being up. (It is not a speed problem: the live path measures
2.6-2.9s against an 8s ceiling. See `govmap_client.py`.) So we pay the
fetch cost once, here, offline, and commit the result.

Two things this buys us:

  * The committed data is *real* — actual government transaction records,
    not numbers someone invented. That is the whole difference between
    this and the mock implementation.
  * `GovmapClient` gets exercised end to end every time the snapshot is
    regenerated. A successful run is proof the live code path works.

>>> BUCKETS ARE KEYED BY WHAT THE DATA SAYS, NOT BY THE SEED ADDRESS <<<
------------------------------------------------------------------------
This is the important design point, and it was learned the hard way.

The obvious approach — "search around a Florentin address, call the result
Florentin" — is wrong, because a search radius straddles neighbourhood
boundaries. Measured contamination from exactly that approach:

    Kerem HaTeimanim seed  ->  53% of results were actually לב העיר
    Florentin seed         ->  33% were actually צפון יפו
    Ramat Aviv seed        ->  19% were נווה אביבים / אפקה

Kerem HaTeimanim came out majority-foreign, and Florentin's median was
inflated by about a third (real ~38,700/sqm, contaminated ~52,200/sqm) —
enough to tell a seller their overpriced listing was fair.

Every deal carries its true `neighborhood` from the API, so the fix is to
group by that field and ignore the seed's label entirely. A seed is now
only a place to *start looking*; one seed may legitimately populate several
buckets, and several seeds may contribute to the same bucket.

Re-run this whenever the data goes stale. `price_signal` surfaces the
snapshot's `generated_at` date in its `limitations`, so a stale file is
disclosed to the reader rather than passed off as current.
"""

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from src.listing_agent.tools.live_implementations.govmap_client import (
    DEAL_TYPE_SECOND_HAND,
    ComparableDeal,
    GovmapClient,
)

# Where the generated file lands: right next to this module, so it ships
# with the package and `price_signal` can find it by relative path.
SNAPSHOT_PATH = Path(__file__).parent / "comparables_snapshot.json"

# Cap on deals stored per neighbourhood. The API can return well over a
# thousand per area; we keep the most recent N so the committed file stays
# reviewable, without weakening the median (a few hundred recent sales is
# far more than enough, and older ones are less comparable anyway).
MAX_DEALS_PER_NEIGHBORHOOD = 300

# Drop buckets too thin to support a median. `price_signal` needs at least
# 8 recent comparables before it will publish a signal, so anything under
# this would only ever produce `no_comparable_data` — better to omit it and
# say so honestly than to ship a bucket that cannot answer.
MIN_DEALS_PER_NEIGHBORHOOD = 25

# Seed addresses: places to start looking. The *label* is not used to name
# the resulting bucket (see the module docstring) — only to make this list
# readable and to spread the searches across Tel Aviv.
SEED_ADDRESSES: list[str] = [
    "פלורנטין 20 תל אביב",  # Florentin
    "דיזנגוף 200 תל אביב",  # Old North
    "שבזי 20 תל אביב",  # Neve Tzedek
    "רוטשילד 30 תל אביב",  # Lev HaIr / city centre
    "ברודצקי 20 תל אביב",  # Ramat Aviv
    "נחליאל 10 תל אביב",  # Kerem HaTeimanim
    "אילת 30 תל אביב",  # Neve Tzedek / North Jaffa boundary
    "ארלוזורוב 100 תל אביב",  # Old North, eastern side
]

# English aliases per real Hebrew neighbourhood name.
#
# Needed because the two `geo_resolver` implementations disagree about
# language: govmap (and so the live resolver) returns Hebrew, while the
# existing mock resolver emits English like "florentin". Both must reach
# the same bucket. The Hebrew name itself is always an alias, added
# automatically below, so a neighbourhood missing from this map still works
# for Hebrew callers.
_ENGLISH_ALIASES: dict[str, list[str]] = {
    "פלורנטין": ["florentin", "florentine"],
    "לב העיר": ["lev hair", "lev ha'ir", "city center", "city centre"],
    "נווה צדק": ["neve tzedek", "neveh tzedek"],
    "כרם התימנים": ["kerem hateimanim", "kerem hatemanim", "yemenite quarter"],
    "רמת אביב": ["ramat aviv"],
    "צפון יפו": ["north jaffa", "tzafon yafo"],
    "נווה אביבים וסביבתה": ["neve avivim"],
    "הצפון הישן-החלק הצפוני": [
        "old north (hatzafon hayashan)",
        "old north",
        "hatzafon hayashan",
    ],
    "הצפון הישן-החלק הדרומי": ["old north south", "old north"],
    # NOTE the inconsistent punctuation in the source data: the "old north"
    # names carry a hyphen, the "new north" ones a plain space. Both
    # spellings are listed rather than normalised, so the keys match
    # whatever the API actually returns.
    "הצפון החדש החלק הדרומי": ["new north south", "new north"],
    "הצפון החדש-החלק הדרומי": ["new north south", "new north"],
    "הצפון החדש החלק הצפוני": ["new north north"],
    "הצפון החדש-החלק הצפוני": ["new north north"],
    "הצפון החדש-סביבת כיכר המדינה": ["kikar hamedina"],
    "גבעת הרצל": ["givat herzl"],
}


async def build() -> dict:
    """Fetch every seed, regroup by true neighbourhood, and assemble the file.

    Seeds are fetched sequentially rather than concurrently: `GovmapClient`
    self-throttles, and firing eight pipelines at once would defeat that and
    risk a 429 from a free public API. This script is not latency-sensitive
    — it runs once, by hand.
    """
    # Accumulates across seeds, so two seeds landing in the same
    # neighbourhood contribute to one bucket instead of competing.
    by_neighborhood: dict[str, list[ComparableDeal]] = defaultdict(list)

    async with GovmapClient() as client:
        for seed in SEED_ADDRESSES:
            print(f"Fetching around {seed} ...")
            try:
                # No `only_neighborhood` filter here on purpose: we want
                # everything the radius found, then split it by true
                # neighbourhood below. A boundary seed usefully populates
                # two buckets rather than polluting one.
                deals = await client.find_comparable_deals(seed)
            except Exception as exc:
                # One seed failing must not lose the others. Report and
                # carry on; the operator can re-run.
                print(f"  !! FAILED: {type(exc).__name__}: {exc}")
                continue

            found: dict[str, int] = defaultdict(int)
            for deal in deals:
                # Deals with no neighbourhood cannot be bucketed at all.
                if not deal.neighborhood:
                    continue
                by_neighborhood[deal.neighborhood.strip()].append(deal)
                found[deal.neighborhood.strip()] += 1

            summary = ", ".join(f"{name} ({n})" for name, n in sorted(found.items()))
            print(f"  -> {len(deals)} deals across: {summary or 'nothing'}")

    # --- assemble the buckets ---------------------------------------------
    snapshot: dict = {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "source": "govmap.gov.il real-estate API",
        "deal_type": DEAL_TYPE_SECOND_HAND,
        "deal_type_description": "second-hand (resale) residential transactions only",
        "keyed_by": (
            "the neighborhood reported by the API on each deal, not by the seed "
            "address searched - see build_snapshot.py"
        ),
        "neighborhoods": {},
    }

    print()
    for hebrew_name, deals in sorted(by_neighborhood.items()):
        # The same sale can arrive from two overlapping seeds.
        unique: dict[tuple, ComparableDeal] = {}
        for d in deals:
            unique[(d.price, d.area_sqm, d.deal_date, d.street)] = d
        merged = list(unique.values())

        if len(merged) < MIN_DEALS_PER_NEIGHBORHOOD:
            print(f"  skipping {hebrew_name}: only {len(merged)} deals")
            continue

        # Most recent first, then truncate. `deal_date` is ISO-8601, so a
        # plain string sort is chronological; `or ""` parks undated rows at
        # the end rather than crashing the comparison.
        merged.sort(key=lambda d: d.deal_date or "", reverse=True)
        kept = merged[:MAX_DEALS_PER_NEIGHBORHOOD]

        # The Hebrew name is always its own alias, so Hebrew callers work
        # even for neighbourhoods absent from `_ENGLISH_ALIASES`.
        aliases = [hebrew_name] + _ENGLISH_ALIASES.get(hebrew_name, [])

        key = _ENGLISH_ALIASES.get(hebrew_name, [hebrew_name])[0]
        snapshot["neighborhoods"][key] = {
            "display_name": hebrew_name,
            "aliases": aliases,
            "deals": [
                {
                    "price": d.price,
                    "area_sqm": d.area_sqm,
                    "rooms": d.rooms,
                    "deal_date": d.deal_date,
                    "street": d.street,
                    # Persisted so bucket purity stays auditable after the
                    # fact. Dropping this field is what let the original
                    # contamination go unnoticed.
                    "neighborhood": d.neighborhood,
                }
                for d in kept
            ],
        }
        print(f"  {key:28s} <- {hebrew_name:26s} {len(merged):5d} deals, keeping {len(kept)}")

    return snapshot


def main() -> None:
    """Entry point: build the snapshot and write it to disk."""
    snapshot = asyncio.run(build())

    total = sum(len(n["deals"]) for n in snapshot["neighborhoods"].values())
    if total == 0:
        # Refuse to overwrite good committed data with an empty file — that
        # would turn every price_signal call into "no_comparable_data".
        print("\nNo deals fetched at all. Refusing to overwrite the existing snapshot.")
        raise SystemExit(1)

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"\nWrote {SNAPSHOT_PATH} "
        f"({len(snapshot['neighborhoods'])} neighbourhoods, {total} deals)."
    )


if __name__ == "__main__":
    main()
