"""Builds the illustrative end-to-end examples served by `GET /api/agent_info`.

Each example is produced by actually running `agent_orchestrator` (via an
offline, deterministic `FakeLLMClient` fixture) so `full_response` and
`steps` always reflect the real shape the pipeline produces — never
hand-typed prose that could drift from the real behavior.
"""

from src.listing_agent.agent.orchestrator import run_agent_pipeline
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import LocalToolExecutor
from src.listing_agent.api.schemas import PromptExample
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.report_writer import ReportOutput
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
from src.listing_agent.tools.dependency import get_tool_registry

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"

_FAMILY_SUITABILITY_PROMPT = (
    "We're considering HaYarkon 120, Tel Aviv for our family: 4 rooms, 90 sqm, asking "
    "3,200,000 ILS. The listing says it's 'close to parks and schools' and calls the "
    "apartment 'beautiful and bright'. Is this a good fit for a family with two young kids?"
)

_FAMILY_SUITABILITY_REPORT = """\
1. Overall assessment: This purchase listing has enough facts to check basic \
family-relevant location signals; some claims still need your own verification.
2. What was checked: geo_resolver resolved the address, then nearby_services looked \
up nearby amenities. price_signal was not needed for this family-suitability question.
3. Supported claims: The address resolved successfully, and nearby_services returned \
amenity data for the resolved location (see limitations for what that data does and \
does not prove).
4. Partially supported or unclear claims: "Close to parks and schools" and "beautiful \
and bright" are the seller's own descriptions and were not independently verified by \
any tool - treat them as claims to confirm in person.
5. Missing information: none reported by the planner for this listing.
6. Limitations: nearby_services reports straight-line distance only, not walking time; \
this is not a professional appraisal or legal opinion.
7. Questions to ask the broker or seller: Can you confirm which specific parks and \
schools are nearby and their actual walking distance? Is the apartment currently \
occupied, and when could a family viewing be scheduled?
"""

_PRICE_VALIDATION_MISSING_INFO_PROMPT = (
    "Asking price is 4,200,000 ILS for a purchase listing at HaYarkon 120, Tel Aviv, "
    "3 rooms. The listing doesn't mention the size. Is this price reasonable?"
)

_PRICE_VALIDATION_MISSING_INFO_REPORT = """\
1. Overall assessment: The asking price cannot be validated yet because the listing's \
square meterage is missing.
2. What was checked: geo_resolver resolved the address. price_signal was skipped \
because sqm is required and was not provided.
3. Supported claims: The address resolved successfully.
4. Partially supported or unclear claims: The asking price cannot be assessed against \
comparable transactions without sqm - no price validity claim can be made either way.
5. Missing information: sqm.
6. Limitations: price_signal is a data-driven signal only, not a professional or \
certified appraisal, and could not run without sqm.
7. Questions to ask the broker or seller: What is the exact square meterage (sqm) of \
the apartment? Are there any recent comparable sales you can share for this building \
or street?
"""


async def _run_example(
    title: str, prompt: str, plan: TriagePlan, report: str
) -> PromptExample:
    client = FakeLLMClient()
    client.set_response(TRIAGE_PLANNER_MODULE, plan)
    client.set_response(REPORT_WRITER_MODULE, ReportOutput(report=report))
    tracer = StepTracer()
    tool_executor = LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))

    result = await run_agent_pipeline(
        prompt, llm_client=client, tool_executor=tool_executor, tracer=tracer
    )

    return PromptExample(
        title=title,
        prompt=prompt,
        full_response=result.report,
        steps=tracer.get_steps(),
    )


async def build_prompt_examples() -> list[PromptExample]:
    """Build the fixed set of `/api/agent_info` prompt examples."""
    family_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_200_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="family_suitability",
        claims=[
            Claim(text="close to parks and schools", type="factual", category="location"),
            Claim(text="beautiful and bright", type="subjective", category="condition"),
        ],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": KNOWN_ADDRESS}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    price_missing_sqm_plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=4_200_000,
            rooms=3,
            sqm=None,
        ),
        buyer_goal="price_validation",
        missing_information=["sqm"],
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={"address": KNOWN_ADDRESS}),
            ToolCall(tool="price_signal", arguments={}),
        ],
    )

    return [
        await _run_example(
            "Family suitability",
            _FAMILY_SUITABILITY_PROMPT,
            family_plan,
            _FAMILY_SUITABILITY_REPORT,
        ),
        await _run_example(
            "Price validation with missing information",
            _PRICE_VALIDATION_MISSING_INFO_PROMPT,
            price_missing_sqm_plan,
            _PRICE_VALIDATION_MISSING_INFO_REPORT,
        ),
    ]
