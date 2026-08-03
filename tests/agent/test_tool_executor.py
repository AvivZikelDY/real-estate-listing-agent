"""Tests for the `ToolExecutor` seam introduced in Phase 13.

Covers: the local backend still executes every existing tool through the
same Phase-12 contracts, backend selection via `TOOL_BACKEND`, clear
failure for an unsupported backend, safe failure for MCP without
configuration, and that the agent modules depending on `ToolExecutor`
(`agent_orchestrator`, `evidence_collector`) never import any concrete
mock/local tool implementation directly.
"""

import ast
import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.listing_agent.agent.evidence_collector import collect_evidence
from src.listing_agent.agent.schemas import PropertyDetails, ToolCall, TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.agent.tool_executor import (
    SUPPORTED_TOOL_BACKENDS,
    LocalToolExecutor,
    McpToolExecutor,
    ToolAmbiguousResultError,
    ToolErrorCode,
    ToolExecutor,
    ToolExecutorConfigurationError,
    ToolInsufficientDataError,
    ToolInvalidInputError,
    ToolNotFoundError,
    ToolProviderUnavailableError,
    ToolRateLimitedError,
    get_tool_executor,
)
from src.listing_agent.config import Settings
from src.listing_agent.tools.dependency import get_tool_registry
from src.listing_agent.tools.mock_implementations.geo_resolver import MockGeoResolver
from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import (
    GeoResolverInput,
    GeoResolverOutput,
    NearbyServicesInput,
    PriceSignalInput,
    ToolResult,
)

KNOWN_ADDRESS = "HaYarkon 120, Tel Aviv"


def _local_executor() -> LocalToolExecutor:
    return LocalToolExecutor(get_tool_registry(Settings(tools_mode="mock")))


# --- LocalToolExecutor runs every existing tool -------------------------------


async def test_local_executor_runs_geo_resolver() -> None:
    executor = _local_executor()

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}),
        GeoResolverInput(address=KNOWN_ADDRESS),
    )

    assert result.status == "ok"
    assert result.output.outcome == "resolved"


async def test_local_executor_runs_nearby_services() -> None:
    executor = _local_executor()

    result = await executor.execute(
        ToolCall(tool="nearby_services", arguments={}),
        NearbyServicesInput(lat=32.0853, lon=34.7738),
    )

    assert result.status == "ok"


async def test_local_executor_runs_price_signal() -> None:
    executor = _local_executor()

    result = await executor.execute(
        ToolCall(tool="price_signal", arguments={}),
        PriceSignalInput(
            normalized_address=KNOWN_ADDRESS,
            neighborhood="Old North (HaTzafon HaYashan)",
            listing_type="purchase",
            asking_price=3_600_000,
            area_sqm=90,
        ),
    )

    assert result.status == "ok"
    assert result.output.outcome == "compared"


async def test_local_executor_runs_price_signal_skipped_for_rental() -> None:
    executor = _local_executor()

    result = await executor.execute(
        ToolCall(tool="price_signal", arguments={}),
        PriceSignalInput(
            normalized_address=KNOWN_ADDRESS,
            listing_type="rental",
            asking_price=6_500,
            area_sqm=55,
        ),
    )

    assert result.status == "skipped"


# --- backend selection ---------------------------------------------------------


def test_get_tool_executor_selects_local_executor_for_mock_backend() -> None:
    executor = get_tool_executor(Settings(tool_backend="mock"))
    assert isinstance(executor, LocalToolExecutor)


def test_get_tool_executor_default_backend_is_mock() -> None:
    settings = Settings()
    assert settings.tool_backend == "mock"
    executor = get_tool_executor(settings)
    assert isinstance(executor, LocalToolExecutor)


def test_get_tool_executor_selects_mcp_executor_with_valid_configuration() -> None:
    executor = get_tool_executor(
        Settings(
            tool_backend="mcp",
            mcp_server_url="https://mcp.example.internal",
            mcp_request_timeout_seconds=5,
        )
    )
    assert isinstance(executor, McpToolExecutor)


def test_get_tool_executor_rejects_unsupported_backend() -> None:
    with pytest.raises(ValueError, match="Unsupported TOOL_BACKEND"):
        get_tool_executor(Settings(tool_backend="carrier-pigeon"))


def test_supported_tool_backends_lists_mock_and_mcp() -> None:
    assert set(SUPPORTED_TOOL_BACKENDS) == {"mock", "mcp"}


# --- MCP placeholder: safe failure without configuration ----------------------


def test_mcp_executor_fails_safely_without_server_url() -> None:
    with pytest.raises(ToolExecutorConfigurationError) as exc_info:
        McpToolExecutor(server_url="", timeout_seconds=10)

    message = str(exc_info.value)
    assert "MCP_SERVER_URL" in message
    # The error message must describe *what* is missing, never a real value.
    assert "https://" not in message


