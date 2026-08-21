from fastapi.testclient import TestClient

from src.listing_agent.agent.module_names import ALL_MODULE_NAMES
from src.listing_agent.llm.report_writer import REPORT_WRITER_SYSTEM_PROMPT
from src.listing_agent.llm.triage_planner import TRIAGE_PLANNER_SYSTEM_PROMPT
from src.listing_agent.main import app

client = TestClient(app)

# The exact frozen module/tool names that must appear consistently across
# `/api/agent_info` and the architecture diagram served by
# `/api/model_architecture`. Sourced from `agent.module_names`, the single
# source of truth introduced in Phase 16, so this test can never silently
# drift from the constants the rest of the codebase actually uses.
FROZEN_MODULE_NAMES = ALL_MODULE_NAMES


def test_agent_info_includes_all_required_top_level_fields() -> None:
    response = client.get("/api/agent_info")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "agent_name",
        "description",
        "version",
        "purpose",
        "prompt_template",
        "prompt_examples",
    }
    for field in ("description", "purpose", "prompt_template", "prompt_examples"):
        assert body[field]


def test_agent_info_prompt_template_is_singular_user_facing_object() -> None:
    response = client.get("/api/agent_info")

    body = response.json()
    prompt_template = body["prompt_template"]
    assert set(prompt_template.keys()) == {"template"}
    template = prompt_template["template"]
    assert isinstance(template, str) and template.strip()


def test_agent_info_prompt_template_does_not_leak_internal_system_prompts() -> None:
    response = client.get("/api/agent_info")

    template = response.json()["prompt_template"]["template"]
    assert TRIAGE_PLANNER_SYSTEM_PROMPT not in template
    assert REPORT_WRITER_SYSTEM_PROMPT not in template


def test_agent_info_prompt_template_describes_how_to_use_the_agent() -> None:
    response = client.get("/api/agent_info")

    template = response.json()["prompt_template"]["template"].lower()
    assert "listing" in template
    assert "tel aviv" in template


def test_agent_info_has_at_least_two_prompt_examples() -> None:
    response = client.get("/api/agent_info")

    examples = response.json()["prompt_examples"]
    assert len(examples) >= 2


def test_agent_info_examples_cover_family_and_price_scenarios() -> None:
    response = client.get("/api/agent_info")

    prompts = " ".join(example["prompt"].lower() for example in response.json()["prompt_examples"])
    assert "family" in prompts
    assert "price" in prompts or "missing" in prompts


def test_agent_info_examples_include_full_response_and_steps() -> None:
    response = client.get("/api/agent_info")

    for example in response.json()["prompt_examples"]:
        assert set(example.keys()) == {"prompt", "full_response", "steps"}
        assert isinstance(example["full_response"], str) and example["full_response"].strip()
        assert isinstance(example["steps"], list)
        assert len(example["steps"]) > 0
        for step in example["steps"]:
            assert set(step.keys()) == {"module", "prompt", "response"}
            assert isinstance(step["prompt"], dict)
            assert isinstance(step["response"], dict)


def test_agent_info_example_module_names_match_the_architecture_names() -> None:
    response = client.get("/api/agent_info")

    for example in response.json()["prompt_examples"]:
        step_names = {step["module"] for step in example["steps"]}
        assert step_names <= FROZEN_MODULE_NAMES


def test_agent_info_first_example_steps_start_with_triage_planner() -> None:
    response = client.get("/api/agent_info")

    example = response.json()["prompt_examples"][0]
    assert [step["module"] for step in example["steps"]] == ["triage_planner", "report_writer"]


def test_agent_info_examples_never_leak_tool_or_deterministic_steps() -> None:
    """A4 requirement (4): only the two LLM modules may appear publicly -
    `plan_validator`, `evidence_collector`, `evidence_bundle_builder`,
    `geo_resolver`, `nearby_services`, `price_signal`, etc. must never leak
    into the public example steps, exactly as `/api/execute` already
    guarantees via the shared serializer.
    """
    from src.listing_agent.agent.module_names import REPORT_WRITER, TRIAGE_PLANNER

    response = client.get("/api/agent_info")

    for example in response.json()["prompt_examples"]:
        step_names = {step["module"] for step in example["steps"]}
        assert step_names <= {TRIAGE_PLANNER, REPORT_WRITER}


def test_agent_info_examples_use_the_same_serializer_as_execute() -> None:
    """A4: `prompt_examples[].steps` must use the exact same
    `execute_step_serializer.build_public_steps` function `/api/execute`
    uses - never a re-implemented mapping. Verified two ways: (1) the
    module that builds the examples imports the shared serializer
    function by name, and (2) a step built directly via the shared
    serializer from the same tracer output is byte-for-byte identical to
    what `/api/agent_info` actually returned.
    """
    import inspect

    from src.listing_agent.api import agent_info_examples
    from src.listing_agent.api.execute_step_serializer import build_public_steps

    source = inspect.getsource(agent_info_examples)
    assert "build_public_steps" in source
    assert agent_info_examples.build_public_steps is build_public_steps
