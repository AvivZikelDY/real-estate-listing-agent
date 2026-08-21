"""Phase 16: the Nir integration boundary test.

Proves that `agent.conversation` (the context/cache layer) only ever calls
through the frozen `ToolExecutor` Protocol, sending it just the reduced
set of calls the reuse logic decided still need to run - completely
unaware of, and unaffected by, which concrete transport implements
`ToolExecutor.execute()`. Uses a fake in-memory `McpToolExecutor`-shaped
stub (never Nir's real implementations, never a real MCP call) to prove
this holds for a transport other than `LocalToolExecutor` too.
"""

from pydantic import BaseModel

from src.listing_agent.agent.conversation.cache import collect_evidence_with_context
from src.listing_agent.agent.conversation.context import ConversationContext
from src.listing_agent.agent.schemas import Claim, PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import ToolExecutor
from src.listing_agent.agent.tool_executor.result import ToolExecutionResult
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyServicesInput,
    NearbyServicesOutput,
    PriceSignalInput,
    PriceSignalOutput,
    ServiceCategorySummary,
)

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"


class FakeMcpToolExecutor:
    """A minimal stand-in "future McpToolExecutor" - structurally satisfies
    the `ToolExecutor` Protocol but has nothing to do with Nir's real
    tools/MCP server. Records every call it actually receives so the test
    can assert the context/cache layer sent it only the reduced work."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, BaseModel]] = []

    async def execute(self, call: ToolCall, input_model: BaseModel) -> ToolExecutionResult:
        self.calls.append((call.tool, input_model))
        if call.tool == "geo_resolver":
            return ToolExecutionResult(
                status="ok",
                output=GeoResolverOutput(
                    outcome="resolved",
                    normalized_address="HaYarkon St 120, Tel Aviv-Yafo",
                    lat=32.0853,
                    lon=34.7738,
                    resolution_quality="exact",
                    source="fake_mcp",
                ),
            )
        if call.tool == "nearby_services":
            assert isinstance(input_model, NearbyServicesInput)
            return ToolExecutionResult(
                status="ok",
                output=NearbyServicesOutput(
                    places=[],
                    categories=[
                        ServiceCategorySummary(category=cat, count=0, nearest_distance_m=0.0)
                        for cat in input_model.categories
                    ],
                    evidence_quality="low",
                ),
            )
        assert isinstance(input_model, PriceSignalInput)
        return ToolExecutionResult(
            status="ok",
            output=PriceSignalOutput(outcome="no_comparable_data", source="fake_mcp"),
        )


def _plan(tool_calls: list[ToolCall]) -> TriagePlan:
    return TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=4_700_000,
            rooms=4,
            sqm=90,
        ),
        buyer_goal="nearby_services_check",
        claims=[Claim(text="park?", type="factual", category="nearby_amenities")],
        tool_calls=tool_calls,
    )


async def test_context_layer_reduces_calls_identically_for_a_non_local_executor() -> None:
    executor: ToolExecutor = FakeMcpToolExecutor()
    assert isinstance(executor, ToolExecutor)  # structural Protocol check

    context = ConversationContext(conversation_id="conv-mcp")

    plan1 = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["park"]}),
        ]
    )
    await collect_evidence_with_context(
        plan1, executor, StepTracer(), context, max_total_tool_execution_seconds=20.0
    )
    assert [call for call, _ in executor.calls] == ["geo_resolver", "nearby_services"]

    # Turn 2: same address (geo reused) + a new category (school) - the
    # fake executor must receive ONLY the missing work, exactly like
    # `LocalToolExecutor` would.
    executor.calls.clear()
    plan2 = _plan(
        [
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={"categories": ["school"]}),
        ]
    )
    _, executed_tools, detail = await collect_evidence_with_context(
        plan2, executor, StepTracer(), context, max_total_tool_execution_seconds=20.0
    )

    assert [call for call, _ in executor.calls] == ["nearby_services"]
    nearby_input = executor.calls[0][1]
    assert isinstance(nearby_input, NearbyServicesInput)
    assert nearby_input.categories == ["school"]
    assert executed_tools == ["nearby_services"]
    assert "geo_resolver" in detail["reused"]


async def test_geo_input_model_type_is_unchanged_regardless_of_transport() -> None:
    """The context layer builds the exact same typed `GeoResolverInput` it
    would for `LocalToolExecutor` - the transport never sees a
    conversation-specific shape."""
    executor = FakeMcpToolExecutor()
    context = ConversationContext(conversation_id="conv-mcp-2")
    plan = _plan([ToolCall(tool="geo_resolver", arguments={})])

    await collect_evidence_with_context(
        plan, executor, StepTracer(), context, max_total_tool_execution_seconds=20.0
    )

    assert isinstance(executor.calls[0][1], GeoResolverInput)
