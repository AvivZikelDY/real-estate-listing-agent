"""Direct tests of the tool contract Pydantic schemas themselves.

These complement the mock-implementation tests: they verify the models'
own validation rules (bounds, required combinations), JSON serialization
round-tripping, and that JSON schemas can be generated (useful for
API/OpenAPI documentation and for the live implementations to validate
against).
"""

import pytest
from pydantic import ValidationError

from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyServicesInput,
    NearbyServicesOutput,
    PriceSignalInput,
    PriceSignalOutput,
)

# --- geo_resolver ------------------------------------------------------------


def test_geo_resolver_input_output_serialize_round_trip() -> None:
    input_model = GeoResolverInput(address="HaYarkon 120, Tel Aviv", city_hint="Tel Aviv")
    output_model = GeoResolverOutput(
        outcome="resolved",
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        lat=32.0853,
        lon=34.7738,
        resolution_quality="exact",
        source="mock_dataset",
    )

    assert GeoResolverInput.model_validate(input_model.model_dump()) == input_model
    assert GeoResolverOutput.model_validate(output_model.model_dump()) == output_model


def test_geo_resolver_output_rejects_invalid_latitude_and_longitude() -> None:
    with pytest.raises(ValidationError):
        GeoResolverOutput(
            outcome="resolved",
            normalized_address="x",
            lat=200,
            lon=34.7738,
            resolution_quality="exact",
            source="mock_dataset",
        )
    with pytest.raises(ValidationError):
        GeoResolverOutput(
            outcome="resolved",
            normalized_address="x",
            lat=32.0853,
            lon=-200,
            resolution_quality="exact",
            source="mock_dataset",
        )


def test_geo_resolver_output_resolved_outcome_requires_coordinates() -> None:
    with pytest.raises(ValidationError):
        GeoResolverOutput(outcome="resolved", source="mock_dataset")


def test_geo_resolver_output_ambiguous_outcome_does_not_require_coordinates() -> None:
    output = GeoResolverOutput(
        outcome="ambiguous",
        candidates=["A", "B"],
        source="mock_dataset",
    )
    assert output.lat is None
    assert output.lon is None


def test_geo_resolver_output_json_schema_generates() -> None:
    schema = GeoResolverOutput.model_json_schema()
    assert schema["properties"]["outcome"]


# --- nearby_services -----------------------------------------------------------


def test_nearby_services_input_output_serialize_round_trip() -> None:
    input_model = NearbyServicesInput(lat=32.0853, lon=34.7738, radius_m=500)
    output_model = NearbyServicesOutput(places=[], categories=[], evidence_quality="low")

    assert NearbyServicesInput.model_validate(input_model.model_dump()) == input_model
    assert NearbyServicesOutput.model_validate(output_model.model_dump()) == output_model


def test_nearby_services_input_rejects_invalid_latitude_and_longitude() -> None:
    with pytest.raises(ValidationError):
        NearbyServicesInput(lat=91, lon=34.7738)
    with pytest.raises(ValidationError):
        NearbyServicesInput(lat=32.0853, lon=-181)


def test_nearby_services_input_rejects_non_positive_radius() -> None:
    with pytest.raises(ValidationError):
        NearbyServicesInput(lat=32.0853, lon=34.7738, radius_m=0)


def test_nearby_services_input_defaults_to_all_categories() -> None:
    input_model = NearbyServicesInput(lat=32.0853, lon=34.7738)
    assert len(input_model.categories) == 7


def test_nearby_services_output_empty_result_is_distinguishable_from_failure() -> None:
    output = NearbyServicesOutput(places=[], categories=[], evidence_quality="low")
    assert output.places == []
    assert output.categories == []


def test_nearby_services_output_json_schema_generates() -> None:
    schema = NearbyServicesOutput.model_json_schema()
    assert schema["properties"]["places"]


# --- price_signal --------------------------------------------------------------


def test_price_signal_input_output_serialize_round_trip() -> None:
    input_model = PriceSignalInput(
        normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
        listing_type="purchase",
        asking_price=3_600_000,
        area_sqm=90,
        rooms=3,
    )
    output_model = PriceSignalOutput(
        outcome="compared",
        asking_price_per_sqm=40_000,
        median_comparison_price_per_sqm=40_100,
        reference_range_low_per_sqm=38_000,
        reference_range_high_per_sqm=42_000,
        deviation_percent=-0.25,
        comparison_count=5,
        source="mock_dataset",
    )

    assert PriceSignalInput.model_validate(input_model.model_dump()) == input_model
    assert PriceSignalOutput.model_validate(output_model.model_dump()) == output_model


def test_price_signal_input_rejects_non_positive_price_and_area() -> None:
    with pytest.raises(ValidationError):
        PriceSignalInput(normalized_address="x", asking_price=0)
    with pytest.raises(ValidationError):
        PriceSignalInput(normalized_address="x", area_sqm=-10)
    with pytest.raises(ValidationError):
        PriceSignalInput(normalized_address="x", rooms=0)


def test_price_signal_output_compared_outcome_requires_figures() -> None:
    with pytest.raises(ValidationError):
        PriceSignalOutput(outcome="compared", source="mock_dataset")


def test_price_signal_output_insufficient_data_outcome_does_not_require_figures() -> None:
    output = PriceSignalOutput(
        outcome="insufficient_data",
        missing_fields=["asking_price"],
        source="mock_dataset",
    )
    assert output.asking_price_per_sqm is None


def test_price_signal_output_json_schema_generates() -> None:
    schema = PriceSignalOutput.model_json_schema()
    assert schema["properties"]["outcome"]
