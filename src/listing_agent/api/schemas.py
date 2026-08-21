"""Pydantic v2 schemas for the public HTTP API.

These models define the exact request/response shapes for the FastAPI
routes. See `docs/contracts.md` for the authoritative contract.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

StepType = Literal["llm", "tool", "deterministic"]
StepStatus = Literal["ok", "error"]


class StepTrace(BaseModel):
    """A single recorded step of an `/api/execute` run."""

    step: str
    type: StepType
    status: StepStatus
    detail: dict[str, Any] | None = None


class ExecuteRequest(BaseModel):
    """Request body for `POST /api/execute`."""

    prompt: str = Field(min_length=1)
    # Optional (Phase 2): enables stateful follow-up questions about the
    # same listing within one conversation. Absent/null preserves the
    # exact original stateless behavior - no context lookup, no reuse, no
    # save. An unknown id is simply treated as a brand-new conversation
    # (never an error - no authentication, no ownership).
    conversation_id: str | None = Field(default=None, max_length=128)

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be empty or blank")
        return value

    @field_validator("conversation_id")
    @classmethod
    def conversation_id_blank_means_absent(cls, value: str | None) -> str | None:
        """Normalize a blank/whitespace-only id to `None` - never treated
        as a conversation literally named `""`."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ExecuteStep(BaseModel):
    """A single public LLM-module step of a `POST /api/execute` run.

    This is the API-boundary serialization of the internal `StepTrace`
    (see `agent.step_tracer`) - only the LLM-module calls, in call order,
    reduced to exactly these three fields. The richer internal tracer
    (which also records tool/deterministic steps and secret-redaction)
    is unchanged and stays internal.
    """

    module: str
    prompt: dict[str, Any]
    response: dict[str, Any]


class ExecuteResponse(BaseModel):
    """Response body for `POST /api/execute`.

    Must always contain exactly these four top-level fields, regardless of
    success or failure.
    """

    status: Literal["ok", "error"]
    error: str | None = None
    response: Any | None = None
    steps: list[ExecuteStep] = Field(default_factory=list)


class Student(BaseModel):
    """A single student entry in `/api/team_info`."""

    name: str
    email: str


class TeamInfoResponse(BaseModel):
    """Response body for `GET /api/team_info`."""

    group_batch_order_number: str
    team_name: str
    students: list[Student]


class PromptExample(BaseModel):
    """One illustrative end-to-end example for `GET /api/agent_info`.

    `full_response` and `steps` are produced by actually running the agent
    pipeline (offline, via `FakeLLMClient`) against a fixed, representative
    prompt. `steps` is reduced to the exact same public `ExecuteStep`
    shape `/api/execute` returns, via the shared serializer - so it always
    reflects the real, public shape, never the richer internal `StepTrace`.
    """

    prompt: str
    full_response: str
    steps: list[ExecuteStep]


class PromptTemplate(BaseModel):
    """User-facing guidance on how to use the agent.

    This is NOT the internal `triage_planner`/`report_writer` system
    prompts - those remain internal implementation details and must never
    be exposed here.
    """

    template: str


class AgentInfoResponse(BaseModel):
    """Response body for `GET /api/agent_info`."""

    agent_name: str
    description: str
    version: str
    purpose: str
    prompt_template: PromptTemplate
    prompt_examples: list[PromptExample]
