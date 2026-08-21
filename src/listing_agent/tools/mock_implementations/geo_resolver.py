"""Deterministic mock `geo_resolver` implementation.

Uses a small hardcoded set of known and ambiguous addresses. Any other
address is reported as a structured `outcome="not_found"` result — never
an exception, and never `ToolResult(status="error")`, since "we couldn't
find this address" is a legitimate business outcome the tool successfully
determined, not a technical failure of the tool itself.
"""

import re

from src.listing_agent.tools.schemas import GeoResolverInput, GeoResolverOutput, ToolResult

_SOURCE = "mock_dataset"

# Trailing punctuation the LLM planner sometimes leaves attached to
# `raw_address` when it splits a longer sentence (e.g. "...תל אביב," before
# the next clause). Stripped so lookups match the known-address table
# regardless of this harmless formatting noise - never affects which
# address the tool resolves to, only whether trailing punctuation blocks
# an otherwise-exact match.
_TRAILING_PUNCTUATION = re.compile(r"[,.;:\s]+$")


def _normalize(address: str) -> str:
    collapsed = " ".join(address.strip().split())
    return _TRAILING_PUNCTUATION.sub("", collapsed).lower()


_KNOWN_ADDRESSES: dict[str, GeoResolverOutput] = {
    _normalize("HaYarkon 120, Tel Aviv"): GeoResolverOutput(
        outcome="resolved",
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        lat=32.0853,
        lon=34.7738,
        resolution_quality="exact",
        neighborhood="Old North (HaTzafon HaYashan)",
        source=_SOURCE,
    ),
}

# Second demo/interactive-agent-demo listing address (Derech HaTayasim 8,
# Ramat Aviv). Several literal spellings are registered because
# triage_planner extracts `raw_address` verbatim from whatever text the
# buyer pasted - this keeps the interactive demo deterministic regardless
# of small punctuation/suffix differences in the pasted listing text.
_DERECH_HATAYASIM_OUTPUT = GeoResolverOutput(
    outcome="resolved",
    normalized_address="Derech HaTayasim 8, Tel Aviv-Yafo",
    lat=32.1128,
    lon=34.8042,
    resolution_quality="exact",
    neighborhood="Ramat Aviv",
    source=_SOURCE,
)
for _demo_variant in (
    "דרך הטייסים 8, תל אביב",
    "דרך הטייסים 8, תל אביב-יפו",
    "דרך הטייסים 8, תל אביב יפו",
    "Derech HaTayasim 8, Tel Aviv",
    "Derech HaTayasim 8, Tel Aviv-Yafo",
    "8 Derech HaTayasim, Tel Aviv",
):
    _KNOWN_ADDRESSES[_normalize(_demo_variant)] = _DERECH_HATAYASIM_OUTPUT

# A small hardcoded set of addresses known to match more than one plausible
# location, used to exercise the "ambiguous" outcome deterministically.
_AMBIGUOUS_ADDRESSES: dict[str, list[str]] = {
    _normalize("Dizengoff 50"): [
        "Dizengoff St 50, Tel Aviv-Yafo",
        "Dizengoff St 50, Ramat Gan",
    ],
}


class MockGeoResolver:
    """Deterministic geo_resolver mock backed by small known-address tables."""

    name = "geo_resolver"

    async def execute(self, input_model: GeoResolverInput) -> ToolResult[GeoResolverOutput]:
        normalized = _normalize(input_model.address)

        resolved = _KNOWN_ADDRESSES.get(normalized)
        if resolved is not None:
            return ToolResult(status="ok", data=resolved)

        candidates = _AMBIGUOUS_ADDRESSES.get(normalized)
        if candidates is not None:
            return ToolResult(
                status="ok",
                data=GeoResolverOutput(
                    outcome="ambiguous",
                    candidates=list(candidates),
                    source=_SOURCE,
                    limitations=[
                        f"'{input_model.address}' matches more than one known location "
                        "and could not be resolved to a single address."
                    ],
                ),
            )

        return ToolResult(
            status="ok",
            data=GeoResolverOutput(
                outcome="not_found",
                source=_SOURCE,
                limitations=[
                    f"MockGeoResolver has no known coordinates for address "
                    f"'{input_model.address}'."
                ],
            ),
        )
