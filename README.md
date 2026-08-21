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
scripts/install_deps.sh --dev    # omit --dev for runtime deps only
```

This installs from hash-pinned lock files with supply-chain guards enabled
(`--require-hashes`, `--only-binary=:all:`, `--uploaded-prior-to=P7D`).
See [Dependency security](#dependency-security).

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
