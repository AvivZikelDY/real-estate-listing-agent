"""Evidence-mapping layer (Phase 15).

Maps a `ToolExecutor`'s typed `ToolExecutionResult` (see
`agent/tool_executor/result.py`) into the `ToolEvidence` entries stored on
`EvidenceBundle` and eventually handed to `report_writer`. This is the one
place responsible for:

- Turning each tool's typed output into small, *grounded* `observations` -
  structured facts only, never an inferred conclusion (no
  "family_friendly": true, no "is_bargain": true, no "walking_time_min").
- Attaching a clear, human-readable `limitations` entry for every outcome
  that is not a fully confident, well-covered "ok" result - so a business
  "no answer" (not_found/ambiguous/insufficient_data/no_comparable_data), a
  technical failure, and a dependency skip are each described accurately
  and never conflated with one another.
- Linking evidence back to the `Claim` (by `Claim.id`) it is relevant to,
  when the plan contains a plausible match - see `find_relevant_claim_id`.

Pure, deterministic, LLM-free - like every other module in this package.
"""

from typing import Any

from src.listing_agent.agent.evidence_mapper.claim_linking import (
    find_relevant_claim_id,
    find_relevant_claim_ids,
)
from src.listing_agent.agent.schemas import Claim, ToolCall, ToolEvidence
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult
from src.listing_agent.tools.schemas import (
    GeoResolverOutput,
    NearbyServicesOutput,
    PriceSignalOutput,
)

# A price comparison built from fewer than this many transactions is real,
# typed evidence (`outcome="compared"`) but too thin to state with
# confidence - flagged as a "weak" signal rather than silently presented
# with the same weight as a well-covered comparison.
_WEAK_PRICE_SIGNAL_COMPARISON_COUNT = 3

_TECHNICAL_FAILURE_LIMITATION = {
    "geo_resolver": (
        "geo_resolver could not be completed due to a technical failure; "
        "this is not evidence that the address doesn't exist."
    ),
    "nearby_services": (
        "nearby_services could not be completed due to a technical failure; "
        "this is not evidence that no services exist nearby."
    ),
    "price_signal": (
        "price_signal could not be completed due to a technical failure; "
        "this is not evidence that the asking price is unsupported."
    ),
}


def map_tool_evidence(
    call: ToolCall,
    result: ToolExecutionResult,
    claims: list[Claim],
) -> ToolEvidence:
    """Map one tool's `ToolExecutionResult` into a `ToolEvidence` entry."""
    claim_ids = find_relevant_claim_ids(call.tool, claims, call)
    claim_id = find_relevant_claim_id(call.tool, claims, call)

    if result.status == "error":
        error = result.error
        return ToolEvidence(
            tool=call.tool,
            status="error",
            error=error.message if error is not None else None,
            error_code=error.code.value if error is not None else None,
            claim_id=claim_id,
            claim_ids=claim_ids,
            observations=None,
            source=None,
            limitations=[_TECHNICAL_FAILURE_LIMITATION[call.tool]],
        )

    if result.status == "skipped":
        return ToolEvidence(
            tool=call.tool,
            status="skipped",
            reason=result.reason,
            claim_id=claim_id,
            claim_ids=claim_ids,
            observations=None,
            source=None,
            limitations=[],
        )

    # status == "ok": dispatch per tool to build grounded observations.
    output = result.output
    if call.tool == "geo_resolver":
        assert isinstance(output, GeoResolverOutput)  # noqa: S101 - internal invariant
        observations, limitations, source = _map_geo_resolver(output)
    elif call.tool == "nearby_services":
        assert isinstance(output, NearbyServicesOutput)  # noqa: S101
        observations, limitations, source = _map_nearby_services(output)
    else:
        assert isinstance(output, PriceSignalOutput)  # noqa: S101
        observations, limitations, source = _map_price_signal(output)

    return ToolEvidence(
        tool=call.tool,
        status="ok",
        output=output.model_dump(mode="json"),
        claim_id=claim_id,
        claim_ids=claim_ids,
        observations=observations,
        source=source,
        limitations=limitations,
    )


def _map_geo_resolver(
    output: GeoResolverOutput,
) -> tuple[dict[str, Any], list[str], str | None]:
    limitations = list(output.limitations)
    if output.outcome == "resolved":
        observations: dict[str, Any] = {
            "outcome": "resolved",
            "normalized_address": output.normalized_address,
            "lat": output.lat,
            "lon": output.lon,
            "resolution_quality": output.resolution_quality,
            "neighborhood": output.neighborhood,
        }
    elif output.outcome == "ambiguous":
        observations = {"outcome": "ambiguous", "candidates": list(output.candidates)}
        limitations.append("The address is ambiguous; no single confirmed match was found.")
    else:  # "not_found"
        observations = {"outcome": "not_found"}
        limitations.append(
            "The address could not be resolved; location-based checks were not possible."
        )
    return observations, limitations, output.source


def _map_nearby_services(
    output: NearbyServicesOutput,
) -> tuple[dict[str, Any], list[str], str | None]:
    limitations = list(output.limitations)
    observations: dict[str, Any] = {
        "place_count": len(output.places),
        "categories": [category.model_dump(mode="json") for category in output.categories],
        "evidence_quality": output.evidence_quality,
    }
    if not output.places:
        limitations.append(
            "No matching services were found within the search radius; this does not "
            "necessarily mean the area lacks such services entirely."
        )
    # `NearbyServicesOutput` has no single top-level `source` (see
    # `NearbyPlace.source` per place); leave `source` unset rather than
    # guessing one.
    return observations, limitations, None


def _map_price_signal(
    output: PriceSignalOutput,
) -> tuple[dict[str, Any], list[str], str | None]:
    limitations = list(output.limitations)
    if output.outcome == "compared":
        is_weak = output.comparison_count < _WEAK_PRICE_SIGNAL_COMPARISON_COUNT
        observations: dict[str, Any] = {
            "outcome": "compared",
            "asking_price_per_sqm": output.asking_price_per_sqm,
            "median_comparison_price_per_sqm": output.median_comparison_price_per_sqm,
            "reference_range_low_per_sqm": output.reference_range_low_per_sqm,
            "reference_range_high_per_sqm": output.reference_range_high_per_sqm,
            "deviation_percent": output.deviation_percent,
            "comparison_count": output.comparison_count,
            "signal_strength": "weak" if is_weak else "normal",
        }
        if is_weak:
            limitations.append(
                "This comparison is based on very few comparable transactions "
                f"({output.comparison_count}); treat it as a weak signal only."
            )
    elif output.outcome == "insufficient_data":
        observations = {
            "outcome": "insufficient_data",
            "missing_fields": list(output.missing_fields),
        }
        limitations.append(
            "The listing does not provide enough information "
            f"({', '.join(output.missing_fields) or 'required fields'}) to compute a "
            "price comparison."
        )
    else:  # "no_comparable_data"
        observations = {"outcome": "no_comparable_data"}
        limitations.append(
            "No comparable transaction data was available for this address/area; this "
            "is not evidence about whether the asking price is fair."
        )
    return observations, limitations, output.source
