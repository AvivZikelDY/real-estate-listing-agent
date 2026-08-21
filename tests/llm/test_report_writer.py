from datetime import UTC, datetime

from src.listing_agent.agent.schemas import (
    EvidenceBundle,
    PropertyDetails,
    ToolEvidence,
    TriagePlan,
)
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import (
    build_compact_evidence_bundle,
    run_report_writer,
)
from src.listing_agent.llm.report_writer.schemas import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME


def _bundle(**overrides) -> EvidenceBundle:
    plan = overrides.pop(
        "plan",
        TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address="HaYarkon 120, Tel Aviv",
                asking_price=3_000_000,
                rooms=3,
                sqm=80,
            ),
            buyer_goal="family_suitability",
            tool_calls=[],
        ),
    )
    defaults = {
        "request_id": "req-1",
        "generated_at": datetime.now(UTC),
        "prompt": "Is this good for a family?",
        "plan": plan,
        "executed_tools": [],
        "tool_results": [],
        "missing_information": [],
        "limitations": [],
    }
    defaults.update(overrides)
    return EvidenceBundle(**defaults)


async def test_run_report_writer_returns_report_text_and_records_step() -> None:
    bundle = _bundle()
    client = FakeLLMClient()
    client.set_response(MODULE_NAME, ReportOutput(report="Final buyer-facing report."))
    tracer = StepTracer()

    report = await run_report_writer(bundle, client, tracer)

    assert report == "Final buyer-facing report."
    steps = tracer.get_steps()
    assert len(steps) == 1
    assert steps[0].step == "report_writer"
    assert steps[0].type == "llm"
    assert steps[0].status == "ok"


def test_compact_bundle_excludes_internal_bookkeeping_fields() -> None:
    bundle = _bundle()

    payload = build_compact_evidence_bundle(bundle)

    assert "request_id" not in payload
    assert "generated_at" not in payload
    assert set(payload) == {
        "prompt",
        "property_details",
        "buyer_goal",
        "claims",
        "executed_tools",
        "tool_results",
        "missing_information",
        "limitations",
    }


def test_compact_bundle_never_upgrades_a_failed_tool_to_supporting_evidence() -> None:
    bundle = _bundle(
        tool_results=[
            ToolEvidence(tool="geo_resolver", status="error", error="no known coordinates"),
        ],
        executed_tools=["geo_resolver"],
        limitations=["geo_resolver failed: no known coordinates"],
    )

    payload = build_compact_evidence_bundle(bundle)

    assert payload["tool_results"][0]["status"] == "error"
    assert "no known coordinates" in payload["tool_results"][0]["error"]
    assert any("geo_resolver failed" in item for item in payload["limitations"])


def test_compact_bundle_marks_rental_listing_without_price_signal() -> None:
    rental_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="rental",
            raw_address="HaYarkon 120, Tel Aviv",
            asking_price=6_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="price_validation",
        tool_calls=[],
    )
    bundle = _bundle(plan=rental_plan, executed_tools=[], tool_results=[])

    payload = build_compact_evidence_bundle(bundle)

    assert payload["property_details"]["listing_type"] == "rental"
    assert "price_signal" not in payload["executed_tools"]
    assert all(result["tool"] != "price_signal" for result in payload["tool_results"])


def test_compact_bundle_surfaces_missing_information_for_clarification() -> None:
    bundle = _bundle(missing_information=["sqm"])

    payload = build_compact_evidence_bundle(bundle)

    assert payload["missing_information"] == ["sqm"]


def test_compact_bundle_tool_result_excludes_raw_output_and_transport_metadata() -> None:
    """Only the normalized `observations`/`source`/`limitations` projection is
    sent - never the tool's raw typed `output` dict (redundant with
    `observations`) and never internal executor bookkeeping (e.g. `attempts`).
    """
    bundle = _bundle(
        tool_results=[
            ToolEvidence(
                tool="geo_resolver",
                status="ok",
                output={"outcome": "resolved", "lat": 32.08, "lon": 34.77, "source": "mock-v1"},
                observations={"outcome": "resolved", "lat": 32.08, "lon": 34.77},
                source="mock-v1",
                claim_id="claim-0",
            ),
        ],
        executed_tools=["geo_resolver"],
    )

    payload = build_compact_evidence_bundle(bundle)

    tool_result = payload["tool_results"][0]
    assert set(tool_result) == {
        "tool",
        "status",
        "claim_id",
        "claim_ids",
        "observations",
        "source",
        "limitations",
        "error",
        "error_code",
        "reason",
    }
    assert "output" not in tool_result
    assert "attempts" not in tool_result
    assert tool_result["observations"] == {"outcome": "resolved", "lat": 32.08, "lon": 34.77}
    assert tool_result["claim_id"] == "claim-0"


def test_system_prompt_scopes_missing_information_and_questions_to_current_turn() -> None:
    """The report must not claim asking_price/rooms/sqm are required for a
    nearby-only question, must scope "missing information" to what the
    current turn's check actually needs, and must never proactively push
    price_signal when the buyer did not ask about price."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert "nearby_services-only question does not need asking_price/rooms/sqm" in prompt
    assert "actually needed for the check(s) the buyer is asking about" in prompt
    assert "Never proactively suggest, recommend, or ask about running price_signal" in prompt


def test_system_prompt_avoids_redundant_address_and_comparable_sales_requests() -> None:
    """When geo_resolver already resolved the address and price_signal
    already returned comparison figures this turn, the report must not ask
    the buyer to re-supply either (Phase 0 price-signal regression follow-up)."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert "reconfirm, restate, or provide the address" in prompt
    assert "provide comparable sale prices, market" in prompt
