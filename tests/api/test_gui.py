from fastapi.testclient import TestClient

from src.listing_agent.main import app

client = TestClient(app)


def test_root_page_returns_200_and_contains_run_agent_button() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Run Agent" in response.text


def test_root_page_links_static_assets() -> None:
    response = client.get("/")

    assert "/static/style.css" in response.text
    assert "/static/app.js" in response.text


def test_static_app_js_is_served() -> None:
    response = client.get("/static/app.js")

    assert response.status_code == 200


def test_static_style_css_is_served() -> None:
    response = client.get("/static/style.css")

    assert response.status_code == 200


def test_static_app_js_renders_the_public_execute_step_shape() -> None:
    """A3 compatibility: the GUI must read the new public step fields
    (`module`/`prompt`/`response`), not the old `step`/`detail` shape."""
    response = client.get("/static/app.js")

    body = response.text
    assert "step.module" in body
    assert "step.prompt" in body
    assert "step.response" in body
    assert "step.detail" not in body


def test_root_page_has_final_answer_as_the_primary_result_heading() -> None:
    """The agent's result must be visually primary and clearly the answer,
    not a generic 'response' or exposed raw internal JSON."""
    response = client.get("/")

    assert "Final Answer" in response.text


def test_root_page_has_a_static_how_this_agent_works_explanation() -> None:
    response = client.get("/")

    body = response.text
    assert "How this agent works" in body
    assert "Triage Planner" in body
    assert "Report Writer" in body


def test_root_page_shows_how_this_agent_works_open_by_default() -> None:
    """Submission-facing explanatory content must be visible immediately,
    without requiring the grader to run the agent or expand anything."""
    response = client.get("/")

    assert '<details id="how-it-works" class="how-it-works" open>' in response.text


def test_root_page_links_to_the_architecture_endpoint() -> None:
    response = client.get("/")

    assert 'href="/api/model_architecture"' in response.text


def test_root_page_presents_llm_trace_as_secondary_detail() -> None:
    """The public execution trace stays available but must not be the main
    visual focus - it lives inside a collapsible <details> element."""
    response = client.get("/")

    assert "LLM Execution Trace" in response.text
    assert '<details id="steps-details">' in response.text
    assert '<details id="steps-details" open>' not in response.text


def test_root_page_keeps_new_conversation_wired() -> None:
    response = client.get("/")

    assert 'id="new-conversation-button"' in response.text


def test_static_app_js_has_no_internal_step_tracer_fields() -> None:
    """The GUI must only ever read the public {module, prompt, response}
    step shape - never internal StepTracer metadata."""
    response = client.get("/static/app.js")

    body = response.text
    assert "step.type" not in body
    assert "step.status" not in body
