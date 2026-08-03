"""The deterministic `evidence_bundle_builder` module.

Assembles the final, report_writer-ready `EvidenceBundle` from the
original prompt, the validated plan, and the collected tool evidence. Pure
transformation only — no LLM calls, no tool calls.
"""

import uuid
from datetime import UTC, datetime

from src.listing_agent.agent.schemas import EvidenceBundle, ToolEvidence, TriagePlan


def build_evidence_bundle(
    prompt: str,
    plan: TriagePlan,
    tool_results: list[ToolEvidence],
    executed_tools: list[str],
    *,
    request_id: str | None = None,
    generated_at: datetime | None = None,
) -> EvidenceBundle:
    """Build the `EvidenceBundle` for one triage run.

    `limitations` is the flattened concatenation of every tool result's own
    `ToolEvidence.limitations` (see `agent.evidence_mapper`, Phase 15) — that
    mapping layer is the single place responsible for wording each outcome
    (business "no answer", technical failure, or dependency skip)
    correctly; this function does not re-derive or duplicate that wording.
    """
    limitations: list[str] = []
    for evidence in tool_results:
        limitations.extend(evidence.limitations)

    return EvidenceBundle(
        request_id=request_id or str(uuid.uuid4()),
        generated_at=generated_at or datetime.now(UTC),
        prompt=prompt,
        plan=plan,
        executed_tools=list(executed_tools),
        tool_results=list(tool_results),
        missing_information=list(plan.missing_information),
        limitations=limitations,
    )
