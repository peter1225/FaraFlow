import json
from pathlib import Path
from unittest.mock import AsyncMock

from faraflow.api.main import create_app
from faraflow.config import Settings
from faraflow.model.fara_adapter import ModelEndpointError
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


def test_chat_routes_between_direct_answer_and_browser_task(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.container.chat_model.complete_chat = AsyncMock(
            side_effect=[
                '{"mode":"chat","reply":"FaraFlow 是一个受控浏览器自动化平台。"}',
                (
                    '{"mode":"automation","reply":"我会打开浏览器查询。",'
                    '"task_name":"查询最新文档","description":"查询最新官方文档",'
                    '"start_url":"https://www.bing.com/",'
                    '"allowed_domains":["bing.com","example.com"]}'
                ),
            ]
        )

        first_chat = client.post("/v1/chats", json={"title": "新对话"})
        assert first_chat.status_code == 201
        first_id = first_chat.json()["chat_id"]
        direct = client.post(
            f"/v1/chats/{first_id}/messages",
            json={"content": "介绍一下 FaraFlow"},
        )
        assert direct.status_code == 200, direct.text
        assert direct.json()["route"] == "chat"
        assert direct.json()["task"] is None
        assert [item["role"] for item in direct.json()["chat"]["messages"]] == [
            "user",
            "assistant",
        ]

        second_chat = client.post("/v1/chats", json={"title": "网页查询"})
        second_id = second_chat.json()["chat_id"]
        automated = client.post(
            f"/v1/chats/{second_id}/messages",
            json={"content": "查询最新官方文档", "auto_start": False},
        )
        assert automated.status_code == 200, automated.text
        body = automated.json()
        assert body["route"] == "automation"
        assert body["task"]["status"] == "PLANNED"
        assert body["task"]["allowed_domains"] == ["bing.com", "example.com"]
        assert body["chat"]["messages"][-1]["task_id"] == body["task"]["task_id"]


def test_chat_returns_actionable_message_when_model_is_unavailable(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.container.chat_model.complete_chat = AsyncMock(
            side_effect=ModelEndpointError("connection failed")
        )
        created = client.post("/v1/chats", json={"title": "新对话"})
        chat_id = created.json()["chat_id"]

        response = client.post(
            f"/v1/chats/{chat_id}/messages",
            json={"content": "请介绍一下这个项目"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["route"] == "chat"
        assert body["task"] is None
        assert body["chat"]["messages"][-1]["metadata"] == {
            "error": "model_unavailable"
        }


def test_chat_streams_deltas_and_persists_the_final_answer(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
    )
    app = create_app(settings)

    streamed_arguments = {}

    async def stream_chat_events(*args, **kwargs):
        del args
        streamed_arguments.update(kwargs)
        yield {"type": "reasoning_delta", "content": "先分析"}
        yield {"type": "reasoning_done", "content": ""}
        yield {"type": "content_delta", "content": "流式"}
        yield {"type": "content_delta", "content": "回答"}

    with TestClient(app) as client:
        app.state.container.chat_model.complete_chat = AsyncMock(
            return_value='{"mode":"chat","reply":"ignored in favor of streaming"}'
        )
        app.state.container.chat_model.stream_chat_events = stream_chat_events
        created = client.post("/v1/chats", json={"title": "新对话"})
        chat_id = created.json()["chat_id"]

        with client.stream(
            "POST",
            f"/v1/chats/{chat_id}/messages/stream",
            json={
                "content": "测试流式输出",
                "requested_mode": "auto",
                "enable_thinking": True,
            },
        ) as response:
            assert response.status_code == 200, response.text
            events = [json.loads(line) for line in response.iter_lines() if line]

        assert events[0] == {"type": "route", "route": "chat"}
        assert [
            event["content"]
            for event in events
            if event["type"] == "reasoning_delta"
        ] == ["先分析"]
        assert any(event["type"] == "reasoning_done" for event in events)
        assert [event["content"] for event in events if event["type"] == "delta"] == [
            "流式",
            "回答",
        ]
        done = events[-1]
        assert done["type"] == "done"
        assert done["reply"]["route"] == "chat"
        final_message = done["reply"]["chat"]["messages"][-1]
        assert final_message["content"] == "流式回答"
        assert final_message["metadata"] == {
            "streamed": True,
            "thinking_enabled": True,
            "reasoning": "先分析",
        }
        assert streamed_arguments["enable_thinking"] is True
