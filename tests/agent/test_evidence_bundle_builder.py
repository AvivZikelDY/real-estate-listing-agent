from src.listing_agent.agent.evidence_bundle_builder import build_evidence_bundle
from src.listing_agent.agent.schemas import PropertyDetails, ToolEvidence, TriagePlan


def _plan(**overrides) -> TriagePlan:
    property_details = PropertyDetails(
        listing_type="purchase",
        raw_address="HaYarkon 120, Tel Aviv",
        asking_price=3_000_000,
        rooms=3,
        sqm=80,
    )
    return TriagePlan(
        property_details=property_details,
        buyer_goal="general_question",
        tool_calls=[],
        missing_information=overrides.get("missing_information", []),
    )


def test_bundle_contains_all_required_fields_and_no_report() -> None:
    plan = _plan()

    bundle = build_evidence_bundle(
        prompt="Is this a good deal?",
        plan=plan,
        tool_results=[],
        executed_tools=[],
    )

    assert bundle.prompt == "Is this a good deal?"
    assert bundle.plan == plan
    assert bundle.executed_tools == []
    assert bundle.tool_results == []
    assert bundle.missing_information == []
    assert bundle.limitations == []
    assert bundle.request_id
    assert bundle.generated_at is not None
    assert not hasattr(bundle, "report")


def test_successful_tool_limitations_are_aggregated() -> None:
    plan = _plan()
    evidence = ToolEvidence(
        tool="price_signal",
        status="ok",
        output={"limitations": ["Not a professional appraisal."]},
        limitations=["Not a professional appraisal."],
    )

    bundle = build_evidence_bundle(
        prompt="p", plan=plan, tool_results=[evidence], executed_tools=["price_signal"]
    )

    assert "Not a professional appraisal." in bundle.limitations


def test_failed_tool_produces_a_limitation_note_and_is_not_treated_as_support() -> None:
    plan = _plan()
    evidence = ToolEvidence(
        tool="geo_resolver",
        status="error",
        error="no known coordinates",
        limitations=["geo_resolver failed: no known coordinates"],
    )

    bundle = build_evidence_bundle(
        prompt="p", plan=plan, tool_results=[evidence], executed_tools=["geo_resolver"]
    )

    assert any("geo_resolver failed" in item for item in bundle.limitations)


def test_skipped_tool_produces_a_limitation_note() -> None:
    plan = _plan()
    evidence = ToolEvidence(
        tool="price_signal",
        status="skipped",
        reason="rental listings are skipped",
        limitations=["price_signal skipped: rental listings are skipped"],
    )

    bundle = build_evidence_bundle(
        prompt="p", plan=plan, tool_results=[evidence], executed_tools=[]
    )

    assert any("price_signal skipped" in item for item in bundle.limitations)


def test_missing_information_is_carried_over_from_the_plan() -> None:
    plan = _plan(missing_information=["sqm"])

    bundle = build_evidence_bundle(prompt="p", plan=plan, tool_results=[], executed_tools=[])

    assert bundle.missing_information == ["sqm"]
