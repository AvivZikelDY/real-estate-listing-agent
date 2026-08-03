from fastapi.testclient import TestClient

from src.listing_agent.agent.module_names import ALL_MODULE_NAMES
from src.listing_agent.llm.report_writer.writer import MODULE_NAME as REPORT_WRITER_MODULE
from src.listing_agent.llm.triage_planner.planner import MODULE_NAME as TRIAGE_PLANNER_MODULE
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
    for field in ("description", "purpose", "prompt_templates", "prompt_examples"):
        assert field in body
        assert body[field]


def test_agent_info_prompt_templates_cover_both_llm_modules() -> None:
    response = client.get("/api/agent_info")

    templates = response.json()["prompt_templates"]
    assert set(templates) == {TRIAGE_PLANNER_MODULE, REPORT_WRITER_MODULE}
    assert all(isinstance(text, str) and text.strip() for text in templates.values())


def test_agent_info_has_at_least_two_prompt_examples() -> None:
    response = client.get("/api/agent_info")

    examples = response.json()["prompt_examples"]
    assert len(examples) >= 2


def test_agent_info_examples_cover_family_and_price_scenarios() -> None:
    response = client.get("/api/agent_info")

    titles = " ".join(example["title"].lower() for example in response.json()["prompt_examples"])
    assert "family" in titles
    assert "price" in titles or "missing" in titles


def test_agent_info_examples_include_full_response_and_steps() -> None:
    response = client.get("/api/agent_info")

    for example in response.json()["prompt_examples"]:
        assert set(example.keys()) == {"title", "prompt", "full_response", "steps"}
        assert isinstance(example["full_response"], str) and example["full_response"].strip()
        assert isinstance(example["steps"], list)
        assert len(example["steps"]) > 0
        for step in example["steps"]:
            assert set(step.keys()) == {"step", "type", "status", "detail"}


def test_agent_info_example_module_names_match_the_architecture_names() -> None:
    response = client.get("/api/agent_info")

    for example in response.json()["prompt_examples"]:
        step_names = {step["step"] for step in example["steps"]}
        assert step_names <= FROZEN_MODULE_NAMES


def test_agent_info_first_example_steps_start_with_triage_planner() -> None:
    response = client.get("/api/agent_info")

    example = response.json()["prompt_examples"][0]
    llm_steps = [step for step in example["steps"] if step["type"] == "llm"]
    assert [step["step"] for step in llm_steps] == ["triage_planner", "report_writer"]
