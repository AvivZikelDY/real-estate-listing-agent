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
        "missing_information": list(bundle.missing_information),
        "limitations": list(bundle.limitations),
    }
