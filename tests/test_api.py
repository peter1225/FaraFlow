from pathlib import Path

from faraflow.api.main import create_app
from faraflow.config import Settings
from fastapi.testclient import TestClient


def test_task_lifecycle_contracts(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/tasks",
            json={
                "task_name": "Test task",
                "description": "Find the latest product documentation without signing in.",
                "start_url": "https://www.bing.com/",
                "allowed_domains": ["bing.com", "example.com"],
            },
        )
        assert response.status_code == 201, response.text
        task = response.json()
        assert task["status"] == "PLANNED"
        assert task["session"]["state"] == "PLANNED"
        assert len(task["session"]["resume_token"]) >= 8
        assert len(task["plan"]) == 3

        task_id = task["task_id"]
        fetched = client.get(f"/v1/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["allowed_domains"] == ["bing.com", "example.com"]

        listing = client.get("/v1/tasks")
        assert listing.status_code == 200
        assert [item["task_id"] for item in listing.json()] == [task_id]

        events = client.get(f"/v1/tasks/{task_id}/events")
        assert events.status_code == 200
        assert events.json()[0]["event_type"] == "session.planned"

        skills = client.get("/v1/skills")
        assert skills.status_code == 200
        assert {item["skill_name"] for item in skills.json()} == {
            "browser.computer_use",
            "control.request_approval",
        }


def test_rejects_url_in_domain_allow_list(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/tasks",
            json={
                "task_name": "Invalid allow-list",
                "description": "This request should fail validation.",
                "allowed_domains": ["https://example.com"],
            },
        )
        assert response.status_code == 422
