import pytest
from pydantic import BaseModel

from src.listing_agent.tools.registry import ToolRegistry
from src.listing_agent.tools.schemas import GeoResolverInput, ToolResult


class _EchoOutput(BaseModel):
    ok: bool = True


class _EchoTool:
    name = "echo_tool"

    async def execute(self, input_model: BaseModel) -> ToolResult[_EchoOutput]:
        return ToolResult(status="ok", data=_EchoOutput())


class _ExplodingTool:
    """A tool that raises instead of returning a structured failure.

    Used to verify `ToolRegistry.execute` still never lets an exception
    escape, even if a misbehaving tool implementation does.
    """

    name = "exploding_tool"

    async def execute(self, input_model: BaseModel) -> ToolResult:
        raise RuntimeError("boom")


def test_register_and_get_tool_by_exact_name() -> None:
    registry = ToolRegistry()
    tool = _EchoTool()

    registry.register(tool)

    assert registry.get("echo_tool") is tool


def test_register_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_EchoTool())


def test_get_rejects_unknown_tool_name() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    with pytest.raises(KeyError, match="Unknown tool"):
        registry.get("does_not_exist")


@pytest.mark.asyncio
async def test_execute_rejects_unknown_tool_name() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="Unknown tool"):
        await registry.execute("does_not_exist", GeoResolverInput(address="anywhere"))


@pytest.mark.asyncio
async def test_execute_runs_registered_tool_through_shared_interface() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    result = await registry.execute("echo_tool", GeoResolverInput(address="anywhere"))

    assert result.status == "ok"
    assert result.data.ok is True


@pytest.mark.asyncio
async def test_execute_converts_unhandled_tool_exception_into_structured_error() -> None:
    registry = ToolRegistry()
    registry.register(_ExplodingTool())

    result = await registry.execute("exploding_tool", GeoResolverInput(address="anywhere"))

    assert result.status == "error"
    assert "boom" in result.error
