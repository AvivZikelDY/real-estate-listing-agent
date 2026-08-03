---
applyTo: "**"
---

# General Project Constraints (all phases/tasks)

These constraints apply to every future change made to this repository,
in addition to `.github/copilot-instructions.md` (architecture, tech
stack, and hard prohibitions). Where the two overlap, both must be
satisfied; neither overrides the other.

## Before making changes

- This is an existing Python 3.12 FastAPI AI-agent project — inspect the
  repository before making changes. Do not assume structure; verify it.
- Reuse existing models, services, naming conventions, and
  dependency-injection patterns. Match the style already established in
  the codebase rather than introducing a new one.
- Do not redesign the existing workflow
  (`triage_planner` → `plan_validator` → `evidence_collector` →
  `evidence_bundle_builder` → `report_writer`, via `agent_orchestrator`).

## Scope boundaries

- Do not add extra LLM calls beyond the two frozen modules
  (`triage_planner`, `report_writer`).
- Do not add a multi-agent architecture.
- Do not implement features outside the current project scope (see
  `docs/scope.md`). If a request would require expanding scope, flag it
  instead of implementing a workaround.
- Preserve the exact public API contracts (`GET /api/team_info`,
  `GET /api/agent_info`, `GET /api/model_architecture`,
  `POST /api/execute`, `GET /`; the four-field `/api/execute` response
  shape). Do not add, remove, or rename top-level fields without
  explicit instruction.

## Secrets and simplicity

- Do not expose secrets in logs, traces, responses, tests, or
  documentation. Never write API keys, tokens, or auth headers into
  `StepTracer` output, test fixtures, or committed files.
- Keep the implementation simple and appropriate for a university
  project — prefer straightforward code over clever abstractions.

## Verification requirements

- All existing tests must continue passing.
- Ruff must remain clean.
- Add focused tests for every new behavior introduced.

## Reporting

At the end of every task, report:

1. Changed files.
2. Architectural decisions made.
3. Test results (counts, pass/fail, skipped).
4. Remaining limitations.

## Stopping

- Do not continue to the next phase automatically. Stop and wait for the
  next explicit instruction once the requested task is complete.
