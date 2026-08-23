import pytest

from src.listing_agent.agent.schemas import TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.triage_planner import MODULE_NAME, run_triage_planner
from tests.llm.triage_planner_fixtures import ALL_CASES


@pytest.mark.asyncio
@pytest.mark.parametrize("name,prompt,expected_plan", ALL_CASES, ids=[c[0] for c in ALL_CASES])
async def test_triage_planner_returns_expected_plan(
    name: str, prompt: str, expected_plan: TriagePlan
) -> None:
    client = FakeLLMClient()
    client.set_response(MODULE_NAME, expected_plan)
    tracer = StepTracer()

    plan = await run_triage_planner(prompt, client, tracer)

    assert plan == expected_plan


@pytest.mark.asyncio
async def test_triage_planner_module_name_is_exact() -> None:
    assert MODULE_NAME == "triage_planner"


@pytest.mark.asyncio
async def test_triage_planner_records_step_via_tracer() -> None:
    client = FakeLLMClient()
    from tests.llm.triage_planner_fixtures import PURCHASE_FAMILY_PLAN, PURCHASE_FAMILY_PROMPT

    client.set_response(MODULE_NAME, PURCHASE_FAMILY_PLAN)
    tracer = StepTracer()

    await run_triage_planner(PURCHASE_FAMILY_PROMPT, client, tracer)

    steps = tracer.get_steps()
    assert len(steps) == 1
    step = steps[0]
    assert step.step == "triage_planner"
    assert step.type == "llm"
    assert step.status == "ok"
    assert step.detail["prompt"]["user_payload"] == {"prompt": PURCHASE_FAMILY_PROMPT}


@pytest.mark.asyncio
async def test_family_suitability_may_select_geo_resolver_and_nearby_services() -> None:
    from tests.llm.triage_planner_fixtures import PURCHASE_FAMILY_PLAN

    tools = {call.tool for call in PURCHASE_FAMILY_PLAN.tool_calls}
    assert tools <= {"geo_resolver", "nearby_services"}
    assert PURCHASE_FAMILY_PLAN.buyer_goal == "family_suitability"


@pytest.mark.asyncio
async def test_price_validation_may_select_geo_resolver_and_price_signal() -> None:
    from tests.llm.triage_planner_fixtures import PURCHASE_PRICE_PLAN

    tools = {call.tool for call in PURCHASE_PRICE_PLAN.tool_calls}
    assert tools <= {"geo_resolver", "price_signal"}
    assert PURCHASE_PRICE_PLAN.buyer_goal == "price_validation"


@pytest.mark.asyncio
async def test_price_signal_not_selected_when_sqm_missing() -> None:
    from tests.llm.triage_planner_fixtures import PURCHASE_MISSING_SQM_PLAN

    assert PURCHASE_MISSING_SQM_PLAN.property_details.sqm is None
    assert "sqm" in PURCHASE_MISSING_SQM_PLAN.missing_information
    tools = {call.tool for call in PURCHASE_MISSING_SQM_PLAN.tool_calls}
    assert "price_signal" not in tools


@pytest.mark.asyncio
async def test_price_signal_not_selected_for_rentals() -> None:
    from tests.llm.triage_planner_fixtures import RENTAL_PRICE_PLAN

    assert RENTAL_PRICE_PLAN.property_details.listing_type == "rental"
    tools = {call.tool for call in RENTAL_PRICE_PLAN.tool_calls}
    assert "price_signal" not in tools


@pytest.mark.asyncio
async def test_subjective_only_listing_triggers_no_tools() -> None:
    from tests.llm.triage_planner_fixtures import SUBJECTIVE_ONLY_PLAN

    assert all(claim.type == "subjective" for claim in SUBJECTIVE_ONLY_PLAN.claims)
    assert SUBJECTIVE_ONLY_PLAN.tool_calls == []
    assert set(SUBJECTIVE_ONLY_PLAN.missing_information) == {
        "raw_address",
        "asking_price",
        "rooms",
        "sqm",
    }


# --- Stage 4.2: persona-intent-preservation prompt contract ----------------
#
# NOTE: these are prompt-content contract tests only - they prove the
# instruction text is present in the system prompt, not that a real LLM call
# will obey it. Live behavioral verification is Stage 4.3.


def test_system_prompt_maps_uncovered_persona_suitability_to_general_question() -> None:
    """Stage 4.1 regression: family_suitability must apply only when the
    CURRENT prompt itself asks about family/children/kids. A suitability
    question about another persona (students, roommates, elderly, singles,
    couples) must fall through to general_question, never be silently
    converted to family_suitability, and no new buyer_goal may be invented
    for it."""
    from src.listing_agent.llm.triage_planner.prompts import TRIAGE_PLANNER_SYSTEM_PROMPT

    prompt = TRIAGE_PLANNER_SYSTEM_PROMPT
    assert "family_suitability applies ONLY when the CURRENT user prompt explicitly" in prompt
    assert "must NOT be silently converted to family_suitability" in prompt
    assert "students, roommates, elderly people, singles, couples" in prompt
    assert "Do not invent a new buyer_goal value for it" in prompt


def test_system_prompt_gives_concrete_students_vs_family_followup_example() -> None:
    """Stage 4.1 regression: previous focus "park, supermarket" followed by
    "האם מתאימה לסטודנטים?" must resolve to general_question, not
    family_suitability - explicit current-turn intent always overrides
    previous conversation focus."""
    from src.listing_agent.llm.triage_planner.prompts import TRIAGE_PLANNER_SYSTEM_PROMPT

    prompt = TRIAGE_PLANNER_SYSTEM_PROMPT
    assert '"park, supermarket"' in prompt
    assert "האם מתאימה לסטודנטים?" in prompt
    assert "buyer_goal must be general_question, NOT family_suitability" in prompt


def test_system_prompt_states_street_number_precedence_over_neighborhood() -> None:
    """Stabilization fix regression: when a listing contains both an
    explicit street + house number and a separate neighborhood/area phrase,
    the explicit street + house number must be extracted as raw_address -
    the neighborhood name must never be substituted in its place (the
    observed "בדיצה 4 ... באזור צפון יפו" -> wrongly extracted "צפון יפו 4"
    regression)."""
    from src.listing_agent.llm.triage_planner.prompts import TRIAGE_PLANNER_SYSTEM_PROMPT

    prompt = TRIAGE_PLANNER_SYSTEM_PROMPT
    assert (
        "the explicit street + house number is the property address" in prompt
    )
    assert "never attach the house number to the neighborhood/area name" in prompt
    assert "דיצה 4, תל אביב יפו" in prompt
    assert "צפון יפו 4, תל אביב יפו" in prompt
