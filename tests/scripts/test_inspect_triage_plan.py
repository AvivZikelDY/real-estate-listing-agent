"""Tests for `scripts/inspect_triage_plan.py`.

Covers: stopping before any tool execution, real/fake LLM client
selection, safe failure without real-LLM credentials, all five required
output sections, claim classification (subjective vs. missing-information
vs. rejected), tool-call deduplication ("merged_from_duplicates"), no
fabricated dependent coordinates, JSON-mode validity, and secret
redaction.

All tests run fully offline: `--real-llm` tests monkeypatch
`run_triage_planner` itself, so `LLModClient.generate_json` (the only
network-calling code path) is never actually invoked.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

import scripts.inspect_triage_plan as script
from scripts.triage_plan_examples import EXAMPLE_PLANS
from src.listing_agent.config import Settings
from src.listing_agent.llm.client.fake import FakeLLMClient
from src.listing_agent.llm.client.llmod import LLModClient


def _fake_settings(**overrides: object) -> Settings:
    base = {
        "llm_mode": "fake",
        "llmod_api_key": "",
        "llmod_base_url": "",
        "llmod_model": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- 1 & 2: stops before any tool execution / no tool implementation called ---


def test_module_never_imports_execution_or_downstream_modules() -> None:
    """The script must never import tool execution or downstream (Phase
    15/'report_writer') modules - only `triage_planner`, `plan_validator`,
    and the shared `tool_input_builder` helper."""
    source = script.__file__
    with open(source, encoding="utf-8") as f:
        import_lines = [line for line in f if line.lstrip().startswith(("import ", "from "))]
    import_text = "".join(import_lines)

    for forbidden in (
        "LocalToolExecutor",
        "McpToolExecutor",
        "evidence_collector",
        "evidence_bundle_builder",
        "report_writer",
        "ToolRegistry",
    ):
        assert forbidden not in import_text, (
            f"scripts/inspect_triage_plan.py must not import {forbidden}"
        )


@pytest.mark.asyncio
async def test_stops_before_tool_execution() -> None:
    """Running the full diagnostic report never calls any `ToolExecutor`,
    even when local tool execution machinery is patched to raise if used."""
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[1])
    tracer = script.StepTracer()
    settings = _fake_settings()

    with (
        patch(
            "src.listing_agent.agent.tool_executor.LocalToolExecutor.execute",
            new=AsyncMock(side_effect=AssertionError("tool executor must never be called")),
        ),
    ):
        report = await script.build_diagnostic_report(
            "test listing", client, tracer, settings, "fake"
        )

    assert report["prepared_tool_inputs"], "sanity check: pipeline actually ran"


# --- 3, 4, 5: LLM client selection ---


def test_real_mode_uses_llmod_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        script,
        "get_settings",
        lambda: _fake_settings(
            llmod_api_key="key", llmod_base_url="https://example.invalid", llmod_model="m"
        ),
    )
    captured: dict[str, object] = {}

    async def fake_planner(prompt: str, client: object, tracer: object) -> object:
        captured["client"] = client
        return EXAMPLE_PLANS[3]

    monkeypatch.setattr(script, "run_triage_planner", fake_planner)

    exit_code = script.main(["--real-llm", "--listing", "any text"])

    assert exit_code == 0
    assert isinstance(captured["client"], LLModClient)


def test_fake_mode_uses_fake_client(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = script.main(["--fake-llm", "--example", "1"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1. EXTRACTED LISTING FACTS" in out


def test_real_mode_without_credentials_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "get_settings", lambda: _fake_settings())

    exit_code = script.main(["--real-llm", "--listing", "any text"])

    assert exit_code == 2


def test_fake_mode_requires_example_flag() -> None:
    exit_code = script.main(["--fake-llm", "--listing", "any text"])

    assert exit_code == 2


# --- 6: all five sections present ---


@pytest.mark.asyncio
async def test_all_five_sections_present() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[1])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report("test", client, tracer, _fake_settings(), "fake")

    for key in (
        "extracted_listing_facts",
        "identified_claims",
        "claims_not_sent_to_tools",
        "final_tool_plan",
        "prepared_tool_inputs",
        "diagnostics",
    ):
        assert key in report

    text = script.render_text(report)
    for header in (
        "1. EXTRACTED LISTING FACTS",
        "2. IDENTIFIED CLAIMS",
        "3. CLAIMS NOT SENT TO TOOLS",
        "4. FINAL TOOL PLAN",
        "5. PREPARED TOOL INPUTS",
    ):
        assert header in text


# --- 7 & 8: claim classification ---


@pytest.mark.asyncio
async def test_subjective_claims_marked_not_sent() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[3])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report(
        "subjective only", client, tracer, _fake_settings(), "fake"
    )

    reasons = {c["reason"] for c in report["claims_not_sent_to_tools"]}
    assert reasons == {"subjective_marketing_claim"}
    assert not report["final_tool_plan"]


@pytest.mark.asyncio
async def test_missing_information_distinguished_from_subjective() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[2])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report(
        "missing info", client, tracer, _fake_settings(), "fake"
    )

    by_reason = {c["reason"] for c in report["claims_not_sent_to_tools"]}
    assert "missing_required_information" in by_reason
    assert "subjective_marketing_claim" not in by_reason


# --- 9: duplicate nearby_services calls merged ---


@pytest.mark.asyncio
async def test_duplicate_nearby_services_calls_merged() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[1])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report("test", client, tracer, _fake_settings(), "fake")

    nearby_entries = [e for e in report["final_tool_plan"] if e["tool_name"] == "nearby_services"]
    assert len(nearby_entries) == 1
    assert nearby_entries[0]["merged_from_duplicates"] is True
    merged_categories = nearby_entries[0]["planner_arguments"]["categories"]
    assert merged_categories == ["school", "kindergarten", "park"]
    assert report["diagnostics"]["planned_tool_calls_before_deduplication"] == 5
    assert report["diagnostics"]["planned_tool_calls_after_deduplication"] == 3


# --- 10: dependent coordinates never fabricated ---


@pytest.mark.asyncio
async def test_dependent_coordinates_not_fabricated() -> None:
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[1])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report("test", client, tracer, _fake_settings(), "fake")

    nearby = next(e for e in report["prepared_tool_inputs"] if e["tool_name"] == "nearby_services")
    assert nearby["resolved"] is False
    assert nearby["value"]["lat"] == {"from_call": "tool-call-1", "field": "lat"}
    assert nearby["value"]["lon"] == {"from_call": "tool-call-1", "field": "lon"}


# --- 11: JSON mode ---


def test_json_mode_valid(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = script.main(["--fake-llm", "--example", "1", "--json"])

    assert exit_code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert set(parsed.keys()) == {
        "extracted_listing_facts",
        "identified_claims",
        "claims_not_sent_to_tools",
        "final_tool_plan",
        "prepared_tool_inputs",
        "diagnostics",
    }


# --- 12: secrets redacted ---


def test_secrets_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "super-secret-llmod-key-value"
    monkeypatch.setattr(
        script,
        "get_settings",
        lambda: _fake_settings(
            llmod_api_key=secret, llmod_base_url="https://example.invalid", llmod_model="m"
        ),
    )

    async def fake_planner(prompt: str, client: object, tracer: object) -> object:
        return EXAMPLE_PLANS[1]

    monkeypatch.setattr(script, "run_triage_planner", fake_planner)

    exit_code = script.main(["--real-llm", "--listing", "any text", "--json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


# --- 13: claim original text is not silently mutated ---


@pytest.mark.asyncio
async def test_claim_original_text_not_mutated_by_deterministic_processing() -> None:
    """Guards against re-introducing the observed real-LLM-run bug where a
    downstream step further altered claim text (e.g. "בתי ספר" ->
    "בית ספיר"). Deterministic code (plan_validator, the diagnostic
    classification/report building) must only ever read `claim.text`."""
    client = FakeLLMClient()
    client.set_response("triage_planner", EXAMPLE_PLANS[1])
    tracer = script.StepTracer()
    report = await script.build_diagnostic_report("test", client, tracer, _fake_settings(), "fake")

    original_texts = {claim.text for claim in EXAMPLE_PLANS[1].claims}
    reported_texts = {claim["text"] for claim in report["identified_claims"]}
    assert reported_texts == original_texts
