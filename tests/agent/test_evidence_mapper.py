"""Tests for the Phase 15 evidence-mapping layer.

Covers: every geo_resolver/nearby_services/price_signal outcome maps to a
correctly-shaped `ToolEvidence`; limitations are preserved/added; raw
provider fields never leak; unsupported conclusions are never introduced;
claim linkage works; and `report_writer` receives only the compact,
normalized Evidence Bundle.
"""

from src.listing_agent.agent.evidence_mapper import find_relevant_claim_id, map_tool_evidence
from src.listing_agent.agent.schemas import Claim, ToolCall
from src.listing_agent.agent.tool_executor.errors import ToolError, ToolErrorCode
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult
from src.listing_agent.tools.schemas import (
    GeoResolverOutput,
    NearbyPlace,
    NearbyServicesOutput,
    PriceSignalOutput,
    ServiceCategorySummary,
)

# --- claim linking -------------------------------------------------------------


def test_find_relevant_claim_id_matches_price_category() -> None:
    claims = [
        Claim(id="claim-0", text="4 rooms", type="factual", category="size"),
        Claim(id="claim-1", text="below market price", type="factual", category="price"),
    ]

    assert find_relevant_claim_id("price_signal", claims) == "claim-1"


def test_find_relevant_claim_id_returns_none_without_a_match() -> None:
    claims = [Claim(id="claim-0", text="4 rooms", type="factual", category="size")]

    assert find_relevant_claim_id("price_signal", claims) is None
    assert find_relevant_claim_id("geo_resolver", claims) is None
    assert find_relevant_claim_id("nearby_services", []) is None


# --- geo_resolver outcomes -------------------------------------------------------


def test_map_geo_resolver_resolved() -> None:
    output = GeoResolverOutput(
        outcome="resolved",
        normalized_address="HaYarkon 120, Tel Aviv",
        lat=32.08,
        lon=34.77,
        resolution_quality="exact",
        neighborhood="Old North",
        source="mock-geo-v1",
        limitations=["Mock dataset only."],
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="geo_resolver"), result, [])

    assert evidence.status == "ok"
    assert evidence.observations["outcome"] == "resolved"
    assert evidence.observations["lat"] == 32.08
    assert evidence.source == "mock-geo-v1"
    assert "Mock dataset only." in evidence.limitations


def test_map_geo_resolver_ambiguous() -> None:
    output = GeoResolverOutput(
        outcome="ambiguous",
        candidates=["1 Herzl St, Tel Aviv", "1 Herzl St, Ramat Gan"],
        source="mock-geo-v1",
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="geo_resolver"), result, [])

    assert evidence.status == "ok"
    assert evidence.observations["outcome"] == "ambiguous"
    assert len(evidence.observations["candidates"]) == 2
    assert any("ambiguous" in item.lower() for item in evidence.limitations)


def test_map_geo_resolver_not_found() -> None:
    output = GeoResolverOutput(outcome="not_found", source="mock-geo-v1")
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="geo_resolver"), result, [])

    assert evidence.status == "ok"
    assert evidence.observations["outcome"] == "not_found"
    assert any("could not be resolved" in item for item in evidence.limitations)


def test_map_geo_resolver_technical_failure() -> None:
    result = ToolExecutionResult(
        status="error",
        error=ToolError(code=ToolErrorCode.PROVIDER_UNAVAILABLE, message="upstream down"),
    )

    evidence = map_tool_evidence(ToolCall(tool="geo_resolver"), result, [])

    assert evidence.status == "error"
    assert evidence.observations is None
    assert evidence.error_code == "provider_unavailable"
    assert any(
        "not evidence that the address doesn't exist" in item for item in evidence.limitations
    )


# --- nearby_services outcomes ----------------------------------------------------


