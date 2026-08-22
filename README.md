# Tel Aviv Real Estate Listing Triage Agent

A university AI Agents project: an agent that triages a Tel Aviv
real-estate listing and produces a buyer-facing, grounded report. Given a
free-text listing (optionally with a buyer question or goal), it verifies
only the intentionally supported external-evidence scope for that listing
— it does not attempt to fact-check every claim in the text.

See `docs/scope.md`, `docs/contracts.md`, and `docs/tools.md` for
detailed scope, API/interface contracts, and tool-implementation context.
See `.github/copilot-instructions.md` for hard constraints on this repo.

## Architecture

Normal execution uses exactly **two LLM calls**:

1. `triage_planner` — reads the listing (and, on a follow-up turn, a
   compact prior-turn context) and produces a plan of which supported
   claims/questions to check and which tools that requires.
2. `report_writer` — turns the collected evidence into the final
   buyer-facing report.

Everything between them is deterministic Python — no additional LLM calls:
plan validation, tool-input building, evidence collection/execution,
evidence-to-claim mapping, and evidence-bundle assembly, orchestrated by
`agent_orchestrator` and recorded by `step_tracer`.

Three deterministic tools supply external evidence when a plan calls for
them:

- `geo_resolver` — resolves an address/description to a location.
- `nearby_services` — nearby points of interest for a given category.
- `price_signal` — comparable-sale/price signal for a resolved location.

Each tool has a `mock` implementation (small hardcoded fixtures, the
default) and a live implementation (`TOOLS_MODE=live`) that makes real
external HTTP calls (GovMap, OpenStreetMap/Overpass). See
`docs/contracts.md` for the full per-tool contract.

## API

The required endpoints:

- `GET /api/team_info` — team/course metadata.
- `GET /api/agent_info` — description, purpose, user-facing prompt
  template, and worked prompt/response examples.
- `GET /api/model_architecture` — a PNG diagram of the module/tool
  topology.
- `POST /api/execute` — runs the agent on `{"prompt": "...", "conversation_id":
  "..." (optional)}` and returns exactly `{"status", "error", "response",
  "steps"}`, where each item of `steps` is exactly
  `{"module", "prompt", "response"}` for each LLM call made, in order.
- `GET /` — the GUI (see below).

`GET /health` also exists as an operational liveness check; it is not one
of the four assignment API endpoints.

## Conversation / follow-up support

`conversation_id` is optional. Without it (or with an unknown id), a
request is handled as a fresh, fully stateless single turn. When the same
id is reused, the agent keeps a small structured "compact follow-up
context" (not a transcript replay, not the raw report text) so a follow-up
question can reuse the prior turn's resolved location/tool results where
still valid, without spending an extra tool or LLM call. See
`docs/contracts.md` for the exact fields kept and the current in-memory
storage limitation.

## Requirements

- Python 3.12

## Setup

Create a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install dependencies (production + dev):

```bash
scripts/install_deps.sh --dev    # omit --dev for runtime deps only
```

This installs from hash-pinned lock files with supply-chain guards enabled
(`--require-hashes`, `--only-binary=:all:`, `--uploaded-prior-to=P7D`).
See [Dependency security](#dependency-security).

Copy the environment example and adjust as needed:

```bash
cp .env.example .env
```

### Configuration

Key settings (see `.env.example` for the full list):

- `LLM_MODE` — `fake` (default) uses a deterministic offline
  `FakeLLMClient`, useful for local development and the entire automated
  test suite; `llmod` uses the real LLMod.ai client and makes live network
  calls. **A deployed/submitted instance must explicitly set
  `LLM_MODE=llmod`** — otherwise it silently serves fixture output.
- LLMod credentials/base URL/model are read only from environment
  variables (`LLMOD_API_KEY`, etc.); no secret is ever committed, logged,
  or included in a traced step.
- `TOOLS_MODE` — `mock` (default) uses small hardcoded fixture data;
  `live` uses the live GovMap/Overpass implementations.

## Running tests

```bash
pytest
```

The automated suite runs fully offline (`LLM_MODE=fake`, `TOOLS_MODE=mock`)
and covers the API contracts, the orchestrator pipeline, each LLM module,
each tool (mock and live implementations), the conversation/follow-up
layer, and a set of permanent regression tests pinning previously-found
bugs.

## Linting

```bash
ruff check .
```

## Running the app

```bash
uvicorn app:app --reload
```

Then visit:

- `http://127.0.0.1:8000/` — the GUI
- `http://127.0.0.1:8000/health` — health check

By default this runs with `LLM_MODE=fake`; set `LLM_MODE=llmod` (plus the
`LLMOD_*` environment variables) in `.env` to exercise the real LLM
provider.

## Regenerating the architecture diagram

`GET /api/model_architecture` serves a pre-generated PNG. Regenerate it
after changing the module/tool topology:

```bash
python scripts/generate_architecture_image.py
```

This writes `src/listing_agent/static/model_architecture.png`.


## Dependency security

Dependencies are locked and installed under four supply-chain guards.

| Guard | Where | Attack it blocks |
| --- | --- | --- |
| `--require-hashes` | `scripts/install_deps.sh` | A tampered or re-uploaded artifact on the index. Every file must match a sha256 recorded in the lock. |
| `--only-binary=:all:` | `scripts/install_deps.sh` | Arbitrary code execution at install time. Wheels are just archives; sdists run a build backend. |
| `--uploaded-prior-to=P7D` | `scripts/install_deps.sh` | A compromised release published minutes ago. Nothing younger than 7 days is eligible. |
| `exclude-newer = "7 days"` | `pyproject.toml` `[tool.uv]` | The same cooldown, applied at *resolution* time so it can never enter the lock. |

`--no-deps` is also passed, so only the locked closure is installed, and the
project's own editable install uses `--no-build-isolation` against the
hash-pinned `requirements-build.txt` — otherwise pip would fetch an
unverified build backend from the index.

`.github/workflows/ci.yml` runs the hardened install, then Ruff and pytest,
and pins every action to a full commit SHA (a tag can be repointed at
attacker-controlled code). A second job regenerates the lock and fails on any
drift, so a hand-edited requirements file cannot smuggle in an unhashed
dependency. `tests/test_supply_chain_guards.py` asserts all of this stays true.

### Changing dependencies

Edit `pyproject.toml`, then regenerate and commit the lock files together:

```bash
scripts/lock_deps.sh
```

This rewrites `uv.lock`, `requirements.txt`, `requirements-dev.txt` and
`requirements-build.txt`. Never hand-edit those four files.

To temporarily exempt a package you control from the cooldown, add it under
`[tool.uv.exclude-newer-package]` in `pyproject.toml` — note that
`test_uv_cooldown_has_no_unexpected_per_package_exemptions` will fail until
the exemption is reviewed and the test updated, which is deliberate.
