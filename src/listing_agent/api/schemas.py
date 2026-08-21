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


class ExecuteResponse(BaseModel):
    """Response body for `POST /api/execute`.

    Must always contain exactly these four top-level fields, regardless of
    success or failure.
    """

    status: Literal["ok", "error"]
    error: str | None = None
    response: Any | None = None
    steps: list[StepTrace] = Field(default_factory=list)


class TeamMember(BaseModel):
    """A single team member entry in `/api/team_info`."""

    name: str
    role: str
    email: str | None = None


class TeamInfoResponse(BaseModel):
    """Response body for `GET /api/team_info`."""

    batch: str
    order: str
    team_name: str
    members: list[TeamMember]


class PromptExample(BaseModel):
    """One illustrative end-to-end example for `GET /api/agent_info`.

    `full_response` and `steps` are produced by actually running the agent
    pipeline (offline, via `FakeLLMClient`) against a fixed, representative
    prompt — so they always reflect the real shape `/api/execute` returns.
    """

    title: str
    prompt: str
    full_response: str
    steps: list[StepTrace]


class AgentInfoResponse(BaseModel):
    """Response body for `GET /api/agent_info`."""

    agent_name: str
    description: str
    version: str
    purpose: str
    prompt_templates: dict[str, str]
    prompt_examples: list[PromptExample]
