"""Run ten fixed listings end to end through `POST /api/execute` and write a
baseline result file.

WHY THIS EXISTS
---------------
Nir's tools and Aviv's pipeline are developed apart and integrated late. When
they meet, "it behaves differently" is easy to say and hard to localise: a
changed report could come from the planner, the validator, the tool inputs,
a tool, or the report writer. This script pins what the whole chain produces
for ten deliberately chosen listings, in one committed file, so the two sides
can diff the same thing instead of describing it to each other.

Each scenario is ONE real `POST /api/execute` against a running app - the
same entry point a user hits. Nothing is stubbed.

WHAT TO COMPARE, AND WHAT TO IGNORE
-----------------------------------
Two LLM calls sit in this chain (`triage_planner`, `report_writer`), so no
run reproduces another exactly. The file separates the two kinds of output
for that reason:

  * STABLE - extracted fields, planned tools, the tool inputs those calls
    build, tool outcomes, coordinates, category lists. Differences here are
    real and worth chasing.
  * VARIES - claim wording and ids, buyer_goal, and the report prose. The
    same listing legitimately produces different text run to run; only
    treat a difference as a defect if it changes a STABLE value.

Every scenario also carries explicit checks. A FAIL is a rule the system is
supposed to enforce deterministically (e.g. `price_signal` must never run on
a rental), and is worth investigating whichever side you are on.

TOOL INPUTS ARE RECONSTRUCTED, NOT GUESSED
------------------------------------------
`/api/execute` reports what the planner asked for, not the typed input each
tool actually received. This script rebuilds that input with the very
function the pipeline uses (`agent.tool_input_builder.build_tool_input`), so
the recorded `categories`/`city_hint` are the real ones - and if that module
ever changes, this file changes with it rather than drifting silently.

USAGE
-----
Start the app first (this script never launches it):

    .\\.venv\\Scripts\\python.exe -m uvicorn src.listing_agent.main:app --port 8000
    .\\.venv\\Scripts\\python.exe scripts\\run_e2e_scenarios.py

    --base-url URL   default http://127.0.0.1:8000
    --only 04,07     run a subset by scenario number
    --out-dir DIR    default docs/e2e_baseline
    --rtl            print Hebrew right-to-left in the CONSOLE (the result
                     file always keeps the real text, so it stays diffable)

Budget: ~90-160s and two LLM calls per scenario, so a full pass is roughly
20-25 minutes and 20 calls. Live tools mean live upstreams: an Overpass
mirror having a bad day shows up here as a slow or empty `nearby_services`,
which is worth knowing but is not a code difference.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# Allow running as a script from the repo root without installing the package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.listing_agent.agent.schemas import PropertyDetails, ToolCall  # noqa: E402
from src.listing_agent.agent.tool_input_builder import (  # noqa: E402
    MissingDependencyError,
    build_tool_input,
)
from src.listing_agent.tools.schemas import GeoResolverOutput  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OUT_DIR = "docs/e2e_baseline"

# One run can legitimately take minutes; the server's own deadline is 290s.
REQUEST_TIMEOUT_S = 300.0


@dataclass
class Scenario:
    """One listing, what it is for, and what must hold afterwards."""

    name: str
    purpose: str
    prompt: str
    # Tools that MUST appear in the validated plan.
    expect_tools: set[str] = field(default_factory=set)
    # Tools that must NEVER appear. A hit here is always a real defect.
    forbid_tools: set[str] = field(default_factory=set)
    # Expected `geo_resolver` outcome, or None to record without judging.
    expect_geo_outcome: str | None = None
    # Field names that must be reported as missing.
    expect_missing: set[str] = field(default_factory=set)
    # Exact category set `nearby_services` must be called with. Set only
    # where narrowing is the point of the scenario.
    expect_nearby_categories: set[str] | None = None
    # Free-text notes that go into the result file next to the scenario.
    notes: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="01_no_street",
        purpose="A city with no street name at all.",
        prompt=(
            'דירת 3 חדרים למכירה, 75 מ"ר, קומה 4 מתוך 8, מעלית ומרפסת שמש. '
            'תל אביב. מחיר מבוקש 3,900,000 ש"ח.'
        ),
        expect_tools={"geo_resolver"},
        notes=(
            "There is a city to search but nothing to find inside it, so the "
            "question is how coarse a match the tool admits to. A settlement- "
            "or city-level hit must report `low_confidence`, never `exact`."
        ),
    ),
    Scenario(
        name="02_no_city",
        purpose="A street and number, but no city anywhere in the text.",
        prompt=(
            "בן יהודה 45. דירת 2 חדרים, 55 מ\"ר, משופצת מהיסוד, קומה 1 ללא מעלית. "
            'מחיר מבוקש 2,850,000 ש"ח.'
        ),
        expect_geo_outcome="skipped",
        notes=(
            "The rule added on 2026-08-19: without a recognised city govmap "
            "searches the whole country, so the run stops instead of guessing. "
            "`geo_resolver` must be reported `skipped` with a reason naming the "
            "address, and every location-dependent tool skips with it."
        ),
    ),
    Scenario(
        name="03_no_price",
        purpose="A complete address with no price stated.",
        prompt=(
            "רוטשילד 30, תל אביב. דירת 3 חדרים, 80 מ\"ר, קומה 2 עם מעלית, "
            "מרפסת ומחסן. הדירה שמורה ומוארת."
        ),
        expect_tools={"geo_resolver"},
        forbid_tools={"price_signal"},
        expect_geo_outcome="resolved",
        expect_missing={"asking_price"},
        notes="A price comparison with no price is meaningless; the tool must not be planned.",
    ),
    Scenario(
        name="04_normal",
        purpose="An ordinary, complete purchase listing. The baseline case.",
        prompt=(
            "דיזנגוף 50, תל אביב. דירת 3 חדרים, 85 מ\"ר, קומה 3 מתוך 5, מעלית "
            'ומרפסת. מחיר מבוקש 4,200,000 ש"ח.'
        ),
        expect_tools={"geo_resolver", "price_signal"},
        expect_geo_outcome="resolved",
        notes="If anything here differs, suspect the integration before the listing.",
    ),
    Scenario(
        name="05_nearby_claims",
        purpose="Six amenity claims, all inside the supported categories.",
        prompt=(
            "אבן גבירול 100, תל אביב. דירת 4 חדרים, 100 מ\"ר. בית ספר וגן ילדים "
            "במרחק הליכה, סופרמרקט ובית מרקחת מתחת לבניין, תחנת אוטובוס בפינת "
            'הרחוב ופארק גדול ממול. מחיר מבוקש 5,500,000 ש"ח.'
        ),
        expect_tools={"geo_resolver", "nearby_services"},
        expect_geo_outcome="resolved",
        notes=(
            "Every claim maps onto a real category, so this is the widest "
            "`nearby_services` call the system can legitimately make. Compare "
            "the category list, not the place counts - OpenStreetMap coverage "
            "changes on its own."
        ),
    ),
    Scenario(
        name="06_q_price",
        purpose="A buyer question about the price.",
        prompt=(
            "ארלוזורוב 25, תל אביב. דירת 3 חדרים, 78 מ\"ר, קומה 2, משופצת. "
            'מחיר מבוקש 4,600,000 ש"ח. האם המחיר הגיוני ביחס לאזור?'
        ),
        expect_tools={"geo_resolver", "price_signal"},
        expect_geo_outcome="resolved",
        notes="An explicit question should reach the same plan as the implicit one in 04.",
    ),
    Scenario(
        name="07_q_supermarket",
        purpose="A buyer question naming ONE category.",
        prompt=(
            "קינג ג'ורג' 40, תל אביב. דירת 2 חדרים, 60 מ\"ר, קומה 3. מחיר מבוקש "
            '3,100,000 ש"ח. יש סופרמרקטים בסביבה?'
        ),
        expect_tools={"geo_resolver", "nearby_services"},
        expect_geo_outcome="resolved",
        expect_nearby_categories={"supermarket"},
        notes=(
            "The narrowing case. A question about supermarkets must not fan out "
            "to all seven categories and answer with hospitals."
        ),
    ),
    Scenario(
        name="08_q_kindergarten",
        purpose="The same shape as 07, for a category with no exact enum name.",
        prompt=(
            "יהודה הנשיא 30, תל אביב. דירת 4 חדרים, 95 מ\"ר, קומה 1, מעלית וחניה. "
            'מחיר מבוקש 6,200,000 ש"ח. יש גני ילדים קרובים לבניין?'
        ),
        expect_tools={"geo_resolver", "nearby_services"},
        expect_geo_outcome="resolved",
        expect_nearby_categories={"daycare"},
        notes=(
            "There is no `kindergarten` category; גן ילדים must resolve to "
            "`daycare` (see `_NEARBY_CATEGORY_ALIASES`), which is what OSM tags "
            "Israeli גנים as."
        ),
    ),
    Scenario(
        name="09_rental",
        purpose="A rental, with the buyer asking about the price anyway.",
        prompt=(
            "להשכרה: פרישמן 20, תל אביב. דירת 2 חדרים, 50 מ\"ר, קומה 2, מרוהטת "
            'במלואה. שכר דירה 6,500 ש"ח לחודש. האם המחיר הגיוני לאזור?'
        ),
        expect_tools={"geo_resolver"},
        forbid_tools={"price_signal"},
        expect_geo_outcome="resolved",
        notes=(
            "The question pulls one way and the rule pulls the other. "
            "`price_signal` compares against SALE transactions, so running it on "
            "a monthly rent would compare 6,500 with millions. The rule wins."
        ),
    ),
    Scenario(
        name="10_prefix_and_shorthand",
        purpose="Three known hazards in one listing.",
        prompt=(
            "למכירה בשמואל תמיר 2, תל אביב. פנטהאוז מרווח 160 מ\"ר במפלס אחד "
            "עם מרפסות שמש. מחיר מבוקש 10.5 מ'."
        ),
        expect_tools={"geo_resolver"},
        expect_geo_outcome="resolved",
        notes=(
            "(a) the street arrives with the Hebrew preposition attached "
            "('בשמואל'), which govmap answers with the wrong street; (b) house "
            "number 2 is not in the government index for that street, so the "
            "answer should be a street-level `approximate` match with the "
            "missing number stated; (c) KNOWN OPEN DEFECT - \"10.5 מ'\" is "
            "extracted as 10.5 shekels rather than 10,500,000, which makes any "
            "price comparison in this scenario nonsense. Left unfixed "
            "deliberately; recorded here so the integration does not rediscover "
            "it as a mystery."
        ),
    ),
]

# --- RTL console helper ----------------------------------------------------
#
# A terminal that does not reorder bidirectional text prints Hebrew in
# logical order, which reads backwards. `--rtl` flips it for display only.
# Digit and Latin runs are flipped back so "10.5" does not become "5.01".
_KEEP_LTR = re.compile(r"[0-9A-Za-z][0-9A-Za-z.,'’]*")


def visual_rtl(text: str) -> str:
    return _KEEP_LTR.sub(lambda m: m.group(0)[::-1], text[::-1])


def _display(text: str, rtl: bool) -> str:
    return visual_rtl(text) if rtl else text


# --- pulling the run apart -------------------------------------------------


def _step(response: dict, name: str) -> dict:
    for step in response.get("steps") or []:
        if step.get("step") == name:
            return step
    return {}


def _planner_output(response: dict) -> dict:
    return (_step(response, "triage_planner").get("detail") or {}).get("response") or {}


def _bundle(response: dict) -> dict:
    """The compact evidence bundle, read back off the report_writer step.

    It is the only place the post-validation plan and every tool result are
    visible together, which is exactly what this file needs to record.
    """
    detail = _step(response, "report_writer").get("detail") or {}
    return (detail.get("prompt") or {}).get("user_payload") or {}


def _tool_result(bundle: dict, tool: str) -> dict | None:
    for result in bundle.get("tool_results") or []:
        if result.get("tool") == tool:
            return result
    return None


def _geo_state(bundle: dict) -> tuple[str, dict]:
    """`geo_resolver`'s state as one word, plus its observations.

    "skipped" and "error" are tool-level states; anything else is the typed
    business outcome (`resolved`/`ambiguous`/`not_found`). Flattening them
    into one field is what lets a scenario state its expectation simply.
    """
    result = _tool_result(bundle, "geo_resolver")
    if result is None:
        return "not_planned", {}
    if result.get("status") != "ok":
        return str(result.get("status")), {}
    observations = result.get("observations") or {}
    return str(observations.get("outcome") or "ok"), observations


def _rebuild_tool_inputs(response: dict) -> list[tuple[str, Any]]:
    """The typed input each planned call would be executed with.

    Uses the pipeline's own `build_tool_input`, so what is recorded is what
    the tool really received - notably the resolved `categories`, which the
    HTTP response never shows.
    """
    plan = _planner_output(response)
    details = PropertyDetails.model_validate(plan.get("property_details") or {})
    claims = plan.get("claims") or []

    bundle = _bundle(response)
    geo_result = _tool_result(bundle, "geo_resolver")
    geo_output: GeoResolverOutput | None = None
    if geo_result and geo_result.get("status") == "ok" and geo_result.get("observations"):
        try:
            # `source` is a required field on the model but the mapper keeps
            # it beside the observations rather than inside them, so it has
            # to be put back or validation fails and every dependent tool
            # looks like it was never built.
            candidate = GeoResolverOutput.model_validate(
                {
                    **geo_result["observations"],
                    "source": geo_result.get("source") or "(recorded outside observations)",
                }
            )
            if candidate.outcome == "resolved":
                geo_output = candidate
        except Exception:
            geo_output = None

    rebuilt: list[tuple[str, Any]] = []
    for raw_call in plan.get("tool_calls") or []:
        try:
            call = ToolCall.model_validate(raw_call)
        except Exception as exc:
            rebuilt.append((str(raw_call.get("tool")), f"(unparsable call: {exc})"))
            continue
        try:
            from src.listing_agent.agent.schemas import Claim

            model = build_tool_input(
                call,
                details,
                geo_output,
                [Claim.model_validate(c) for c in claims],
            )
            rebuilt.append((call.tool, model.model_dump()))
        except MissingDependencyError as exc:
            rebuilt.append((call.tool, f"(not built - {exc.reason})"))
        except Exception as exc:
            rebuilt.append((call.tool, f"({type(exc).__name__}: {exc})"))
    return rebuilt


def _nearby_categories_used(rebuilt: list[tuple[str, Any]]) -> set[str] | None:
    for tool, model in rebuilt:
        if tool == "nearby_services" and isinstance(model, dict):
            return set(model.get("categories") or [])
    return None


# --- checks ----------------------------------------------------------------


def run_checks(scenario: Scenario, response: dict) -> list[tuple[str, str, str]]:
    """Return `(verdict, label, detail)` for every rule this scenario states.

    Verdicts are PASS / FAIL / n-a. Nothing raises: a scenario that fails is
    information, and the remaining nine still need to run.
    """
    checks: list[tuple[str, str, str]] = []
    bundle = _bundle(response)
    planned = {call.get("tool") for call in _planner_output(response).get("tool_calls") or []}
    reported = {result.get("tool") for result in bundle.get("tool_results") or []}
    present = planned | reported
    geo_state, _ = _geo_state(bundle)
    rebuilt = _rebuild_tool_inputs(response)

    for tool in sorted(scenario.expect_tools):
        checks.append(
            (
                "PASS" if tool in present else "FAIL",
                f"{tool} is planned",
                f"planned={sorted(present)}",
            )
        )
    for tool in sorted(scenario.forbid_tools):
        checks.append(
            (
                "FAIL" if tool in present else "PASS",
                f"{tool} is NOT planned",
                f"planned={sorted(present)}",
            )
        )
    if scenario.expect_geo_outcome:
        checks.append(
            (
                "PASS" if geo_state == scenario.expect_geo_outcome else "FAIL",
                f"geo_resolver is '{scenario.expect_geo_outcome}'",
                f"actual={geo_state}",
            )
        )
    missing = set(bundle.get("missing_information") or [])
    for field_name in sorted(scenario.expect_missing):
        checks.append(
            (
                "PASS" if field_name in missing else "FAIL",
                f"'{field_name}' reported missing",
                f"missing={sorted(missing)}",
            )
        )
    if scenario.expect_nearby_categories is not None:
        used = _nearby_categories_used(rebuilt)
        if used is None:
            checks.append(("FAIL", "nearby_services categories", "nearby_services was not built"))
        else:
            checks.append(
                (
                    "PASS" if used == scenario.expect_nearby_categories else "FAIL",
                    f"nearby_services categories == {sorted(scenario.expect_nearby_categories)}",
                    f"actual={sorted(used)}",
                )
            )
    return checks


# --- one run ---------------------------------------------------------------


def execute(base_url: str, prompt: str) -> tuple[dict, float]:
    started = time.time()
    reply = httpx.post(
        f"{base_url}/api/execute", json={"prompt": prompt}, timeout=REQUEST_TIMEOUT_S
    )
    return reply.json(), time.time() - started


def render_scenario(scenario: Scenario, response: dict, seconds: float) -> str:
    """One scenario's section of the result file."""
    plan = _planner_output(response)
    bundle = _bundle(response)
    geo_state, geo_observations = _geo_state(bundle)
    rebuilt = _rebuild_tool_inputs(response)
    checks = run_checks(scenario, response)

    lines: list[str] = []
    add = lines.append

    add(f"## {scenario.name}")
    add("")
    add(f"_{scenario.purpose}_")
    add("")
    add("**Listing**")
    add("")
    add("```")
    add(scenario.prompt)
    add("```")
    add("")
    if scenario.notes:
        add(f"**Why this scenario:** {scenario.notes}")
        add("")

    add(f"**Result:** `status={response.get('status')}` in {seconds:.0f}s")
    if response.get("error"):
        add("")
        add(f"> ERROR: {response['error']}")
    add("")

    add("**Checks**")
    add("")
    if checks:
        add("| | check | detail |")
        add("|---|---|---|")
        for verdict, label, detail in checks:
            add(f"| {verdict} | {label} | `{detail}` |")
    else:
        add("_(none - this scenario only records what happened)_")
    add("")

    add("**STABLE - compare these**")
    add("")
    add("```json")
    add(
        json.dumps(
            {
                "property_details": plan.get("property_details"),
                "planned_tools": [c.get("tool") for c in plan.get("tool_calls") or []],
                "executed_tools": bundle.get("executed_tools"),
                "tool_inputs_built": {
                    tool: model for tool, model in rebuilt
                },
                "geo_resolver": geo_state,
                "geo_observations": geo_observations,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
    )
    add("```")
    add("")

    add("**Tool results**")
    add("")
    for result in bundle.get("tool_results") or []:
        add(f"- `{result.get('tool')}` status=`{result.get('status')}`")
        if result.get("reason"):
            add(f"  - reason: {result['reason']}")
        if result.get("error"):
            add(f"  - error: {result['error']}")
        observations = result.get("observations") or {}
        if result.get("tool") == "nearby_services" and observations:
            summary = {k: v for k, v in observations.items() if k != "places"}
            add(f"  - `{json.dumps(summary, ensure_ascii=False, sort_keys=True)}`")
        elif observations:
            add(f"  - `{json.dumps(observations, ensure_ascii=False, sort_keys=True)}`")
    add("")

    add("<details><summary>VARIES - claims, buyer_goal, report prose</summary>")
    add("")
    add(f"`buyer_goal`: {plan.get('buyer_goal')}")
    add("")
    add("| id | category | type | text |")
    add("|---|---|---|---|")
    for claim in plan.get("claims") or []:
        text = str(claim.get("text", "")).replace("|", "\\|")
        add(
            f"| {claim.get('id') or '-'} | {claim.get('category')} "
            f"| {claim.get('type')} | {text} |"
        )
    add("")
    add("```")
    add(str(response.get("response") or "(no report)"))
    add("```")
    add("")
    add("</details>")
    add("")
    return "\n".join(lines)


def _provenance(base_url: str) -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except Exception:
            return "(unavailable)"

    from src.listing_agent.config import get_settings

    settings = get_settings()
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": git("rev-parse", "--short", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "base_url": base_url,
        "llm_mode": settings.llm_mode,
        "llm_model": settings.llmod_model,
        "tools_mode": settings.tools_mode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--only", default="", help='e.g. "04,07" to run a subset')
    parser.add_argument("--rtl", action="store_true", help="right-to-left console output")
    args = parser.parse_args()

    selected = SCENARIOS
    if args.only:
        wanted = {part.strip() for part in args.only.split(",") if part.strip()}
        selected = [s for s in SCENARIOS if s.name.split("_")[0] in wanted]
        if not selected:
            print(f"No scenario matches --only {args.only!r}")
            return 2

    try:
        health = httpx.get(f"{args.base_url}/health", timeout=10)
        health.raise_for_status()
    except Exception as exc:
        print(f"The app is not answering at {args.base_url} ({type(exc).__name__}).")
        print("Start it first:")
        print("  .\\.venv\\Scripts\\python.exe -m uvicorn src.listing_agent.main:app --port 8000")
        return 2

    provenance = _provenance(args.base_url)
    print(json.dumps(provenance, indent=1))
    print(f"\nRunning {len(selected)} scenario(s). Expect ~2 minutes each.\n")

    sections: list[str] = []
    summary: list[tuple[str, int, int, float]] = []
    raw: dict[str, Any] = {}
    total_started = time.time()

    for index, scenario in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] {scenario.name} ...", end=" ", flush=True)
        try:
            response, seconds = execute(args.base_url, scenario.prompt)
        except Exception as exc:
            response, seconds = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}, 0.0
        checks = run_checks(scenario, response)
        failures = sum(1 for verdict, _, _ in checks if verdict == "FAIL")
        summary.append((scenario.name, len(checks) - failures, failures, seconds))
        sections.append(render_scenario(scenario, response, seconds))
        raw[scenario.name] = response
        print(f"{response.get('status')} in {seconds:.0f}s, {failures} failed check(s)")
        geo_state, observations = _geo_state(_bundle(response))
        address = observations.get("normalized_address")
        if address:
            print(f"      geo: {geo_state} -> {_display(str(address), args.rtl)}")
        elif geo_state != "not_planned":
            print(f"      geo: {geo_state}")

    out_dir = _REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out_dir / f"e2e_baseline_{stamp}.md"
    json_path = out_dir / f"e2e_baseline_{stamp}.json"

    total_failures = sum(f for _, _, f, _ in summary)
    header: list[str] = [
        f"# End-to-end scenario baseline - {stamp}",
        "",
        "Ten fixed listings, each one real `POST /api/execute` against the running",
        "app with live tools. Generated by `scripts/run_e2e_scenarios.py`.",
        "",
        "**How to use this file:** re-run the script after integrating and diff the",
        "two. Compare the STABLE block of each scenario - extracted fields, planned",
        "tools, built tool inputs, geo outcome and coordinates. Ignore differences",
        "in claim wording, claim ids, `buyer_goal` and report prose: two LLM calls",
        "sit in this chain and none of that reproduces exactly. A FAILED check is",
        "a deterministic rule that broke, and is always worth investigating.",
        "",
        "```json",
        json.dumps(provenance, indent=1, sort_keys=True),
        "```",
        "",
        f"**{total_failures} failed check(s)** across {len(summary)} scenario(s), "
        f"total {time.time() - total_started:.0f}s.",
        "",
        "| scenario | passed | failed | seconds |",
        "|---|---|---|---|",
    ]
    for name, passed, failed, seconds in summary:
        anchor = name.replace("_", "-")
        header.append(f"| [{name}](#{anchor}) | {passed} | {failed} | {seconds:.0f} |")
    header.append("")

    md_path.write_text("\n".join([*header, *sections]), encoding="utf-8")
    json_path.write_text(
        json.dumps({"provenance": provenance, "runs": raw}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    print(f"\nWrote {md_path.relative_to(_REPO_ROOT)}")
    print(f"Wrote {json_path.relative_to(_REPO_ROOT)}  (full responses)")
    print(f"{total_failures} failed check(s) in total.")
    return 0


if __name__ == "__main__":
    # Hebrew on a Windows console needs UTF-8 or the run dies mid-print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
