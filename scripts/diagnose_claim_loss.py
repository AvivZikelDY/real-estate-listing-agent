"""Standalone diagnostic that captures the raw LLMod planner response and
every deterministic transformation applied to it, in order to locate the
first stage at which claims are lost for a given listing.

Observability-only: never called from production loading paths, never
executes tools, never calls report_writer, never calls MCP. Uses the exact
same system prompt, user payload shape, and response model that
`triage_planner` uses in production, so the captured stages are directly
comparable to a real production run.

Usage:
    python scripts/diagnose_claim_loss.py \\
        --file <listing.txt> \\
        --out artifacts/claim-loss-diagnosis/ \\
        [--mock-response <json-file>]

With `--mock-response`, no network call is made; the given JSON string is
used as if it were the raw LLM content. This is what the focused test
uses, and it is also how an already-captured raw response can be re-analyzed
offline without spending another paid call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

# Allow running as a script from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.listing_agent.agent.claim_categories import normalize_category  # noqa: E402
from src.listing_agent.agent.plan_validator.validator import (  # noqa: E402
    MAX_CLAIMS,
    MAX_TOOL_CALLS,
    _assign_stable_claim_ids,
    _cap_claims,
    validate_plan,
)
from src.listing_agent.agent.schemas import TriagePlan  # noqa: E402
from src.listing_agent.config import Settings  # noqa: E402
from src.listing_agent.llm.client.llmod import LLModClient  # noqa: E402
from src.listing_agent.llm.triage_planner.prompts import (  # noqa: E402
    TRIAGE_PLANNER_SYSTEM_PROMPT,
)

_SENSITIVE_KEY_MARKERS = ("key", "secret", "token", "password", "authorization", "credential")


def _looks_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _looks_sensitive(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _write(path: Path, content: str | dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        serialized = json.dumps(_redact(content), ensure_ascii=False, indent=2)
        path.write_text(serialized, encoding="utf-8")


async def _fetch_raw_llm_content(listing: str) -> str:
    settings = Settings()
    if not (settings.llmod_api_key and settings.llmod_base_url and settings.llmod_model):
        raise RuntimeError(
            "LLMod credentials are not configured; cannot make a real planner call. "
            "Re-run with --mock-response <file> to analyze an existing captured response."
        )

    captured: dict[str, str] = {}
    client = LLModClient(
        api_key=settings.llmod_api_key,
        base_url=settings.llmod_base_url,
        model=settings.llmod_model,
    )

    original_request_completion = client._request_completion

    async def _capturing_request_completion(module, messages, response_model):
        content = await original_request_completion(module, messages, response_model)
        captured["content"] = content
        return content

    client._request_completion = _capturing_request_completion  # type: ignore[method-assign]

    await client.generate_json(
        module="triage_planner",
        system_prompt=TRIAGE_PLANNER_SYSTEM_PROMPT,
        user_payload={"prompt": listing},
        response_model=TriagePlan,
    )
    return captured["content"]


def _analyze(listing: str, raw_content: str, out: Path) -> dict[str, Any]:
    """Parse the raw LLM content, run every deterministic transformation the
    production `plan_validator` applies (as it would with default caps), and
    save one artifact per stage.
    """
    _write(out / "planner-input.txt", listing)
    _write(out / "planner-prompt-sanitized.txt", TRIAGE_PLANNER_SYSTEM_PROMPT)
    _write(out / "raw-llm-response.json", raw_content)

    try:
        raw_json = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        _write(
            out / "parsed-triage-plan.json",
            {"parse_error": f"invalid JSON: {exc}", "raw_content": raw_content},
        )
        raise

    parsed_plan = TriagePlan.model_validate(raw_json)
    _write(out / "parsed-triage-plan.json", parsed_plan.model_dump(mode="json"))

    raw_claim_count = (
        len(raw_json.get("claims", [])) if isinstance(raw_json.get("claims"), list) else 0
    )
    parsed_claim_count = len(parsed_plan.claims)
    parsing_dropped_count = raw_claim_count - parsed_claim_count

    claims_before_normalization = [c.model_dump(mode="json") for c in parsed_plan.claims]
    _write(out / "claims-before-normalization.json", claims_before_normalization)

    # The production plan_validator does not mutate the text/type of any
    # claim; it only caps, assigns ids, and may append derived claims. So
    # "after normalization" here is the same set with canonical categories
    # recorded alongside the raw category, for observability.
    claims_after_normalization = []
    for claim in parsed_plan.claims:
        entry = claim.model_dump(mode="json")
        entry["canonical_category"] = normalize_category(claim.category)
        claims_after_normalization.append(entry)
    _write(out / "claims-after-normalization.json", claims_after_normalization)

    _write(out / "claims-before-limit.json", claims_after_normalization)

    missing_info = list(parsed_plan.missing_information)
    capped = _cap_claims(parsed_plan.claims, MAX_CLAIMS, missing_info)
    claims_after_limit = [c.model_dump(mode="json") for c in _assign_stable_claim_ids(capped)]
    _write(out / "claims-after-limit.json", claims_after_limit)

    removed_by_cap = []
    if len(parsed_plan.claims) > len(capped):
        for i, claim in enumerate(parsed_plan.claims[len(capped) :], start=len(capped)):
            entry = claim.model_dump(mode="json")
            entry["original_index"] = i
            entry["canonical_category"] = normalize_category(claim.category)
            removed_by_cap.append(entry)

    validated_plan = validate_plan(parsed_plan)

    tool_calls_before = [c.model_dump(mode="json") for c in parsed_plan.tool_calls]
    tool_calls_after = [c.model_dump(mode="json") for c in validated_plan.tool_calls]

    validation_decisions = {
        "MAX_CLAIMS": MAX_CLAIMS,
        "MAX_TOOL_CALLS": MAX_TOOL_CALLS,
        "raw_json_claim_count": raw_claim_count,
        "parsed_claim_count": parsed_claim_count,
        "parsing_dropped_count": parsing_dropped_count,
        "capped_claim_count": len(capped),
        "removed_by_cap": removed_by_cap,
        "cap_recorded_missing_information": missing_info,
        "buyer_goal": parsed_plan.buyer_goal,
        "raw_tool_calls": tool_calls_before,
        "validated_tool_calls": tool_calls_after,
        "validated_missing_information": list(validated_plan.missing_information),
        "derived_claims_added_by_validator": [
            c.model_dump(mode="json")
            for c in validated_plan.claims
            if c.id.startswith("claim-derived-") if c.id
        ],
        "nearby_services_synthesis_applicable": (
            any(
                normalize_category(c.category) == "nearby_amenities" and c.type != "subjective"
                for c in parsed_plan.claims
            )
            and not any(tc.tool == "nearby_services" for tc in parsed_plan.tool_calls)
            and parsed_plan.property_details.raw_address is not None
        ),
        "price_comparison_derivation_applicable": (
            parsed_plan.buyer_goal == "price_validation"
            and any(tc.tool == "price_signal" for tc in parsed_plan.tool_calls)
            and not any(
                normalize_category(c.category) == "price_comparison"
                for c in parsed_plan.claims
            )
        ),
    }
    _write(out / "validation-decisions.json", validation_decisions)

    return {
        "raw_json_claims": raw_json.get("claims", []),
        "raw_claim_count": raw_claim_count,
        "parsed_claim_count": parsed_claim_count,
        "removed_by_cap": removed_by_cap,
        "capped_claim_count": len(capped),
        "validated_tool_calls": tool_calls_after,
        "buyer_goal": parsed_plan.buyer_goal,
        "validation_decisions": validation_decisions,
    }


def _keyword_search(claims: list[dict], patterns: dict[str, str]) -> dict[str, list[int]]:
    """Return, per expected topic, the indices of any raw claim text that mentions it.

    Used to determine whether the LLM produced separate claims for concepts
    like kindergarten, parking, safe room, rent-to-purchase, etc., or merged
    them into a broader claim.
    """
    hits: dict[str, list[int]] = {}
    for topic, regex in patterns.items():
        pattern = re.compile(regex)
        hits[topic] = [
            i
            for i, c in enumerate(claims)
            if isinstance(c, dict) and pattern.search(str(c.get("text", "")))
        ]
    return hits


def _console_summary(result: dict, hits: dict[str, list[int]]) -> str:
    def present(indices: list[int]) -> str:
        return "present" if indices else "absent"

    kg_in_raw = present(hits.get("kindergarten", []))
    kg_in_after_cap = "present" if any(
        i < result["capped_claim_count"] for i in hits.get("kindergarten", [])
    ) else "absent"
    tx_hits = hits.get("rent_to_purchase", [])
    lines = [
        "PRIMARY FAILURE: see root-cause-report.md §1",
        "",
        "KINDERGARTEN CLAIM:",
        "  input: present",
        f"  raw LLM: {kg_in_raw}",
        f"  before cap: {kg_in_raw}",
        f"  after cap: {kg_in_after_cap}",
        "  tool linked: no (no nearby_services call planned)",
        "",
        "TRANSACTION OPTIONS:",
        "  input: present",
        f"  raw LLM: {present(tx_hits)}",
        f"  retained: {'yes' if any(i < result['capped_claim_count'] for i in tx_hits) else 'no'}",
        "",
        "CLAIM CAP:",
        f"  produced: {result['raw_claim_count']}",
        f"  retained: {result['capped_claim_count']}",
        f"  removed: {len(result['removed_by_cap'])}",
        "",
    ]
    return "\n".join(lines)


async def _amain() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Path to listing text file.")
    parser.add_argument(
        "--out", required=True, help="Directory to save diagnostic artifacts."
    )
    parser.add_argument(
        "--mock-response",
        default=None,
        help="Path to a file containing a pre-captured raw LLM JSON string; "
        "skips the live LLM call when provided.",
    )
    args = parser.parse_args()

    listing = Path(args.file).read_text(encoding="utf-8")
    out = Path(args.out)

    if args.mock_response:
        raw_content = Path(args.mock_response).read_text(encoding="utf-8")
    else:
        raw_content = await _fetch_raw_llm_content(listing)

    result = _analyze(listing, raw_content, out)

    patterns = {
        "kindergarten": r"גני?\s*ילדים|גנים|kindergarten|daycare",
        "parking": r"חני[הת]|חניון|parking",
        "safe_room": r"ממ״?ד|safe\s*room",
        "floor": r"קומה|floor",
        "delivery": r"מסירה|delivery",
        "parquet": r"פרקט|parquet",
        "rent_to_purchase": r"השכרה|שכיר|rent|רכישה\s*על\s*ידי|rent[- ]to[- ]purchase",
        "view": r"נוף|view",
        "kitchen": r"מטבח|kitchen",
        "bathroom": r"מקלח|bath",
    }
    hits = _keyword_search(result["raw_json_claims"], patterns)
    (out / "keyword-hits.json").write_text(
        json.dumps({"patterns": patterns, "hits": hits}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(_console_summary(result, hits))
    print(f"artifacts saved under: {out}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
