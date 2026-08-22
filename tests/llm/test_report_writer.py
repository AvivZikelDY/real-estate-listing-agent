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


# --- Stage 1.8: skipped-this-turn must never be mislabeled "reused" --------


def test_system_prompt_never_labels_a_skipped_result_as_reused() -> None:
    """Stage 1.5-1.8 regression (the fresh-conversation A3 rerun): a
    `status == "skipped"` (or "error") tool_results entry absent from
    `executed_tools` was attempted and skipped/failed THIS turn - it must
    never be described as "reused from an earlier turn" or "already checked
    earlier in this conversation", even in a stateless/first-turn request
    with no earlier turn at all. Only a `status == "ok"` entry absent from
    `executed_tools` is genuinely reused evidence from a prior turn."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert 'status "skipped" or "error" is NEVER "reused from an earlier turn"' in prompt
    assert 'A status "ok" entry absent from' in prompt
    assert "was reused from an earlier turn of this conversation" in prompt


def test_compact_bundle_skipped_this_turn_result_carries_status_and_reason() -> None:
    """Case A - skipped this turn: a fresh, stateless request (no
    conversation_id, no earlier turn) where `price_signal` is
    `status="skipped"` because `geo_resolver` did not resolve the address,
    and `price_signal` is absent from `executed_tools` (this is exactly the
    real A3-rerun shape). The compact bundle handed to `report_writer` must
    carry the real skip status and reason - the only signal the (now fixed)
    system prompt needs to correctly describe this as a this-turn skip,
    never a prior-turn reuse. No schema change: `status`/`reason` already
    existed on `ToolEvidence`."""
    bundle = _bundle(
        prompt="האם המחיר הגיוני ביחס לאזור?",
        executed_tools=["geo_resolver"],
        tool_results=[
            ToolEvidence(
                tool="geo_resolver",
                status="ok",
                observations={"outcome": "ambiguous"},
            ),
            ToolEvidence(
                tool="price_signal",
                status="skipped",
                reason="Skipped because geo_resolver did not produce a resolved address.",
                limitations=[
                    "price_signal skipped: geo_resolver did not produce a resolved address."
                ],
            ),
        ],
    )

    payload = build_compact_evidence_bundle(bundle)
    price_entry = next(e for e in payload["tool_results"] if e["tool"] == "price_signal")

    assert price_entry["status"] == "skipped"
    assert "did not produce a resolved address" in price_entry["reason"]
    assert "price_signal" not in payload["executed_tools"]


def test_compact_bundle_reused_ok_result_is_distinguishable_from_skip() -> None:
    """Behavioral proof (Case B - genuine reuse): a successful `status="ok"`
    `nearby_services` result absent from `executed_tools` (e.g. reused via
    conversation context on a "כמה רחוק?" follow-up) is a different, still
    valid shape from a skipped/errored entry - report_writer can tell them
    apart purely from `status`, with no schema change required."""
    bundle = _bundle(
        executed_tools=["geo_resolver"],
        tool_results=[
            ToolEvidence(tool="geo_resolver", status="ok", observations={"outcome": "resolved"}),
            ToolEvidence(
                tool="nearby_services",
                status="ok",
                observations={"place_count": 1, "categories": ["park"]},
            ),
        ],
    )

    payload = build_compact_evidence_bundle(bundle)
    nearby_entry = next(e for e in payload["tool_results"] if e["tool"] == "nearby_services")

    assert nearby_entry["status"] == "ok"
    assert "nearby_services" not in payload["executed_tools"]


def test_compact_bundle_executed_this_turn_result_is_correctly_flagged() -> None:
    """Behavioral proof (Case C - executed this turn): a successful result
    whose tool name IS in `executed_tools` is the plain "ran this turn"
    case - unaffected by the skip/reuse distinction fix."""
    bundle = _bundle(
        executed_tools=["geo_resolver", "price_signal"],
        tool_results=[
            ToolEvidence(tool="geo_resolver", status="ok", observations={"outcome": "resolved"}),
            ToolEvidence(
                tool="price_signal", status="ok", observations={"deviation_percent": -11.71}
            ),
        ],
    )

    payload = build_compact_evidence_bundle(bundle)

    assert set(payload["executed_tools"]) == {"geo_resolver", "price_signal"}
    for entry in payload["tool_results"]:
        assert entry["tool"] in payload["executed_tools"]
        assert entry["status"] == "ok"


# --- Stage 4.2: report_writer defense-in-depth safeguards ------------------
#
# NOTE: these are prompt-content contract tests only - they prove the
# instruction text is present in the deployed system prompt, not that a real
# LLM call will obey it. Live behavioral verification is Stage 4.3.


def test_system_prompt_forbids_persona_substitution() -> None:
    """Stage 4.1 regression (students answered as family): the report must
    never substitute one buyer persona/audience for another, and the
    current-turn prompt is the sole authoritative source for the persona."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert "sole authoritative source for which buyer persona/audience" in prompt
    assert "Never substitute one persona/audience for another" in prompt
    assert '"students"' in prompt and '"family"' in prompt


def test_system_prompt_preserves_proper_nouns_verbatim() -> None:
    """Stage 4.1 regression ("ראובן רובין 6" -> "רחוב רובין 6"): addresses,
    street names, and other proper nouns must be reproduced exactly as given,
    never shortened, "cleaned up", or corrected."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert "Reproduce addresses, street names, neighborhood names" in prompt
    assert '"ראובן רובין 6"' in prompt
    assert 'never "רחוב רובין 6"' in prompt


def test_system_prompt_forbids_redundant_nearby_services_rerun_recommendation() -> None:
    """Stage 4.1 finding: when a nearby_services result already has
    status "ok" and already answers the current question, the report must
    not recommend re-running/re-confirming it - only a genuinely uncovered
    category, distance, or fact justifies mentioning nearby_services again."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert 're-run, or "double-check" a' in prompt
    assert "nearby_services category when a nearby_services tool_result already has" in prompt


def test_system_prompt_forbids_internal_evidence_bundle_terminology() -> None:
    """Stage 4.1 finding: the report must never expose implementation/
    data-structure terms such as "Evidence Bundle" or "bundle"/"בנדל", while
    tool names themselves remain intentionally visible in "What was
    checked"."""
    from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT

    prompt = REPORT_WRITER_SYSTEM_PROMPT
    assert '"Evidence Bundle", "bundle", "בנדל"' in prompt
    assert "Tool names themselves (geo_resolver, nearby_services, price_signal) may" in prompt

