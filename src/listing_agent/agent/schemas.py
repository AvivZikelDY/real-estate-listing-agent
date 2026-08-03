"""Pydantic v2 schemas shared by the deterministic agent modules.

Covers the planning models produced/consumed by `triage_planner` and
`plan_validator`, and the evidence models produced by `evidence_collector`
and `evidence_bundle_builder`. See `docs/contracts.md` §2-3.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# The fixed tool whitelist. `plan_validator` must reject any tool name not
# in this list; using a Literal here makes that rejection automatic via
# Pydantic validation.
ToolName = Literal["geo_resolver", "nearby_services", "price_signal"]

# The kind of listing detected from the user's prompt.
ListingType = Literal["purchase", "rental", "unknown"]

# What the buyer is actually trying to get out of this triage.
BuyerGoal = Literal[
    "fast_claim_triage",
    "family_suitability",
    "price_validation",
    "nearby_services_check",
    "general_question",
]

# --- planning ---------------------------------------------------------------


class PropertyDetails(BaseModel):
    """Listing facts extracted from the user's prompt.

    Any fact the prompt does not provide is left as `None` and must be
    reflected in `TriagePlan.missing_information` instead of being guessed.
    """

    listing_type: ListingType = "unknown"
    raw_address: str | None = None
    asking_price: float | None = None
    rooms: float | None = None
    sqm: float | None = None


class ClaimSource(StrEnum):
    """Where a `Claim` originated from (Phase 19).

    Not populated by `triage_planner` today (its schema/prompt are
    unchanged) - only ever set deterministically by `plan_validator` when
    it synthesizes a claim itself (`DERIVED`), so downstream consumers
    (Evidence Bundle, `report_writer`) can tell a claim the *system*
    derived (e.g. a comparative claim implied by a buyer question, never
    asserted by the seller) apart from an actual listing/seller claim.
    `LISTING`/`USER_QUESTION` are reserved for future use if `triage_planner`
    ever gains a way to tag claim provenance itself; `None` (unset) simply
    means "unknown/not tracked", never "listing" by default - avoids
    silently mislabeling a claim's source.
    """

    LISTING = "listing"
    USER_QUESTION = "user_question"
    DERIVED = "derived"


class Claim(BaseModel):
    """A claim made about the listing that the plan has classified.

    Subjective claims (e.g. "beautiful", "charming") must never, by
    themselves, trigger a tool call.
    """

    # Stable identifier used by `evidence_mapper` to link a tool's evidence
    # back to the claim it is relevant to. Assigned deterministically by
    # `plan_validator` (as "claim-{index}") if the planner did not set one
    # itself - never guessed or reused across a different plan/run.
    id: str = ""
    text: str
    type: Literal["factual", "subjective"]
    category: str | None = None
    # Optional provenance (Phase 19). `None` for every claim `triage_planner`
    # itself produces (unknown/not tracked) - only set when
    # `plan_validator` synthesizes a claim that was never actually stated by
    # the seller (see `ClaimSource.DERIVED`), so it is never confused with a
    # real seller/listing claim.
    source: ClaimSource | None = None


class ToolCall(BaseModel):
    """A single *proposed* call to one of the whitelisted tools.

    The planner only proposes calls; it must never claim a tool has already
    run or verified anything.
    """

    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Optional, explicit tool-call -> claim linkage. Not currently
    # populated by `triage_planner` (its schema is frozen), but a
    # forward-compatible hook: `evidence_mapper.find_relevant_claim_ids`
    # treats this as the highest-priority (tier 1) source of truth over
    # its deterministic category-based heuristics (tiers 2-3) - only when
    # it is actually present and references real claim ids, never guessed.
    supports_claim_ids: list[str] | None = None


class TriagePlan(BaseModel):
    """The plan produced by `triage_planner` and checked by `plan_validator`."""

    property_details: PropertyDetails
    buyer_goal: BuyerGoal
    claims: list[Claim] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)


# --- evidence ----------------------------------------------------------------


class ToolEvidence(BaseModel):
    """The result of executing (or attempting to execute) a single planned tool call."""

    tool: ToolName
    status: Literal["ok", "error", "skipped"]
    output: dict[str, Any] | None = None
    error: str | None = None
    # The typed `ToolErrorCode` value (see
    # `src/listing_agent/agent/tool_executor/errors.py`), stored as its
    # plain string value to avoid this leaf schema module depending on the
    # `tool_executor` package. `None` unless `status == "error"`.
    error_code: str | None = None
    reason: str | None = None

    # --- Phase 15: normalized evidence-mapping fields ------------------------
    # Populated by `agent.evidence_mapper.map_tool_evidence`. Kept on the same
    # `ToolEvidence` model (rather than a new, parallel model) so the
    # internal `EvidenceBundle` and the compact payload sent to
    # `report_writer` both stay a single, non-duplicated shape.

    # The `Claim.id` this evidence is relevant to, if a claim in the plan
    # matches this tool (see `evidence_mapper`). `None` when no claim in the
    # plan is a plausible match - evidence is not forced onto an unrelated
    # claim. Kept for backward compatibility; equals `claim_ids[0]` when
    # `claim_ids` is non-empty. Prefer `claim_ids` for anything new.
    claim_id: str | None = None
    # --- Phase 17: shared evidence across claims -----------------------------
    # All claim ids this single tool result is relevant to (a deduplicated
    # tool call may legitimately support more than one claim, e.g. one
    # nearby_services result backing both a "near a good school" claim and
    # a "quiet area" claim). Empty when no claim in the plan is a plausible
    # match.
    claim_ids: list[str] = Field(default_factory=list)
    # Grounded, structured facts extracted from the tool's typed output -
    # never an inferred conclusion (e.g. never "family_friendly": true,
    # never "is_bargain": true). `None` when the tool did not produce a
    # successful output (error/skipped).
    observations: dict[str, Any] | None = None
    # The tool's own source identifier (e.g. "mock-geo-dataset-v1"), when
    # the underlying output declares one. `None` for nearby_services (no
    # single source - see each `NearbyPlace.source` instead) and for
    # error/skipped results.
    source: str | None = None
    # Per-evidence-item limitations, already including the tool's own
    # disclaimers (e.g. straight-line-only distances, appraisal caveats)
    # plus a clear, grounded caveat for any non-"ok" or partial-confidence
    # result. `evidence_bundle_builder` simply concatenates these across
    # all tool results - no wording logic is duplicated there.
    limitations: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    """The complete record of one triage run's planning + evidence collection.

    This is an internal artifact — it has no final narrative report of its
    own. `report_writer` consumes a compact projection of it to produce the
    user-facing triage report.
    """

    request_id: str
    generated_at: datetime
    prompt: str
    plan: TriagePlan
    executed_tools: list[str] = Field(default_factory=list)
    tool_results: list[ToolEvidence] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """The full internal result of one `agent_orchestrator` run.

    `bundle` is the internal Evidence Bundle; `report` is the buyer-facing
    text produced by `report_writer` from it. `POST /api/execute` exposes
    only `report` (as `response`) plus the LLM-module `steps`.
    """

    bundle: EvidenceBundle
    report: str
