"""Stable module/tool name constants (Phase 16).

Single source of truth for every name that appears in a `StepTrace.step`,
in `docs/contracts.md`, in `scripts/generate_architecture_image.py`, and in
`GET /api/agent_info` — so all of these stay in lockstep instead of each
hardcoding its own copy of the same string. Existing per-module
`MODULE_NAME` constants (e.g. `llm/triage_planner/planner.py`) now simply
alias the value defined here.
"""

from typing import Final

# --- LLM modules (exactly two, per project scope) ---------------------------
TRIAGE_PLANNER: Final = "triage_planner"
REPORT_WRITER: Final = "report_writer"

# --- deterministic modules ----------------------------------------------------
AGENT_ORCHESTRATOR: Final = "agent_orchestrator"
PLAN_VALIDATOR: Final = "plan_validator"
EVIDENCE_COLLECTOR: Final = "evidence_collector"
EVIDENCE_BUNDLE_BUILDER: Final = "evidence_bundle_builder"
STEP_TRACER: Final = "step_tracer"
CONTEXT_REUSE: Final = "context_reuse"

# --- tools --------------------------------------------------------------------
GEO_RESOLVER: Final = "geo_resolver"
NEARBY_SERVICES: Final = "nearby_services"
PRICE_SIGNAL: Final = "price_signal"

# All names that may legitimately appear as a `StepTrace.step` value.
ALL_MODULE_NAMES: Final = frozenset(
    {
        TRIAGE_PLANNER,
        REPORT_WRITER,
        AGENT_ORCHESTRATOR,
        PLAN_VALIDATOR,
        EVIDENCE_COLLECTOR,
        EVIDENCE_BUNDLE_BUILDER,
        STEP_TRACER,
        CONTEXT_REUSE,
        GEO_RESOLVER,
        NEARBY_SERVICES,
        PRICE_SIGNAL,
    }
)
