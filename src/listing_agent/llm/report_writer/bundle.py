"""Builds the compact Evidence Bundle payload handed to `report_writer`.

`report_writer` must see nothing beyond what `evidence_bundle_builder`
collected: this strips internal bookkeeping fields (`request_id`,
`generated_at`) that carry no report-relevant information, and passes
through exactly the data needed to ground every section of the report.

Per tool result, only the *normalized* facts produced by
`agent.evidence_mapper` (Phase 15) are sent - `observations`, `source`,
`limitations`, and the error/skip details - never the tool's raw typed
`output` (already redundant with `observations`) and never any internal
transport metadata (e.g. attempt counts, timing). This keeps the payload
compact (smaller LLM context/cost) and guarantees `report_writer` only
ever sees grounded, already-vetted facts rather than a raw provider
response it would have to interpret itself.
"""

from typing import Any

from src.listing_agent.agent.schemas import EvidenceBundle, ToolEvidence

# Internal diagnostic markers written by `plan_validator._cap_claims` into
# `TriagePlan.missing_information` (Phase: truncation observability). These
# exist so internal callers/tests can detect a claims-cap truncation by
# exact string match - they must keep flowing through the deterministic
# pipeline unchanged. But they are raw internal marker text, not something a
# buyer should ever see verbatim (Stage 4.3 finding: the LLM quoted
# "additional_claims_truncated" straight into the buyer-facing report
# despite the system prompt already forbidding it - prompt-only protection
# proved insufficient). They are stripped here, at the report_writer
# input boundary, and replaced with a single plain-language item so the
# underlying fact ("some listing details were trimmed") is not lost.
_TRUNCATION_MARKER = "additional_claims_truncated"
_TRUNCATION_DETAIL_PREFIX = "additional_claims_truncated_detail:"
_TRUNCATION_PLAIN_LANGUAGE_ITEM = (
    "some additional details in the listing were not included in this check"
)


def _sanitize_missing_information(missing_information: list[str]) -> list[str]:
    """Drop raw internal truncation marker strings, replacing them (if any
    were present) with one plain-language item. All other entries (e.g.
    real missing property fields like "sqm", "raw_address") pass through
    unchanged."""
    had_marker = False
    sanitized: list[str] = []
    for item in missing_information:
        if item == _TRUNCATION_MARKER or item.startswith(_TRUNCATION_DETAIL_PREFIX):
            had_marker = True
            continue
        sanitized.append(item)
    if had_marker and _TRUNCATION_PLAIN_LANGUAGE_ITEM not in sanitized:
        sanitized.append(_TRUNCATION_PLAIN_LANGUAGE_ITEM)
    return sanitized


def _compact_tool_result(evidence: ToolEvidence) -> dict[str, Any]:
    """Return the normalized, report_writer-facing projection of one `ToolEvidence`."""
    return {
        "tool": evidence.tool,
        "status": evidence.status,
        "claim_id": evidence.claim_id,
        "claim_ids": list(evidence.claim_ids),
        "observations": evidence.observations,
        "source": evidence.source,
        "limitations": list(evidence.limitations),
        "error": evidence.error,
        "error_code": evidence.error_code,
        "reason": evidence.reason,
    }


def build_compact_evidence_bundle(bundle: EvidenceBundle) -> dict[str, Any]:
    """Return the compact dict payload sent to `report_writer` for `bundle`."""
    return {
        "prompt": bundle.prompt,
        "property_details": bundle.plan.property_details.model_dump(mode="json"),
        "buyer_goal": bundle.plan.buyer_goal,
        "claims": [claim.model_dump(mode="json") for claim in bundle.plan.claims],
        "executed_tools": list(bundle.executed_tools),
        "tool_results": [_compact_tool_result(evidence) for evidence in bundle.tool_results],
        "missing_information": _sanitize_missing_information(list(bundle.missing_information)),
        "limitations": list(bundle.limitations),
    }
