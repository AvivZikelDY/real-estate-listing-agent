from fastapi.testclient import TestClient

from src.listing_agent.main import app

client = TestClient(app)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_model_architecture_returns_png_content_type() -> None:
    response = client.get("/api/model_architecture")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_model_architecture_body_starts_with_png_signature() -> None:
    response = client.get("/api/model_architecture")

    assert response.content[:8] == PNG_SIGNATURE


def test_model_architecture_response_is_not_json() -> None:
    response = client.get("/api/model_architecture")

    assert "application/json" not in response.headers["content-type"]
