from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from faraflow.domain.enums import SessionState
from faraflow.model.fara_protocol import ComputerAction, ModelDecision
from faraflow.runtime.agent_runtime import AgentRuntime


class FakeRepository:
    def __init__(self) -> None:
        self.task = SimpleNamespace(
            task_id="task_1",
            session_id="sess_1",
            description="Read the page",
            start_url="https://example.com/",
            allowed_domains=["example.com"],
            approval_policy={},
            runtime_policy={
                "max_model_actions": 10,
                "max_runtime_minutes": 1,
                "max_step_retries": 1,
            },
            status=SessionState.PLANNED.value,
        )
        self.session = SimpleNamespace(
            session_id="sess_1",
            state=SessionState.PLANNED.value,
            runtime_state={},
        )

    async def get_task(self, task_id: str) -> Any:
        assert task_id == self.task.task_id
        return self.task

    async def get_session(self, session_id: str) -> Any:
        assert session_id == self.session.session_id
        return self.session

    async def update_state(
        self,
        task_id: str,
        session_id: str,
        state: SessionState,
        *,
        runtime_state: dict[str, Any] | None = None,
        last_screenshot_ref: str | None = None,
        final_result: dict[str, Any] | None = None,
    ) -> None:
        del task_id, session_id, last_screenshot_ref, final_result
        self.task.status = state.value
        self.session.state = state.value
        if runtime_state is not None:
            self.session.runtime_state = runtime_state

    async def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return SimpleNamespace(
            event_id=f"evt_{event_type}",
            session_id=session_id,
            event_type=event_type,
            message=message,
            payload=payload or {},
            created_at=datetime.now(timezone.utc),
        )


class BlockingFara:
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.settings = SimpleNamespace(
            fara_model="microsoft/Fara1.5-9B",
            browser_viewport_width=1440,
            browser_viewport_height=900,
        )

    def initial_user_message(self, goal: str, screenshot: bytes) -> dict[str, Any]:
        del goal, screenshot
        return {"role": "user", "content": []}

    async def next_action(self, conversation: Any) -> ModelDecision:
        del conversation
        self.called.set()
        await self.release.wait()
        return ModelDecision(
            action=ComputerAction(action="left_click", coordinate=(500, 500)),
            raw_response=(
                '<tool_call>{"name":"computer_use","arguments":'
                '{"action":"left_click","coordinate":[500,500]}}</tool_call>'
            ),
        )


class FakeBrowserPool:
    def __init__(self) -> None:
        self.executions = 0
        self.checkpoints = 0

    async def ensure_session(self, *args: Any) -> None:
        del args

    async def screenshot(self, session_id: str, name: str) -> Any:
        return b"png", f"/v1/artifacts/{session_id}/screenshots/{name}"

    async def visible_text(self, session_id: str) -> str:
        del session_id
        return ""

    async def checkpoint(self, session_id: str) -> None:
        del session_id
        self.checkpoints += 1

    async def execute(self, session_id: str, action: ComputerAction) -> Any:
        del session_id, action
        self.executions += 1
        raise AssertionError("a paused runtime must not execute the pending model action")

    async def close_session(self, session_id: str) -> None:
        del session_id


class FakeEventBus:
    async def publish(self, event: Any) -> None:
        del event


@pytest.mark.asyncio
async def test_pause_during_model_call_blocks_the_pending_action() -> None:
    repository = FakeRepository()
    browser = FakeBrowserPool()
    fara = BlockingFara()
    runtime = AgentRuntime(repository, browser, fara, FakeEventBus())  # type: ignore[arg-type]

    await runtime.start("task_1")
    await asyncio.wait_for(fara.called.wait(), timeout=1)
    await runtime.pause("task_1")
    fara.release.set()
    await asyncio.wait_for(runtime._jobs["sess_1"], timeout=1)

    assert repository.session.state == SessionState.PAUSED.value
    assert browser.executions == 0
    assert browser.checkpoints == 1
