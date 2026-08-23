# Tel Aviv Real Estate Listing Triage Agent

A university AI Agents project: an agent that triages a Tel Aviv
real-estate listing and produces a buyer-facing, grounded report.

See `docs/scope.md`, `docs/contracts.md`, and `docs/tools.md` for
detailed scope, API/interface contracts, and tool-implementation context.
See `.github/copilot-instructions.md` for hard constraints on this repo.

## What the agent checks

Given a free-text listing (optionally with a buyer question or goal), the
agent verifies only the intentionally supported external-evidence scope
for that listing — it does not attempt to fact-check every claim in the
text. Depending on what the listing/question needs, it may:

- resolve the listing's address/location (`geo_resolver`);
- look up nearby amenities such as parks, schools, or transit
  (`nearby_services`);
- compare the asking price against comparable transactions
  (`price_signal`).

Only claims within this scope are checked; everything else is passed
through as unverified listing text.

## Architecture / execution flow

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

```text
POST /api/execute {prompt, conversation_id?}
    ↓
agent_orchestrator.run_agent_pipeline
    ↓
ConversationContext lookup (if conversation_id given)
    ↓
triage_planner            (LLM #1)
    ↓
plan_validator
    ↓
collect_evidence_with_context   (reuses cached tool results when possible)
    ↓
ToolExecutor  ->  ToolRegistry  ->  geo_resolver / nearby_services / price_signal
    ↓
evidence_bundle_builder
    ↓
report_writer              (LLM #2)
    ↓
{status, error, response, steps}
    ↓
ConversationContext saved (if conversation_id given)
```

Each tool has a `mock` implementation (small hardcoded fixtures, the
default) and a live implementation (`TOOLS_MODE=live`) that makes real
external HTTP calls (GovMap, OpenStreetMap/Overpass). See `docs/tools.md`
for the full per-tool contract.

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
  `conversation_id` is optional: without it, a request is a fresh,
  fully stateless single turn.
