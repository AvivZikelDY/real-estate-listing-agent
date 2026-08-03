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