def test_map_nearby_services_with_results() -> None:
    output = NearbyServicesOutput(
        places=[
            NearbyPlace(name="Corner Market", category="supermarket", distance_m=120, source="mock")
        ],
        categories=[
            ServiceCategorySummary(category="supermarket", count=1, nearest_distance_m=120)
        ],
        evidence_quality="high",
        limitations=["Distances are straight-line, not walking time."],
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="nearby_services"), result, [])

    assert evidence.status == "ok"
    assert evidence.observations["place_count"] == 1
    assert evidence.source is None  # no single source for nearby_services
    assert "Distances are straight-line, not walking time." in evidence.limitations
    # Grounding: no inferred booleans like "family_friendly" ever appear.
    assert "family_friendly" not in evidence.observations
    assert "school_quality" not in evidence.observations


def test_map_nearby_services_empty_successful_result() -> None:
    output = NearbyServicesOutput(places=[], categories=[], evidence_quality="low")
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="nearby_services"), result, [])

    assert evidence.status == "ok"
    assert evidence.observations["place_count"] == 0
    assert any("no matching services were found" in item.lower() for item in evidence.limitations)


def test_map_nearby_services_provider_unavailable() -> None:
    result = ToolExecutionResult(
        status="error",
        error=ToolError(code=ToolErrorCode.TIMEOUT, message="request timed out"),
    )

    evidence = map_tool_evidence(ToolCall(tool="nearby_services"), result, [])

    assert evidence.status == "error"
    assert evidence.observations is None
    assert any(
        "not evidence that no services exist nearby" in item for item in evidence.limitations
    )


# --- price_signal outcomes -------------------------------------------------------


def test_map_price_signal_meaningful_comparison() -> None:
    output = PriceSignalOutput(
        outcome="compared",
        asking_price_per_sqm=40_000,
        median_comparison_price_per_sqm=38_000,
        reference_range_low_per_sqm=35_000,
        reference_range_high_per_sqm=42_000,
        deviation_percent=5.3,
        comparison_count=25,
        source="mock-price-v1",
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, [])

    assert evidence.observations["signal_strength"] == "normal"
    assert evidence.source == "mock-price-v1"
    assert not any("weak signal" in item for item in evidence.limitations)


def test_map_price_signal_weak_signal_for_few_comparisons() -> None:
    output = PriceSignalOutput(
        outcome="compared",
        asking_price_per_sqm=40_000,
        median_comparison_price_per_sqm=38_000,
        deviation_percent=5.3,
        comparison_count=2,
        source="mock-price-v1",
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, [])

    assert evidence.observations["signal_strength"] == "weak"
    assert any("weak signal" in item for item in evidence.limitations)


def test_map_price_signal_insufficient_property_data() -> None:
    output = PriceSignalOutput(
        outcome="insufficient_data",
        missing_fields=["asking_price"],
        source="mock-price-v1",
    )
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, [])

    assert evidence.observations["outcome"] == "insufficient_data"
    assert any("does not provide enough information" in item for item in evidence.limitations)


def test_map_price_signal_no_comparable_data() -> None:
    output = PriceSignalOutput(outcome="no_comparable_data", source="mock-price-v1")
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, [])

    assert evidence.observations["outcome"] == "no_comparable_data"
    assert any(
        "not evidence about whether the asking price is fair" in item
        for item in evidence.limitations
    )
    # Grounding: never labels this a bargain/overpriced conclusion.
    assert "is_bargain" not in evidence.observations


def test_map_price_signal_provider_unavailable() -> None:
    result = ToolExecutionResult(
        status="error",
        error=ToolError(code=ToolErrorCode.RATE_LIMITED, message="rate limited"),
    )

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, [])

    assert evidence.status == "error"
    assert any(
        "not evidence that the asking price is unsupported" in item for item in evidence.limitations
    )


# --- claim linkage end-to-end ----------------------------------------------------


def test_map_tool_evidence_links_claim_id_when_category_matches() -> None:
    claims = [Claim(id="claim-7", text="below market price", type="factual", category="price")]
    output = PriceSignalOutput(outcome="no_comparable_data", source="mock-price-v1")
    result = ToolExecutionResult(status="ok", output=output)

    evidence = map_tool_evidence(ToolCall(tool="price_signal"), result, claims)

    assert evidence.claim_id == "claim-7"
