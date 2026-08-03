# GitHub Copilot Instructions — Tel Aviv Real Estate Listing Triage Agent

These instructions apply to all Copilot suggestions and chat/agent sessions
in this repository. Read `docs/scope.md`, `docs/contracts.md`, and
`docs/nir_handoff.md` before generating code — they are the source of
truth for architecture and interfaces.

## Project summary

A university AI Agents project: a Tel Aviv real estate listing triage
agent. It uses exactly two LLM modules in normal execution
(`triage_planner`, `report_writer`) plus deterministic Python modules
(`agent_orchestrator`, `plan_validator`, `evidence_collector`,
`evidence_bundle_builder`, `step_tracer`) and three tools (`geo_resolver`,
`nearby_services`, `price_signal`).

## Approved technology stack only

- Python 3.12
- FastAPI
- Pydantic v2
- httpx
- pytest
- Ruff
- Plain HTML/CSS/JavaScript for the GUI (no frontend framework/build step)
- Vercel for deployment
- LLMod.ai, accessed only via an abstract LLM client

Do not introduce any other framework, library, or infrastructure without
explicit approval, even if it seems convenient.

## Hard prohibitions

Copilot **must not** suggest, add, or use any of the following, under any
circumstances, anywhere in this repository:

- **LangChain**
- **LangGraph**
- **CrewAI**
- **Authentication** of any kind (login, sessions, tokens, OAuth, API keys
  for end users, etc.)
- **Databases** of any kind (SQL, NoSQL, ORMs, embedded DBs like SQLite for
  application state)
- **RAG** (retrieval-augmented generation, vector search, embeddings-based
  retrieval)
- **Pinecone** or any other vector database
- **A microservices architecture** — this is a single deployable
  application; do not split it into separately deployed services
- **Implementing Nir's real tool implementations.** Copilot may only
  generate mock/stub versions of `geo_resolver`, `nearby_services`, and
  `price_signal` (for testing/scaffolding purposes). The real
  implementations and data sources for these three tools belong to Nir —
  see `docs/nir_handoff.md`. Do not fill in real logic, real data sources,
  or real external API integrations for these three tools.

If a request would require any of the above, stop and flag it instead of
implementing a workaround.

## Architectural rules to follow

- Exactly two LLM modules are used in normal execution:
  `triage_planner` and `report_writer`. Do not add additional LLM calls
  into deterministic modules or tools.
- All LLM access must go through the abstract LLM client in
  `src/listing_agent/llm/client`. No module may call LLMod.ai directly.
- `plan_validator`, `evidence_collector`, `evidence_bundle_builder`, and
  `step_tracer` must be deterministic — no LLM calls, no network calls to
  LLM providers.
- Tools (`geo_resolver`, `nearby_services`, `price_signal`) must be
  deterministic and must not call an LLM.
- `POST /api/execute` must always return a JSON object with exactly these
  top-level fields: `status`, `error`, `response`, `steps`. Do not add,
  remove, or rename top-level fields.
- Required endpoints are exactly: `GET /api/team_info`,
  `GET /api/agent_info`, `GET /api/model_architecture`,
  `POST /api/execute`, `GET /`. Do not add unrelated endpoints without
  updating `docs/contracts.md` first.
- Keep ownership boundaries: only touch `src/listing_agent/tools/geo_resolver/`,
  `src/listing_agent/tools/nearby_services/`, `src/listing_agent/tools/price_signal/`
  (and their tests) with mock/stub implementations, per `docs/nir_handoff.md`.

## Style / process

- Run Ruff before considering Python changes complete.
- Add/extend pytest tests alongside any new logic.
- Keep the GUI as plain HTML/CSS/JavaScript — no React/Vue/Svelte/build
  tooling.
- Prefer small, contract-driven changes that match `docs/contracts.md`
  over speculative abstractions.
