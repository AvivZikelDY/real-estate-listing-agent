# Project Scope — Tel Aviv Real Estate Listing Triage Agent

## Purpose

A university AI Agents course project. The agent receives a Tel Aviv real
estate listing (address + basic listing facts) and produces a triage report:
a plain-language assessment of the listing grounded in evidence gathered by
deterministic tools (location resolution, nearby services, price signal).

The project demonstrates a small, auditable agent architecture: an LLM plans
which evidence to gather, deterministic Python code executes that plan and
collects real evidence, and a second LLM writes a report strictly from that
evidence. Every step is traced and returned to the caller.

## Phase Plan

- **Phase 0 (this phase):** Documentation, contracts, ownership boundaries,
  and empty package scaffolding only. No application logic, no FastAPI app,
  no LLM calls, no tool implementations.
- **Later phases (not part of this task):** Implement the FastAPI app,
  Pydantic v2 schemas, the abstract LLM client, the two LLM modules, the
  deterministic orchestration pipeline, the GUI, tests, and deployment to
  Vercel.

## Technology Stack

- Python 3.12
- FastAPI (HTTP API layer)
- Pydantic v2 (data validation / schemas)
- httpx (outbound HTTP calls, e.g. to LLMod.ai and any external data source)
- pytest (testing)
- Ruff (linting/formatting)
- Plain HTML/CSS/JavaScript (GUI — no frontend framework/build step)
- Vercel (deployment target)
- LLMod.ai, accessed only through an abstract LLM client interface

## Normal Execution Flow (conceptual, for Phase 1+)

Normal execution invokes **exactly two** LLM modules:

1. `triage_planner` — LLM module. Given the listing input, produces a plan
   describing which deterministic tools/evidence to gather and why.
2. `report_writer` — LLM module. Given the collected evidence bundle (and
   nothing else), writes the final triage report.

Everything else in the pipeline is deterministic Python:

- `agent_orchestrator` — coordinates the end-to-end run.
- `plan_validator` — validates the plan produced by `triage_planner` before
  it is executed (schema/whitelist checks, no LLM calls).
- `evidence_collector` — executes the validated plan by calling tools.
- `evidence_bundle_builder` — assembles collected evidence into the fixed
  bundle shape that `report_writer` consumes.
- `step_tracer` — records every step of the run (plan, validation, tool
  calls, bundle, report) for the `steps` field returned to the caller.

## Tools (data-layer only, no LLM involvement)

- `geo_resolver` — resolves an address/listing location to coordinates and
  normalized area data.
- `nearby_services` — finds nearby amenities/services for a resolved
  location.
- `price_signal` — produces a price signal/comparison for the listing.

## Ownership boundary

- The application, agent, LLM modules, API, GUI, tests, architecture, and
  deployment are one stable area — everything except the three tools'
  live implementations.
- The **live tool implementation layer** is a separate, stable integration
  boundary covering only the real implementations and data sources for:
  - `geo_resolver`
  - `nearby_services`
  - `price_signal`

  Work outside this layer must not modify live tool behavior unless the
  task explicitly targets that layer. See `docs/tools.md` and
  `docs/contracts.md` for the exact interfaces the live implementations
  satisfy.

## Out of Scope (explicitly, for all phases of this project)

- LangChain, LangGraph, CrewAI, or any agent-framework library.
- Authentication / user accounts / sessions.
- Databases of any kind (SQL or NoSQL) and persistent storage.
- RAG (retrieval-augmented generation) / vector search / embeddings.
- Pinecone or any vector database.
- A microservices architecture — this is a single deployable app.
- Providing non-stub/non-mock tool logic outside the live tool
  implementation layer — other work may only provide stub/mock
  implementations of the tools for testing purposes.

## Required HTTP Endpoints

- `GET /api/team_info`
- `GET /api/agent_info`
- `GET /api/model_architecture`
- `POST /api/execute`
- `GET /` (serves the GUI)

`POST /api/execute` must always return a JSON object with exactly these
top-level fields: `status`, `error`, `response`, `steps` (see
`docs/contracts.md` for the full shape).

## Phase 0 Deliverables (this task)

1. `docs/scope.md` (this file)
2. `docs/contracts.md`
3. `docs/tools.md`
4. `.github/copilot-instructions.md`
5. Empty package directories/skeletons for the future implementation (no
   application logic yet)