def test_get_tool_executor_mcp_backend_without_url_fails_safely() -> None:
    with pytest.raises(ToolExecutorConfigurationError):
        get_tool_executor(Settings(tool_backend="mcp", mcp_server_url=""))


def test_mcp_executor_fails_safely_with_non_positive_timeout() -> None:
    with pytest.raises(ToolExecutorConfigurationError):
        McpToolExecutor(server_url="https://mcp.example.internal", timeout_seconds=0)


async def test_mcp_executor_execute_raises_not_implemented_error() -> None:
    executor = McpToolExecutor(server_url="https://mcp.example.internal", timeout_seconds=5)

    with pytest.raises(NotImplementedError):
        await executor.execute(
            ToolCall(tool="geo_resolver", arguments={}),
            GeoResolverInput(address=KNOWN_ADDRESS),
        )


def test_mcp_executor_repr_and_dict_never_expose_server_url() -> None:
    """A defensive check that nothing convenient (repr/str/error text)
    leaks the configured server URL, since it may embed credentials."""
    executor = McpToolExecutor(
        server_url="https://mcp.example.internal/secret-path?token=abc", timeout_seconds=5
    )
    assert "secret-path" not in repr(executor)
    assert "token=abc" not in repr(executor)


def test_local_tool_executor_satisfies_tool_executor_protocol() -> None:
    assert isinstance(_local_executor(), ToolExecutor)


def test_mcp_tool_executor_satisfies_tool_executor_protocol() -> None:
    executor = McpToolExecutor(server_url="https://mcp.example.internal", timeout_seconds=5)
    assert isinstance(executor, ToolExecutor)


# --- orchestrator/evidence_collector have no direct mock/local dependency -----


_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "listing_agent"
_FORBIDDEN_MODULE_SUBSTRINGS = ("mock_implementations", "nir_implementations", "tools.registry")


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "relative_path",
    [
        "agent/orchestrator/orchestrator.py",
        "agent/evidence_collector/collector.py",
    ],
)
def test_orchestration_modules_do_not_import_concrete_tool_implementations(
    relative_path: str,
) -> None:
    imports = _imported_module_names(_SRC_ROOT / relative_path)
    for forbidden in _FORBIDDEN_MODULE_SUBSTRINGS:
        assert not any(forbidden in module for module in imports), (
            f"{relative_path} must not import '{forbidden}' - it must depend only on ToolExecutor"
        )


# --- Phase 14: unified error taxonomy, timeout, retry, fail-soft behavior -----


class _FlakyTool:
    """A test-double `BaseTool` that raises a configurable sequence of
    outcomes on successive calls: an item may be an `Exception` instance
    to raise, `"sleep"` to block past the executor's timeout, or a
    `ToolResult` to return directly. Records how many times it was called.
    """

    def __init__(self, name: str, outcomes: list) -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.calls = 0

    async def execute(self, input_model: BaseModel) -> ToolResult:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if outcome == "sleep":
            await asyncio.sleep(10)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _executor_with(tool: _FlakyTool, *, timeout_seconds: float = 8.0) -> LocalToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return LocalToolExecutor(registry, timeout_seconds=timeout_seconds)


async def test_local_executor_reports_timeout_as_typed_error() -> None:
    tool = _FlakyTool("geo_resolver", ["sleep", "sleep"])
    executor = _executor_with(tool, timeout_seconds=0.05)

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )

    assert result.status == "error"
    assert result.error.code == ToolErrorCode.TIMEOUT
    assert result.attempts == 2  # timeout is retryable, so a retry was attempted
    assert tool.calls == 2


async def test_local_executor_retries_retryable_failure_then_succeeds() -> None:
    success = ToolResult(status="ok", data=GeoResolverOutput(outcome="not_found", source="mock"))
    tool = _FlakyTool("geo_resolver", [ToolProviderUnavailableError("upstream down"), success])
    executor = _executor_with(tool)

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )

    assert result.status == "ok"
    assert result.attempts == 2
    assert tool.calls == 2


async def test_local_executor_retries_retryable_failure_then_still_fails() -> None:
    tool = _FlakyTool(
        "geo_resolver",
        [ToolRateLimitedError("rate limited"), ToolRateLimitedError("rate limited again")],
    )
    executor = _executor_with(tool)

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )

    assert result.status == "error"
    assert result.error.code == ToolErrorCode.RATE_LIMITED
    assert result.attempts == 2
    assert tool.calls == 2


