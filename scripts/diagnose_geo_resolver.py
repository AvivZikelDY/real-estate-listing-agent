"""Standalone diagnostic that shows WHY `geo_resolver` returned what it did
for one address, gate by gate.

`geo_resolver` collapses four very different situations into the single
`not_found` outcome, and the report text cannot tell them apart:

    1. govmap returned nothing at all;
    2. govmap returned only non-addressable types (roads, junctions,
       transport features) - `_ADDRESSABLE_TYPES` filtered them out;
    3. the best match came back WITHOUT the house number the query asked
       for, AND the street-level fallback found nothing to stand behind
       (`_house_number_matches` + `_street_level_match`);
    4. the match carried no parsable WKT point.

This script prints the raw govmap response and evaluates each gate
separately, so the actual cause is visible. It also re-runs the same
address through a few mechanical variants (comma stripped, Hebrew "ב"
prefix stripped off the street name, house number dropped), because the
prefix and punctuation that `triage_planner` copies verbatim from a
listing are themselves prime suspects: govmap scores on word overlap, and
"בשמואל" is not a word in "שמואל תמיר".

Observability-only: never imported by the app, executes no LLM call, and
touches only govmap's public autocomplete endpoint.

Usage:
    python scripts/diagnose_geo_resolver.py
    python scripts/diagnose_geo_resolver.py "בשמואל תמיר 2, תל אביב"
    python scripts/diagnose_geo_resolver.py "רוטשילד 30 תל אביב" --no-variants

Each variant is one HTTP call against the live API (plus one per
`execute()`), self-throttled by `GovmapClient` to ~5 req/s.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Allow running as a script from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.listing_agent.agent.tool_input_builder import extract_city_hint  # noqa: E402
from src.listing_agent.tools.nir_implementations.coordinates import (  # noqa: E402
    parse_wkt_point,
    web_mercator_to_wgs84,
)
from src.listing_agent.tools.nir_implementations.geo_resolver import (  # noqa: E402
    _ADDRESSABLE_TYPES,
    _DOMINANT_SCORE_RATIO,
    NirGeoResolver,
)
from src.listing_agent.tools.nir_implementations.govmap_client import GovmapClient  # noqa: E402
from src.listing_agent.tools.schemas import GeoResolverInput  # noqa: E402

# The address that prompted this script: reported "not found in the
# government index" end to end, with nearby_services skipped as a result.
_DEFAULT_ADDRESS = "בשמואל תמיר 2, תל אביב"

_DIGITS = re.compile(r"\d{1,4}")


def _build_query(address: str, city_hint: str | None) -> str:
    """Reproduce the exact search text `NirGeoResolver.execute` would send.

    MIRRORS `geo_resolver.py` (the `_strip_filler` call plus the city-hint
    folding immediately after it). Kept as a copy rather than a refactor of
    production code - if that block ever changes, this line drifts, so the
    printed "sent to API" is a reconstruction, not a capture. Every run also
    calls the real `execute()`, whose outcome is the authoritative one.
    """
    query = NirGeoResolver._strip_filler(address)
    hint = (city_hint or "").strip()
    if hint:
        query = query.replace(f"ב{hint}", hint)
        if hint.lower() not in query.lower():
            query = f"{query} {hint}"
    return query


def _variants(address: str) -> list[tuple[str, str]]:
    """Mechanical rewrites of `address`, each isolating one suspect.

    Only ever removes characters that are already in the input - never
    invents a street name or a city.
    """
    out: list[tuple[str, str]] = [("as extracted by triage_planner", address)]

    no_punct = address.replace(",", " ").replace("  ", " ").strip()
    if no_punct != address:
        out.append(("commas removed", no_punct))

    # Strip a leading Hebrew "ב" from the FIRST token only ("בשמואל" ->
    # "שמואל"). Production deliberately does not do this (it would maul
    # "ביאליק"); the point here is to measure how much it costs.
    tokens = no_punct.split()
    if tokens and tokens[0].startswith("ב") and len(tokens[0]) > 2:
        stripped = " ".join([tokens[0][1:], *tokens[1:]])
        out.append(("leading ב dropped from the street name", stripped))

    # Same again with no house number: separates "this street is unknown"
    # from "this street is known but number N is not indexed".
    last = out[-1][1]
    without_number = " ".join(t for t in last.split() if not _DIGITS.fullmatch(t))
    if without_number != last:
        out.append(("house number dropped", without_number))

    return out


def _print_results(results: list[dict]) -> None:
    if not results:
        print("    (govmap returned 0 results)")
        return
    for i, result in enumerate(results):
        if not isinstance(result, dict):
            print(f"    [{i}] (non-dict entry: {result!r})")
            continue
        type_ = str(result.get("type", "")).lower()
        mark = "keep" if type_ in _ADDRESSABLE_TYPES else "DROP"
        shape = result.get("shape")
        point = parse_wkt_point(shape) if shape else None
        coords = ""
        if point is not None:
            lat, lon = web_mercator_to_wgs84(*point)
            coords = f"  {lat:.5f},{lon:.5f}"
        print(
            f"    [{i}] {mark}  score={float(result.get('score', 0) or 0):>9.1f}"
            f"  type={type_ or '?':<14} {result.get('text', '')!r}{coords}"
        )


def _explain_gates(query: str, results: list[dict]) -> None:
    """Walk the same four gates `execute()` walks, printing each verdict."""
    if not results:
        print("  GATE 1 (any results)      -> FAIL: not_found")
        return
    print("  GATE 1 (any results)      -> pass")

    usable = [
        r
        for r in results
        if isinstance(r, dict) and str(r.get("type", "")).lower() in _ADDRESSABLE_TYPES
    ]
    if not usable:
        print(
            f"  GATE 2 (addressable type) -> FAIL: not_found "
            f"(all {len(results)} results were non-addressable types)"
        )
        return
    print(f"  GATE 2 (addressable type) -> pass ({len(usable)}/{len(results)} kept)")

    best = usable[0]
    wanted = set(_DIGITS.findall(query))
    found = set(_DIGITS.findall(str(best.get("text") or "")))
    if wanted and not (wanted & found):
        print(
            f"  GATE 3 (house number)     -> MISS "
            f"(query asked for {sorted(wanted)}, best match "
            f"{best.get('text')!r} carries {sorted(found) or 'no number'})"
        )
        print(
            "      -> hands over to the street-level fallback "
            "(`_street_level_match`); see execute() below for what it decided"
        )
        return
    asked = f"asked for {sorted(wanted)}" if wanted else "query names no number"
    print(f"  GATE 3 (house number)     -> pass ({asked})")

    if len(usable) == 1:
        print("  GATE 4 (dominance)        -> pass (single addressable result)")
    else:
        best_score = float(usable[0].get("score", 0) or 0)
        runner_up = float(usable[1].get("score", 0) or 0)
        ratio = best_score / runner_up if runner_up > 0 else float("inf")
        verdict = "pass" if ratio >= _DOMINANT_SCORE_RATIO else "FAIL: ambiguous"
        print(
            f"  GATE 4 (dominance)        -> {verdict} "
            f"({best_score:.1f} / {runner_up:.1f} = {ratio:.2f}, "
            f"need >= {_DOMINANT_SCORE_RATIO})"
        )

    if parse_wkt_point(best.get("shape")) is None:
        print("  GATE 5 (parsable point)   -> FAIL: not_found (unparsable shape)")
    else:
        print("  GATE 5 (parsable point)   -> pass")


async def _diagnose(address: str, *, label: str, neighborhood: bool) -> None:
    city_hint = extract_city_hint(address)
    query = _build_query(address, city_hint)

    print()
    print("=" * 78)
    print(f"VARIANT: {label}")
    print(f"  raw address : {address!r}")
    print(f"  city_hint   : {city_hint!r}   (extract_city_hint, substring match)")
    print(f"  sent to API : {query!r}")
    print("-" * 78)

    async with GovmapClient() as client:
        results = await client.autocomplete(query)
    print(f"  govmap returned {len(results)} result(s):")
    _print_results(results)
    print("-" * 78)
    _explain_gates(query, results)

    tool = NirGeoResolver(resolve_neighborhood=neighborhood)
    result = await tool.execute(GeoResolverInput(address=address, city_hint=city_hint))
    print("-" * 78)
    if result.data is None:
        print(f"  execute()  -> status={result.status} error={result.error}")
        return
    data = result.data
    print(
        f"  execute()  -> status={result.status} outcome={data.outcome}"
        f" quality={data.resolution_quality} address={data.normalized_address!r}"
    )
    if data.lat is not None:
        print(f"                lat/lon={data.lat},{data.lon} neighborhood={data.neighborhood!r}")
    if data.candidates:
        print(f"                candidates={data.candidates}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", nargs="?", default=_DEFAULT_ADDRESS)
    parser.add_argument(
        "--no-variants",
        action="store_true",
        help="Only diagnose the address exactly as given.",
    )
    parser.add_argument(
        "--neighborhood",
        action="store_true",
        help="Also run the two-call neighbourhood lookup (slower).",
    )
    args = parser.parse_args()

    cases = (
        [("as given", args.address)] if args.no_variants else _variants(args.address)
    )
    for label, address in cases:
        await _diagnose(address, label=label, neighborhood=args.neighborhood)
    print()


if __name__ == "__main__":
    # Hebrew output on a Windows console defaults to a codepage that cannot
    # encode it; force UTF-8 rather than crash halfway through a run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(_main())