- `GET /` — the GUI (see [Try the GUI](#try-the-gui)).

`GET /health` also exists as an operational liveness check; it is not one
of the four assignment API endpoints.

### Conversation / follow-up support

`conversation_id` is optional. Without it (or with an unknown id), a
request is handled as a fresh, fully stateless single turn. When the same
id is reused, the agent keeps a small structured "compact follow-up
context" (not a transcript replay, not the raw report text) so a follow-up
question can reuse the prior turn's resolved location/tool results where
still valid, without spending an extra tool or LLM call. Context lives
only in the running server process's memory: restarting the server, or
landing on a different serverless instance, clears it. See
`docs/contracts.md` and `docs/follow_up_context_plan.md` for the exact
fields kept and this limitation.

## Run locally

Requires Python 3.12.

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

Start the app:

```bash
uvicorn app:app --reload
```

By default this runs fully offline (`LLM_MODE=fake`, `TOOLS_MODE=mock`).
See [Configuration](#configuration) to switch to the real LLM/tool
backends.

## Try the GUI

The easiest way to try the GUI is the deployed instance — see
[Real examples we tried](#real-examples-we-tried) for the listing/question
combinations to paste in, and [Evaluation and demo](#evaluation-and-demo)
for the deployment URL.

To run the GUI locally with real, working responses, configure a real LLM
backend first (`LLM_MODE=llmod`, see [Configuration](#configuration)),
then visit `http://127.0.0.1:8000/` (or `http://127.0.0.1:8000/health` for
a liveness check).

> **Note:** the default local setup (`LLM_MODE=fake`) does **not** produce
> working GUI/HTTP responses — the bundled `FakeLLMClient` has no fixture
> wired for arbitrary prompts submitted through `/api/execute`, so a
> freshly-cloned local instance run with default settings will return an
> application error for any prompt typed into the GUI. Deterministic
> fake-mode responses are only exercised by the automated pytest suite and
> by `scripts/run_e2e_scenarios.py`'s own scripted fixtures (see
> [Testing / acceptance](#testing--acceptance)), not by the live GUI/HTTP
> endpoint.

The page shows a conversation indicator and a "New conversation" button:
a `conversation_id` is generated client-side the first time you submit a
question and sent with every subsequent request in the same browser tab,
until you click "New conversation" or close the tab.

The same flow works over plain HTTP with no GUI, using the same
`/api/execute` contract and `conversation_id` field (requires
`LLM_MODE=llmod` to actually return a response, per the note above):

```bash
CID="demo-$(date +%s)"
curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"דרך הטייסים 8, תל אביב, 4 חדרים, 111 מ\\\"ר, מחיר מבוקש 4,700,000 ₪. האם יש פארק קרוב?\", \"conversation_id\": \"$CID\"}"

curl -s -X POST http://127.0.0.1:8000/api/execute \
  -H "Content-Type: application/json" \
  -d "{\"prompt\": \"כמה רחוק?\", \"conversation_id\": \"$CID\"}"
```

## Configuration

Key settings (see `.env.example` for the full list):

- `LLM_MODE` — `fake` (default) uses a deterministic offline
  `FakeLLMClient`, useful for local development and the entire automated
  test suite; `llmod` uses the real LLMod.ai client and makes live network
  calls. **A deployed/submitted instance must explicitly set
  `LLM_MODE=llmod`** — otherwise it silently serves fixture output.
- LLMod credentials/base URL/model are read only from environment
  variables (`LLMOD_API_KEY`, etc.); no secret is ever committed, logged,
  or included in a traced step. Never put real keys in this README or any
  other committed file.
- `TOOLS_MODE` — `mock` (default) uses small hardcoded fixture data;
  `live` uses the live GovMap/Overpass implementations.
- `TOOL_BACKEND` — `mock` (default) is the in-process `LocalToolExecutor`,
  which is what actually honours `TOOLS_MODE` above; `mcp` is an
  unimplemented placeholder that only proves the seam/configuration
  validation. Leave this as `mock`.

For safe/offline local development:

```bash
LLM_MODE=fake
TOOLS_MODE=mock
```

For real submission/live behavior:

```bash
LLM_MODE=llmod
TOOLS_MODE=live
```

## Testing / acceptance

Automated suite (fully offline, `LLM_MODE=fake`/`TOOLS_MODE=mock`):

```bash
pytest
```

It covers the API contracts, the orchestrator pipeline, each LLM module,
each tool (mock and live implementations), the conversation/follow-up
layer, and a set of permanent regression tests pinning previously-found
bugs.

```bash
ruff check .
```

`scripts/run_e2e_scenarios.py` is the manual **End-to-End Acceptance
Scenarios** harness — a set of scripted, checked scenarios run against a
live server for final acceptance, separate from pytest:

```bash
# Cheap, offline check of one or more scenarios:
LLM_MODE=fake TOOLS_MODE=mock python scripts/run_e2e_scenarios.py --only 1 4

# Full run against a live server for final acceptance:
python scripts/run_e2e_scenarios.py
```

Generated JSON/Markdown results are written under `.tmp/e2e_acceptance/`
(gitignored — running the script never dirties `git status`). Use
`LLM_MODE=fake`/`TOOLS_MODE=mock` for cheap local iteration, and reserve
an `LLM_MODE=llmod`/`TOOLS_MODE=live` run against a running server for an
intentional final acceptance pass.

## Known limitations

- The optional follow-up context is in-memory and process-local: it does
  not survive a restart, and is not guaranteed to be visible across
  instances/cold starts in a serverless deployment. Stateless single-turn
  use is unaffected.
- `price_signal` does not accept `lat`/`lon`, so it may perform its own
  address lookup even when `geo_resolver` already resolved it this turn.

See `docs/contracts.md` §7 for the complete list of known, intentional,
non-blocking limitations.

## Real examples we tried

These are real `POST /api/execute` calls made against the deployed
production instance (`LLM_MODE=llmod`, `TOOLS_MODE=live`) during
acceptance testing — not fabricated samples. Full methodology and the
complete scenario table are in
[Final Evaluation Results](docs/final_evaluation.md); this section is just
a few representative runs against the same listing.

Same listing used in all three examples below:

```text
2,400,000 ₪
3.5 חדרים
קומה 5
84 מ"ר

מעפילי אגוז 28, נוה חן, תל אביב יפו

דירה למכירה.
דירת 3.5 חדרים שמורה בשכונת נווה חן המתחדשת בתל אביב.
קומה 5 מתוך 7 עם מעלית, מיזוג אוויר ודוד שמש.
מתאימה למשקיעים.
קרובה לפארק, מחיר מציאה!
```

**1. Listing only, no question (default triage).** The planner picked out
the listing's own checkable claims ("קרובה לפארק", "מחיר מציאה") and
selected `geo_resolver`/`nearby_services`/`price_signal` accordingly — it
did not invent verification for claims like the elevator, A/C, solar
heater, or "מתאימה למשקיעים", which are outside this agent's tool scope.
Result: grounded report, no fabricated facts.

**2. Same listing + "האם הדירה מתאימה למשפחה?" (is this apartment suitable
for a family?).** The plan materially shifted toward family-relevant
nearby evidence (schools/daycare/parks/transit) instead of a generic
summary. The report did not claim the area is "good for families" or
"safe" — it framed conclusions around the actual nearby-service evidence
returned and named what the tools can't establish.

**3. Same listing + "האם המחיר הגיוני ביחס לאזור?" (is the price
reasonable for the area?).** The planner recognized the explicit
comparative-price question and planned `geo_resolver` + `price_signal`;
`nearby_services` was correctly not planned, since it isn't needed to
answer a price question. The report used the actual price-signal
evidence returned rather than inventing a verdict, and did not produce an
unsupported investment recommendation.

`price_signal` requires a resolved location from `geo_resolver` first. The
live government address index can occasionally return several address
candidates instead of one exact match (address ambiguity is a property of
the live data source, not specific to this listing). When that happens,
the agent skips the price comparison and reports the ambiguity rather than
inventing a number — this is expected, grounded behavior, not a planning
failure. A successful run of this same scenario does produce a full
comparison (asking price/sqm, comparable median price/sqm, and percent of
median); see [Final Evaluation Results](docs/final_evaluation.md) for both
outcomes as actually observed.

A fourth real production check — a post-deployment park-only health check
on a simplified version of the same listing, made as the final Stage 6
production smoke test — also returned a grounded, correctly-scoped
response (no `price_signal` planned for a park-only question). See
[Final Evaluation Results](docs/final_evaluation.md) for the full
acceptance run table.

## Evaluation and demo

- Deployed instance: <https://real-estate-listing-agent.vercel.app>
- [Evaluation & Demo Guide](docs/evaluation_guide.md) — supported question
  types, a recommended multi-turn demo, and what good agent behavior looks
  like.
- [Final Evaluation Results](docs/final_evaluation.md) — the acceptance
  scenario table and findings discovered/fixed before submission.

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

## Regenerating the architecture diagram

`GET /api/model_architecture` serves a pre-generated PNG. Regenerate it
after changing the module/tool topology:

```bash
python scripts/generate_architecture_image.py
```

This writes `src/listing_agent/static/model_architecture.png`.
