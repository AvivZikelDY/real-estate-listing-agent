"""Table-driven evaluation scenarios for the triage agent.

Each `EvalScenario` fully specifies one end-to-end run: the `TriagePlan`
`triage_planner` would have produced (fed to a `FakeLLMClient` — no real
LLM call is ever made), the report_writer's canned output, and the
assertions a passing pipeline run must satisfy. `tests/evaluation/
test_evaluation_suite.py` runs every scenario through the real
`agent_orchestrator` (with mock tools) and checks them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from src.listing_agent.agent.schemas import (
    AgentRunResult,
    Claim,
    PropertyDetails,
    ToolCall,
    TriagePlan,
)
from src.listing_agent.agent.tool_executor import LocalToolExecutor, ToolExecutor
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import NearbyServicesInput, ToolResult

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"
# Imaginary street, real city. An address naming no recognised city is
# refused before the tool runs now (`_NO_CITY_HINT_REASON`), and that is a
# scenario of its own - see `address_without_a_city` below.
UNKNOWN_ADDRESS = "99 Imaginary Ave, Tel Aviv"


class _AlwaysFailingNearbyServices:
    """Test double simulating a genuine `nearby_services` outage.

    Distinct from "no coverage for this location" (which the real mock
    reports as `status="ok"` with zero categories) - this simulates the
    tool itself failing (e.g. the data source being unreachable).
    """

    name = "nearby_services"

    async def execute(self, input_model: NearbyServicesInput) -> ToolResult:
        return ToolResult(status="error", error="nearby_services dataset is unreachable.")


def failing_nearby_services_executor() -> ToolExecutor:
    """A `ToolExecutor` identical to the standard mock backend, except
    `nearby_services` always fails."""
    from src.listing_agent.tools.mock_implementations import MockGeoResolver, MockPriceSignal

    registry = ToolRegistry()
    registry.register(MockGeoResolver())
    registry.register(_AlwaysFailingNearbyServices())
    registry.register(MockPriceSignal())
    return LocalToolExecutor(registry)


def default_mock_executor() -> ToolExecutor:
    from src.listing_agent.config import Settings
    from src.listing_agent.tools.dependency import get_tool_registry

    return LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))


def _assert_tool_failed(result: AgentRunResult, tool: str) -> bool:
    evidence = next(e for e in result.bundle.tool_results if e.tool == tool)
    assert evidence.status == "error", f"expected {tool} to have failed"
    return True


def _assert_geo_resolver_not_found(result: AgentRunResult) -> bool:
    """geo_resolver itself succeeds ("ok") with a typed not_found outcome;
    it is not a technical failure of the tool."""
    evidence = next(e for e in result.bundle.tool_results if e.tool == "geo_resolver")
    assert evidence.status == "ok", "expected geo_resolver to report status=ok"
    assert evidence.output["outcome"] == "not_found", "expected outcome=not_found"
    return True


def _assert_tool_never_attempted(result: AgentRunResult, tool: str) -> bool:
    assert tool not in result.bundle.executed_tools, f"{tool} should never have been attempted"
    return True


@dataclass
class EvalScenario:
    """One evaluation scenario: inputs, canned LLM outputs, and expectations."""

    name: str
    input_prompt: str
    plan: TriagePlan
    report: str

    expected_goal: str
    # Tools that must be *attempted* (appear in `bundle.executed_tools`),
    # regardless of whether they succeeded.
    expected_tools: list[str] = field(default_factory=list)
    # Tools that must never be attempted at all.
    forbidden_tools: list[str] = field(default_factory=list)
    expected_missing_information: list[str] = field(default_factory=list)
    expected_report_fragments: list[str] = field(default_factory=list)

    executor_factory: Callable[[], ToolExecutor] = default_mock_executor
    semantic_assertion: Callable[[AgentRunResult], None] | None = None


SCENARIOS: list[EvalScenario] = [
    EvalScenario(
        name="listing_only_fast_triage",
        input_prompt=(
            "3-room apartment in central Tel Aviv, recently renovated, available immediately."
        ),
        plan=TriagePlan(
            property_details=PropertyDetails(listing_type="unknown", rooms=3),
            buyer_goal="fast_claim_triage",
            claims=[Claim(text="recently renovated", type="factual", category="condition")],
            tool_calls=[],
            missing_information=["raw_address"],
        ),
        report=(
            "1. Overall assessment: A quick triage only - the address wasn't given, so no "
            "location checks were possible.\n"
            "2. What was checked: No tools were needed for this fast claim triage.\n"
            "3. Supported claims: none - no tool ran to support any claim.\n"
            "4. Partially supported or unclear claims: 'recently renovated' is the seller's "
            "own claim and was not independently verified.\n"
            "5. Missing information: raw_address.\n"
            "6. Limitations: This is a fast triage only, not a full evaluation.\n"
            "7. Questions to ask: What is the exact address? When was it renovated?"
        ),
        expected_goal="fast_claim_triage",
        expected_tools=[],
        forbidden_tools=["geo_resolver", "nearby_services", "price_signal"],
        expected_missing_information=["raw_address"],
        expected_report_fragments=["recently renovated", "not independently verified"],
    ),
    EvalScenario(
        name="family_suitability",
        input_prompt=(
            f"{KNOWN_ADDRESS}, 4 rooms, 90 sqm, asking 3,200,000 ILS. "
            "Is this suitable for a family with two young kids?"
        ),
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address=KNOWN_ADDRESS,
                asking_price=3_200_000,
                rooms=4,
                sqm=90,
            ),
            buyer_goal="family_suitability",
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: Good basic location signals for a family.\n"
            "2. What was checked: geo_resolver resolved the address, then nearby_services "
            "reported nearby amenities.\n"
            "3. Supported claims: The address resolved and nearby amenities were found.\n"
            "4. Partially supported or unclear claims: none beyond the tool data.\n"
            "5. Missing information: none.\n"
            "6. Limitations: Distances are straight-line, not walking distance.\n"
            "7. Questions to ask: Which specific schools/parks are closest?"
        ),
        expected_goal="family_suitability",
        expected_tools=["geo_resolver", "nearby_services"],
        forbidden_tools=["price_signal"],
        expected_report_fragments=["straight-line"],
    ),
    EvalScenario(
        name="price_validation_complete_fields",
        input_prompt=f"{KNOWN_ADDRESS}, 3 rooms, 80 sqm, asking 4,000,000 ILS. Is the price fair?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address=KNOWN_ADDRESS,
                asking_price=4_000_000,
                rooms=3,
                sqm=80,
            ),
            buyer_goal="price_validation",
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: The asking price can be compared to recent transactions.\n"
            "2. What was checked: geo_resolver resolved the address, then price_signal "
            "compared the asking price per sqm to nearby transactions.\n"
            "3. Supported claims: A price-per-sqm comparison was produced by price_signal.\n"
            "4. Partially supported or unclear claims: none.\n"
            "5. Missing information: none.\n"
            "6. Limitations: This is a data-driven signal only, not a professional appraisal.\n"
            "7. Questions to ask: Are there more recent comparable sales available?"
        ),
        expected_goal="price_validation",
        expected_tools=["geo_resolver", "price_signal"],
        forbidden_tools=["nearby_services"],
        expected_report_fragments=["not a professional appraisal"],
    ),
    EvalScenario(
        name="price_validation_missing_sqm",
        input_prompt=f"{KNOWN_ADDRESS}, 3 rooms, asking 4,000,000 ILS. Is the price fair?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address=KNOWN_ADDRESS,
                asking_price=4_000_000,
                rooms=3,
                sqm=None,
            ),
            buyer_goal="price_validation",
            missing_information=["sqm"],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: The price cannot be validated without the size.\n"
            "2. What was checked: geo_resolver resolved the address. price_signal was "
            "skipped because sqm is required and missing.\n"
            "3. Supported claims: The address resolved successfully.\n"
            "4. Partially supported or unclear claims: The asking price cannot be assessed.\n"
            "5. Missing information: sqm.\n"
            "6. Limitations: price_signal requires sqm and could not run.\n"
            "7. Questions to ask: What is the exact sqm of the apartment?"
        ),
        expected_goal="price_validation",
        expected_tools=["geo_resolver"],
        forbidden_tools=["price_signal", "nearby_services"],
        expected_missing_information=["sqm"],
        expected_report_fragments=["cannot be assessed", "sqm"],
    ),
    EvalScenario(
        name="rental_listing_price_question",
        input_prompt=f"{KNOWN_ADDRESS}, 3 rooms, 80 sqm, 6,000 ILS/month rent. Is the rent fair?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="rental",
                raw_address=KNOWN_ADDRESS,
                asking_price=6_000,
                rooms=3,
                sqm=80,
            ),
            buyer_goal="price_validation",
            # The planner should not select price_signal for a rental, but this
            # scenario also proves plan_validator drops it even if proposed.
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: This is a rental listing; a purchase price signal "
            "does not apply.\n"
            "2. What was checked: geo_resolver resolved the address. price_signal was "
            "skipped because it does not apply to rentals.\n"
            "3. Supported claims: The address resolved successfully.\n"
            "4. Partially supported or unclear claims: Whether the rent is fair could "
            "not be assessed by any tool in this run.\n"
            "5. Missing information: none.\n"
            "6. Limitations: price_signal does not apply to rental listings.\n"
            "7. Questions to ask: Can you share comparable rental listings nearby?"
        ),
        expected_goal="price_validation",
        expected_tools=["geo_resolver"],
        forbidden_tools=["price_signal", "nearby_services"],
        expected_report_fragments=["does not apply to rental"],
    ),
    EvalScenario(
        name="subjective_beautiful_apartment_claim",
        input_prompt="Beautiful, charming apartment in Tel Aviv. What do you think?",
        plan=TriagePlan(
            property_details=PropertyDetails(listing_type="unknown"),
            buyer_goal="general_question",
            claims=[Claim(text="beautiful, charming apartment", type="subjective")],
            missing_information=["raw_address"],
            tool_calls=[],
        ),
        report=(
            "1. Overall assessment: Only a subjective description was given; nothing "
            "here could be checked with a tool.\n"
            "2. What was checked: No tools were needed or run.\n"
            "3. Supported claims: none.\n"
            "4. Partially supported or unclear claims: 'beautiful, charming apartment' is "
            "the seller's subjective opinion and was not verified.\n"
            "5. Missing information: raw_address.\n"
            "6. Limitations: No location or price data was available to check.\n"
            "7. Questions to ask: What is the address, price, and size?"
        ),
        expected_goal="general_question",
        expected_tools=[],
        forbidden_tools=["geo_resolver", "nearby_services", "price_signal"],
        expected_missing_information=["raw_address"],
        expected_report_fragments=["subjective opinion", "was not verified"],
    ),
    EvalScenario(
        name="unknown_address",
        input_prompt=f"{UNKNOWN_ADDRESS}, 3 rooms, 80 sqm. Good for a family?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase", raw_address=UNKNOWN_ADDRESS, rooms=3, sqm=80
            ),
            buyer_goal="family_suitability",
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: The address could not be resolved, so location "
            "checks could not run.\n"
            "2. What was checked: geo_resolver attempted to resolve the address and failed. "
            "nearby_services was skipped as a result.\n"
            "3. Supported claims: none.\n"
            "4. Partially supported or unclear claims: Family suitability could not be "
            "assessed without resolved coordinates.\n"
            "5. Missing information: none reported by the planner.\n"
            "6. Limitations: geo_resolver failed to resolve the address; nearby_services "
            "could not run as a result.\n"
            "7. Questions to ask: Can you confirm the exact, full address?"
        ),
        expected_goal="family_suitability",
        expected_tools=["geo_resolver"],
        forbidden_tools=["price_signal"],
        expected_report_fragments=["could not be resolved", "skipped"],
        semantic_assertion=lambda result: (
            _assert_geo_resolver_not_found(result)
            and _assert_tool_never_attempted(result, "nearby_services")
        ),
    ),
    EvalScenario(
        name="nearby_services_failure",
        input_prompt=f"{KNOWN_ADDRESS}, 4 rooms, 90 sqm. Good for a family?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase", raw_address=KNOWN_ADDRESS, rooms=4, sqm=90
            ),
            buyer_goal="family_suitability",
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: The address resolved, but nearby amenities could not "
            "be checked due to a data outage.\n"
            "2. What was checked: geo_resolver resolved the address. nearby_services failed.\n"
            "3. Supported claims: The address resolved successfully.\n"
            "4. Partially supported or unclear claims: Nearby amenities are unknown - "
            "nearby_services failed and its result must not be treated as supporting "
            "evidence.\n"
            "5. Missing information: none.\n"
            "6. Limitations: nearby_services dataset is unreachable.\n"
            "7. Questions to ask: What amenities are actually nearby?"
        ),
        expected_goal="family_suitability",
        expected_tools=["geo_resolver", "nearby_services"],
        forbidden_tools=["price_signal"],
        expected_report_fragments=["must not be treated as supporting evidence"],
        executor_factory=failing_nearby_services_executor,
        semantic_assertion=lambda result: _assert_tool_failed(result, "nearby_services"),
    ),
    EvalScenario(
        name="below_market_price_claim",
        input_prompt=(
            f"{KNOWN_ADDRESS}, 3 rooms, 80 sqm, asking 3,000,000 ILS - below market price!"
        ),
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase",
                raw_address=KNOWN_ADDRESS,
                asking_price=3_000_000,
                rooms=3,
                sqm=80,
            ),
            buyer_goal="price_validation",
            claims=[Claim(text="below market price", type="factual", category="price")],
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="price_signal", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: The 'below market price' claim can be checked against "
            "comparable transactions.\n"
            "2. What was checked: geo_resolver resolved the address, then price_signal "
            "compared the asking price per sqm to nearby transactions.\n"
            "3. Supported claims: price_signal produced a price-per-sqm comparison for "
            "this address.\n"
            "4. Partially supported or unclear claims: Whether the price is truly 'below "
            "market' depends on the comparison figures, not the seller's own wording.\n"
            "5. Missing information: none.\n"
            "6. Limitations: This is a data-driven signal only, not a professional "
            "appraisal.\n"
            "7. Questions to ask: Which specific comparable sales support 'below market'?"
        ),
        expected_goal="price_validation",
        expected_tools=["geo_resolver", "price_signal"],
        forbidden_tools=["nearby_services"],
        expected_report_fragments=["not the seller's own wording", "not a professional"],
    ),
    EvalScenario(
        name="duplicate_tool_calls_from_planner",
        input_prompt=f"{KNOWN_ADDRESS}, 4 rooms, 90 sqm. Good for a family?",
        plan=TriagePlan(
            property_details=PropertyDetails(
                listing_type="purchase", raw_address=KNOWN_ADDRESS, rooms=4, sqm=90
            ),
            buyer_goal="family_suitability",
            tool_calls=[
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="geo_resolver", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
                ToolCall(tool="nearby_services", arguments={}),
            ],
        ),
        report=(
            "1. Overall assessment: Good basic location signals for a family.\n"
            "2. What was checked: geo_resolver resolved the address once, then "
            "nearby_services ran once.\n"
            "3. Supported claims: The address resolved and nearby amenities were found.\n"
            "4. Partially supported or unclear claims: none.\n"
            "5. Missing information: none.\n"
            "6. Limitations: Distances are straight-line, not walking distance.\n"
            "7. Questions to ask: Which specific schools/parks are closest?"
        ),
        expected_goal="family_suitability",
        expected_tools=["geo_resolver", "nearby_services"],
        forbidden_tools=["price_signal"],
        expected_report_fragments=["once"],
        semantic_assertion=lambda result: (
            result.bundle.executed_tools.count("geo_resolver") == 1
            and result.bundle.executed_tools.count("nearby_services") == 1
        ),
    ),
]
