from fastapi.testclient import TestClient

from src.listing_agent.main import app

client = TestClient(app)


def test_team_info_structure() -> None:
    response = client.get("/api/team_info")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"group_batch_order_number", "team_name", "students"}
    assert isinstance(body["students"], list)
    assert len(body["students"]) >= 1
    for student in body["students"]:
        assert set(student.keys()) == {"name", "email"}


def test_team_info_values() -> None:
    response = client.get("/api/team_info")
    body = response.json()

    assert body["group_batch_order_number"] == "1_8"
    assert body["team_name"] == "Group H"
    assert body["students"] == [
        {"name": "Aviv Zikel", "email": "zikel3011@gmail.com"},
        {"name": "Nir Kaplan", "email": "kaplanir24@gmail.com"},
    ]


def test_team_info_contains_no_placeholders() -> None:
    response = client.get("/api/team_info")
    body = response.json()

    placeholder_markers = ("{", "}")

    visible_values = [body["group_batch_order_number"], body["team_name"]]
    for student in body["students"]:
        visible_values.append(student["name"])
        visible_values.append(student["email"])

    for value in visible_values:
        assert not any(marker in value for marker in placeholder_markers), value


def test_agent_info_structure() -> None:
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
