from fastapi.testclient import TestClient

from src.listing_agent.main import app

client = TestClient(app)


def test_root_route_exists() -> None:
    response = client.get("/")
    assert response.status_code == 200


def test_health_route_exists() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_team_info_route_exists() -> None:
    response = client.get("/api/team_info")
    assert response.status_code == 200


def test_agent_info_route_exists() -> None:
    response = client.get("/api/agent_info")
    assert response.status_code == 200


def test_model_architecture_route_returns_png() -> None:
    response = client.get("/api/model_architecture")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_execute_route_exists() -> None:
    response = client.post("/api/execute", json={"prompt": "hello"})
    assert response.status_code == 200
