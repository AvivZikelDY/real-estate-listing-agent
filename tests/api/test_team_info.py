from fastapi.testclient import TestClient

from src.listing_agent.main import app

client = TestClient(app)


def test_team_info_structure() -> None:
    response = client.get("/api/team_info")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"batch", "order", "team_name", "members"}
    assert isinstance(body["members"], list)
    assert len(body["members"]) >= 1
    for member in body["members"]:
        assert set(member.keys()) == {"name", "role", "email"}


def test_team_info_contains_required_placeholders() -> None:
    response = client.get("/api/team_info")
    body = response.json()

    required_placeholders = {
        "{BATCH}",
        "{ORDER}",
        "{TEAM_NAME}",
        "{AVIV_EMAIL}",
        "{NIR_FULL_NAME}",
        "{NIR_EMAIL}",
    }

    visible_values = {body["batch"], body["order"], body["team_name"]}
    for member in body["members"]:
        visible_values.add(member["name"])
        visible_values.add(member["email"])

    assert required_placeholders <= visible_values


def test_agent_info_structure() -> None:
    response = client.get("/api/agent_info")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "agent_name",
        "description",
        "version",
        "purpose",
        "prompt_templates",
        "prompt_examples",
    }