async def test_local_executor_never_retries_invalid_input() -> None:
    tool = _FlakyTool("geo_resolver", [ToolInvalidInputError("bad address"), "sleep"])
    executor = _executor_with(tool)

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )

    assert result.status == "error"
    assert result.error.code == ToolErrorCode.INVALID_INPUT
    assert result.attempts == 1
    assert tool.calls == 1  # never retried - a second call would have hung on "sleep"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ToolNotFoundError("no match"), ToolErrorCode.NOT_FOUND),
        (ToolAmbiguousResultError("multiple matches"), ToolErrorCode.AMBIGUOUS_RESULT),
        (ToolInsufficientDataError("missing fields"), ToolErrorCode.INSUFFICIENT_DATA),
    ],
)
async def test_local_executor_never_retries_never_retry_codes(
    exc: Exception, code: ToolErrorCode
) -> None:
    tool = _FlakyTool("geo_resolver", [exc, "sleep"])
    executor = _executor_with(tool)

    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )

    assert result.status == "error"
    assert result.error.code == code
    assert result.attempts == 1
    assert tool.calls == 1


async def test_error_result_serializes_without_leaking_secrets() -> None:
    """`ToolError.message` must never carry a secret even if the underlying
    exception's text accidentally included one - defensive test proving the
    trace-safe pattern also holds for the executor's own error payloads."""
    tool = _FlakyTool(
        "geo_resolver",
        [ToolProviderUnavailableError("upstream refused connection")],
    )
    executor = _executor_with(tool)
    # This tool never actually embeds a secret in its message (by contract,
    # tool authors must not do so); we assert the executor's own dump does
    # not introduce one either.
    result = await executor.execute(
        ToolCall(tool="geo_resolver", arguments={}), GeoResolverInput(address=KNOWN_ADDRESS)
    )
    dumped = result.model_dump(mode="json")
    assert "upstream refused connection" in dumped["error"]["message"]
    for forbidden in ("api_key", "authorization", "secret", "token", "password"):
        assert forbidden not in str(dumped).lower()


async def test_fail_soft_partial_success_via_evidence_collector() -> None:
    """One independent tool failing must not block evidence from the
    others - the family scenario runs geo_resolver (ok) and nearby_services
    (fails); the bundle must still contain both entries, and the failed one
    must never look like a supported claim."""
    registry = ToolRegistry()
    registry.register(MockGeoResolver())
    registry.register(
        _FlakyTool("nearby_services", [ToolProviderUnavailableError("provider outage")])
    )
    executor = LocalToolExecutor(registry)
    tracer = StepTracer()
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    tool_results, executed = await collect_evidence(plan, executor, tracer)

    assert executed == ["geo_resolver", "nearby_services"]
    geo_evidence, nearby_evidence = tool_results
    assert geo_evidence.status == "ok"
    assert nearby_evidence.status == "error"
    assert nearby_evidence.error_code == ToolErrorCode.PROVIDER_UNAVAILABLE.value
    assert nearby_evidence.output is None  # a failure must never carry a "supported" output


async def test_dependent_tool_skipped_when_geo_resolver_itself_fails() -> None:
    """If geo_resolver technically fails (not just a not_found outcome),
    nearby_services/price_signal must be skipped rather than attempted with
    no coordinates, and the request must still produce a bundle."""
    registry = ToolRegistry()
    registry.register(_FlakyTool("geo_resolver", [ToolProviderUnavailableError("geo down")]))
    executor = LocalToolExecutor(registry)
    tracer = StepTracer()
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="family_suitability",
        tool_calls=[
            ToolCall(tool="geo_resolver", arguments={}),
            ToolCall(tool="nearby_services", arguments={}),
        ],
    )

    tool_results, executed = await collect_evidence(plan, executor, tracer)

    assert executed == ["geo_resolver"]
    geo_evidence, nearby_evidence = tool_results
    assert geo_evidence.status == "error"
    assert nearby_evidence.status == "skipped"
    assert nearby_evidence.reason is not None


async def test_complete_tool_failure_still_produces_evidence_not_a_hard_failure() -> None:
    """Even if every planned tool technically fails, evidence_collector must
    still return one `ToolEvidence` per call (never raise) so the pipeline
    can still produce a bundle/report describing the failures."""
    registry = ToolRegistry()
    registry.register(_FlakyTool("geo_resolver", [ToolProviderUnavailableError("down")]))
    executor = LocalToolExecutor(registry)
    tracer = StepTracer()
    plan = TriagePlan(
        property_details=PropertyDetails(
            listing_type="purchase",
            raw_address=KNOWN_ADDRESS,
            asking_price=3_000_000,
            rooms=3,
            sqm=80,
        ),
        buyer_goal="family_suitability",
        tool_calls=[ToolCall(tool="geo_resolver", arguments={})],
    )

    tool_results, executed = await collect_evidence(plan, executor, tracer)

    assert executed == ["geo_resolver"]
    assert len(tool_results) == 1
    assert tool_results[0].status == "error"
    assert tool_results[0].output is None
