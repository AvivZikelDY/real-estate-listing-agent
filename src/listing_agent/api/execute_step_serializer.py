"""API-boundary serializer: internal `StepTrace` -> public `ExecuteStep`.

This is the one place that reduces the rich internal `StepTracer` output
(which also records `tool` and `deterministic` steps, used to build the
Evidence Bundle) down to the public `POST /api/execute` step contract:
only the LLM-module calls, in call order, each with exactly
`{module, prompt, response}`. The internal `StepTracer`/`StepTrace` shape
is never modified by this module.
"""

from typing import Any

from src.listing_agent.api.schemas import ExecuteStep, StepTrace


def _serialize_llm_step(trace: StepTrace) -> ExecuteStep:
    """Reduce one internal `llm`-typed `StepTrace` to the public shape.

    Normal completed calls (`StepTracer.call`) store
    `detail = {"prompt": {...}, "response": {...}}`; a failed call stores
    `detail = {"prompt": {...}, "error": "..."}` instead. Bounded repair
    attempts (`StepTracer.record_llm_repair_step`) store a differently
    shaped detail (`system_prompt`/`invalid_response`/`outcome`, no normal
    response) - handled generically below so a real repair call still
    surfaces without inventing a new public field.
    """
    detail: dict[str, Any] = trace.detail or {}

    if "prompt" in detail:
        prompt: dict[str, Any] = detail["prompt"]
        remainder = {key: value for key, value in detail.items() if key != "prompt"}
    else:
        prompt = {"system_prompt": detail.get("system_prompt", "")}
        remainder = {key: value for key, value in detail.items() if key != "system_prompt"}

    if trace.status == "ok":
        response: dict[str, Any] = remainder.get("response", remainder)
    else:
        error = remainder.get("error") or remainder.get("outcome") or "Unknown error"
        response = {"error": error}

    return ExecuteStep(module=trace.step, prompt=prompt, response=response)


def build_public_steps(traces: list[StepTrace]) -> list[ExecuteStep]:
    """Build the public `/api/execute` `steps` list from internal traces.

    Only `llm`-typed steps are surfaced - `tool` and `deterministic` steps
    never leak into the public response. Order is preserved exactly as
    recorded.
    """
    return [_serialize_llm_step(trace) for trace in traces if trace.type == "llm"]
