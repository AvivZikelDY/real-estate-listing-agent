"""The `report_writer` LLM module.

Given the compact Evidence Bundle built by `evidence_bundle_builder`,
produces the final buyer-facing triage report. This module only calls the
LLM (via dependency-injected `LLMClient`, through `StepTracer`) — it never
calls a tool or the plan_validator, and it never sees anything beyond the
compact Evidence Bundle it is handed.
"""

from src.listing_agent.agent.module_names import REPORT_WRITER
from src.listing_agent.agent.schemas import EvidenceBundle
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.llm.client.base import LLMClient
from src.listing_agent.llm.report_writer.bundle import build_compact_evidence_bundle
from src.listing_agent.llm.report_writer.prompts import REPORT_WRITER_SYSTEM_PROMPT
from src.listing_agent.llm.report_writer.schemas import ReportOutput

# Must match exactly: this is both the StepTrace `step` name and the module
# name FakeLLMClient/LLModClient are called with.
MODULE_NAME = REPORT_WRITER


async def run_report_writer(
    bundle: EvidenceBundle,
    client: LLMClient,
    tracer: StepTracer,
) -> str:
    """Run the report_writer LLM module and return the final report text.

    Args:
        bundle: The Evidence Bundle produced by `evidence_bundle_builder`.
        client: The injected `LLMClient` implementation to call.
        tracer: The `StepTracer` used to record this LLM call.
    """
    user_payload = build_compact_evidence_bundle(bundle)
    result = await tracer.call(
        client=client,
        module=MODULE_NAME,
        system_prompt=REPORT_WRITER_SYSTEM_PROMPT,
        user_payload=user_payload,
        response_model=ReportOutput,
    )
    return result.report
