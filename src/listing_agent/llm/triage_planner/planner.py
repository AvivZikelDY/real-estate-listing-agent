"""The `triage_planner` LLM module.

Given the user's raw prompt, produces a `TriagePlan`. This module only
calls the LLM (via dependency-injected `LLMClient`, through `StepTracer`)
and returns the structured result — it does not execute tools or write the
report.
"""

from src.listing_agent.agent.module_names import TRIAGE_PLANNER
from src.listing_agent.agent.schemas import TriagePlan
from src.listing_agent.agent.step_tracer import StepTracer
from src.listing_agent.llm.client.base import LLMClient
from src.listing_agent.llm.triage_planner.prompts import TRIAGE_PLANNER_SYSTEM_PROMPT

# Must match exactly: this is both the StepTrace `step` name and the module
# name FakeLLMClient/LLModClient are called with.
MODULE_NAME = TRIAGE_PLANNER


async def run_triage_planner(
    prompt: str,
    client: LLMClient,
    tracer: StepTracer,
) -> TriagePlan:
    """Run the triage_planner LLM module and return the resulting plan.

    Args:
        prompt: The complete user input, exactly as submitted.
        client: The injected `LLMClient` implementation to call.
        tracer: The `StepTracer` used to record this LLM call.
    """
    user_payload = {"prompt": prompt}
    return await tracer.call(
        client=client,
        module=MODULE_NAME,
        system_prompt=TRIAGE_PLANNER_SYSTEM_PROMPT,
        user_payload=user_payload,
        response_model=TriagePlan,
    )
