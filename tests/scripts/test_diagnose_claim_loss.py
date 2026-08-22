"""Focused tests for `scripts/diagnose_claim_loss.py`.

Ensure the diagnostic instrumentation does not alter the production plan,
does not execute tools, does not call `report_writer`, redacts secrets, and
preserves raw and transformed stages accurately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.diagnose_claim_loss as diag
from src.listing_agent.agent.plan_validator.validator import validate_plan
from src.listing_agent.agent.schemas import TriagePlan

LISTING = "גונן 14\n5 חדרים, 123 מ״ר\n4,350,000 ₪\n"

RAW_LLM_JSON = json.dumps(
    {
        "property_details": {
            "listing_type": "purchase",
            "raw_address": "גונן 14",
            "asking_price": 4350000,
            "rooms": 5,
            "sqm": 123,
        },
        "buyer_goal": "fast_claim_triage",
        "claims": [
            {"text": "גונן 14", "type": "factual", "category": "address"},
            {"text": "5 חדרים", "type": "factual", "category": "rooms"},
            {"text": "123 מ״ר", "type": "factual", "category": "size_sqm"},
            {"text": "4,350,000 ₪", "type": "factual", "category": "price"},
            {
                "text": "מתחם גני ילדים מעבר לכביש",
                "type": "factual",
                "category": "nearby_amenities",
            },
            {"text": "המון חניה ברחוב", "type": "factual", "category": "nearby_amenities"},
            {"text": "נכס עם ממ״ד", "type": "factual", "category": "condition"},
            {"text": "קומה 5/6", "type": "factual", "category": "condition"},
            {
                "text": "השכרה לשנה או שנתיים ולאחר מכן רכישה",
                "type": "factual",
                "category": "condition",
            },
            {
                "text": "דירה חדשה לקראת מסירה",
                "type": "subjective",
                "category": "condition",
            },
        ],
        "missing_information": [],
        "tool_calls": [],
    },
    ensure_ascii=False,
)


def test_diagnostic_does_not_alter_the_production_plan(tmp_path: Path) -> None:
    plan_before = TriagePlan.model_validate(json.loads(RAW_LLM_JSON))
    validated_before = validate_plan(plan_before)

    diag._analyze(LISTING, RAW_LLM_JSON, tmp_path)

    plan_after = TriagePlan.model_validate(json.loads(RAW_LLM_JSON))
    validated_after = validate_plan(plan_after)

    assert plan_after.model_dump() == plan_before.model_dump()
    assert validated_after.model_dump() == validated_before.model_dump()


def test_diagnostic_does_not_execute_tools_or_call_report_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagnostic never imports/instantiates a tool executor, MCP client,
    or the report_writer module. The `_analyze` entry point runs a full stage
    breakdown from a captured raw string without any of that."""
    import src.listing_agent.agent.evidence_collector.collector as ec
    import src.listing_agent.agent.tool_executor.local as le
    import src.listing_agent.agent.tool_executor.mcp as mcp
    import src.listing_agent.llm.report_writer.writer as rw

    def _fail(*args, **kwargs):  # pragma: no cover - only reached on regression
        raise AssertionError("diagnostic must not invoke this path")

    monkeypatch.setattr(ec, "collect_evidence", _fail)
    monkeypatch.setattr(le.LocalToolExecutor, "execute", _fail)
    monkeypatch.setattr(mcp.McpToolExecutor, "execute", _fail, raising=False)
    monkeypatch.setattr(rw, "run_report_writer", _fail, raising=False)

    diag._analyze(LISTING, RAW_LLM_JSON, tmp_path)


def test_diagnostic_redacts_secrets(tmp_path: Path) -> None:
    payload = {
        "api_key": "sk-should-never-leak",
        "authorization": "Bearer nope",
        "nested": {"token": "nope", "safe": "value"},
        "list": [{"password": "nope"}, {"other": "kept"}],
    }
    redacted = diag._redact(payload)
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["authorization"] == "***REDACTED***"
    assert redacted["nested"]["token"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "value"
    assert redacted["list"][0]["password"] == "***REDACTED***"
    assert redacted["list"][1]["other"] == "kept"


def test_diagnostic_preserves_raw_and_transformed_stages(tmp_path: Path) -> None:
    result = diag._analyze(LISTING, RAW_LLM_JSON, tmp_path)

    # Raw content saved verbatim (byte-for-byte, no re-serialization loss).
    assert (tmp_path / "raw-llm-response.json").read_text(encoding="utf-8") == RAW_LLM_JSON

    # Parsed plan matches direct pydantic parsing.
    parsed_from_disk = json.loads((tmp_path / "parsed-triage-plan.json").read_text("utf-8"))
    expected_parsed = TriagePlan.model_validate(json.loads(RAW_LLM_JSON)).model_dump(mode="json")
    assert parsed_from_disk == expected_parsed

    # Before-cap has all 10 raw claims; after-cap has exactly MAX_CLAIMS (8).
    before_limit = json.loads((tmp_path / "claims-before-limit.json").read_text("utf-8"))
    after_limit = json.loads((tmp_path / "claims-after-limit.json").read_text("utf-8"))
    assert len(before_limit) == 10
    assert len(after_limit) == 8

    # Removed-by-cap correctly identifies the last two raw claims (list-order drop).
    decisions = json.loads((tmp_path / "validation-decisions.json").read_text("utf-8"))
    assert decisions["raw_json_claim_count"] == 10
    assert decisions["parsed_claim_count"] == 10
    assert decisions["capped_claim_count"] == 8
    assert len(decisions["removed_by_cap"]) == 2
    removed_texts = [c["text"] for c in decisions["removed_by_cap"]]
    assert "השכרה לשנה או שנתיים ולאחר מכן רכישה" in removed_texts
    assert "דירה חדשה לקראת מסירה" in removed_texts

    # additional_claims_truncated marker recorded, not silent.
    assert "additional_claims_truncated" in decisions["cap_recorded_missing_information"]

    # No tool calls in raw plan; the validator faithfully synthesizes
    # geo_resolver + nearby_services (Phase 19 recovery) because a
    # `nearby_amenities` claim exists with a usable address — the diagnostic
    # captures the real production behavior, it does not suppress it.
    assert decisions["raw_tool_calls"] == []
    assert {c["tool"] for c in result["validated_tool_calls"]} == {
        "geo_resolver",
        "nearby_services",
    }


def test_diagnostic_stages_are_faithful_to_production_validator(tmp_path: Path) -> None:
    diag._analyze(LISTING, RAW_LLM_JSON, tmp_path)
    decisions = json.loads((tmp_path / "validation-decisions.json").read_text("utf-8"))

    plan = TriagePlan.model_validate(json.loads(RAW_LLM_JSON))
    validated = validate_plan(plan)

    assert decisions["validated_tool_calls"] == [
        c.model_dump(mode="json") for c in validated.tool_calls
    ]
    assert decisions["validated_missing_information"] == list(validated.missing_information)
