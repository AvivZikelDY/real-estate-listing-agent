# Tel Aviv Real Estate Listing Triage Agent

University AI Agents project. See `docs/scope.md`, `docs/contracts.md`, and
`docs/nir_handoff.md` for architecture, contracts, and ownership. See
`.github/copilot-instructions.md` for hard constraints on this repo.

**Status:** Phase 1 — Python/FastAPI bootstrap only. No assignment
endpoints (besides `/health`), no LLM behavior, no tool behavior yet.

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
pip install -e ".[dev]"
```

Copy the environment example and adjust as needed:

```bash
cp .env.example .env
```

## Running tests

```bash
pytest
```

## Linting

```bash
ruff check .
```

## Running the app

```bash
uvicorn app:app --reload
```

Then visit:

- `http://127.0.0.1:8000/` — placeholder GUI page
- `http://127.0.0.1:8000/health` — health check

## Regenerating the architecture diagram

`GET /api/model_architecture` serves a pre-generated PNG. Regenerate it
after changing the module/tool topology:

```bash
python scripts/generate_architecture_image.py
```

This writes `src/listing_agent/static/model_architecture.png`.

