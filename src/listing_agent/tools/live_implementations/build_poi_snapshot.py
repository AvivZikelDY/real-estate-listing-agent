"""Offline generator for `poi_snapshot.json` (points of interest).

RUN THIS MANUALLY. It is never imported at request time.

    python -m src.listing_agent.tools.live_implementations.build_poi_snapshot

WHY THIS EXISTS
---------------
Same reasoning as `build_snapshot.py` for `price_signal`, but the case is
stronger here: Overpass is a free, donation-funded query engine that is
frequently overloaded. The canonical mirror returned **HTTP 504** on the
first real query attempted while building this. Depending on it for every
listing would make `nearby_services` fail unpredictably for reasons that
have nothing to do with the listing.

WHY A SNAPSHOT ACTUALLY WORKS HERE
----------------------------------
Unlike an address lookup (infinite key space — which is why `geo_resolver`
must stay live), points of interest are a FIXED set over a FIXED area. Grab
every school, park, pharmacy, supermarket, daycare, transport stop and
hospital in the Tel Aviv bounding box once, and any point inside that box
can be answered offline: the distance calculation is pure local arithmetic.

The snapshot is coordinate data, not pre-computed answers, so it serves any
listing location and any radius — nothing is baked in.
"""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from src.listing_agent.tools.live_implementations.overpass_client import OverpassClient

# Where the generated file lands: next to this module, so it ships with the
# package and `nearby_services` can find it by relative path.
SNAPSHOT_PATH = Path(__file__).parent / "poi_snapshot.json"

# Bounding box covering Tel Aviv-Yafo and its immediate surroundings
# (south, west, north, east) in WGS84 degrees.
#
# Padded beyond the municipal boundary on purpose: a listing at the northern
# edge of the city should still find the supermarket just over the line in
# Ramat Gan. Services do not stop at municipal borders, and a padded box
# costs only file size.
TEL_AVIV_BBOX = (32.0100, 34.7350, 32.1450, 34.8400)


async def build() -> dict:
    """Fetch every mapped point of interest in the bounding box."""
    print(f"Querying Overpass for bbox {TEL_AVIV_BBOX} ...")
    print("(this is a city-sized query and can take a minute)")

    async with OverpassClient() as client:
        places = await client.find_places_in_bbox(*TEL_AVIV_BBOX)

    # The same real-world place can be mapped more than once in OSM — a
    # school tagged as both a node and an enclosing way, for example. Two
    # entries at the same name and category would double-count in the
    # per-category `count` the contract asks for, so collapse them.
    unique: dict[tuple, dict] = {}
    for place in places:
        # Rounded coordinates so two mappings of the same building a few
        # metres apart collapse together, while genuinely distinct branches
        # of the same chain stay separate.
        key = (place.name, place.category, round(place.lat, 4), round(place.lon, 4))
        unique[key] = {
            "name": place.name,
            "category": place.category,
            "lat": place.lat,
            "lon": place.lon,
        }

    rows = sorted(unique.values(), key=lambda p: (p["category"], p["name"]))
    counts = Counter(row["category"] for row in rows)

    print(f"\n{len(places)} raw, {len(rows)} unique places:")
    for category, count in sorted(counts.items()):
        print(f"  {category:20s} {count:5d}")

    return {
        "generated_at": datetime.now(UTC).date().isoformat(),
        "source": "OpenStreetMap via Overpass API",
        "licence": "ODbL - (c) OpenStreetMap contributors",
        "bbox": {
            "south": TEL_AVIV_BBOX[0],
            "west": TEL_AVIV_BBOX[1],
            "north": TEL_AVIV_BBOX[2],
            "east": TEL_AVIV_BBOX[3],
        },
        "counts": dict(sorted(counts.items())),
        "places": rows,
    }


def main() -> None:
    """Entry point: build the snapshot and write it to disk."""
    snapshot = asyncio.run(build())

    if not snapshot["places"]:
        # Refuse to overwrite good committed data with an empty file — that
        # would make every location look like a service desert.
        print("\nNo places fetched at all. Refusing to overwrite the existing snapshot.")
        raise SystemExit(1)

    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {SNAPSHOT_PATH} ({len(snapshot['places'])} places).")


if __name__ == "__main__":
    main()
