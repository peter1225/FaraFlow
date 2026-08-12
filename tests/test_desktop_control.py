from pathlib import Path

import pytest
from faraflow.api.main import create_app
from faraflow.config import Settings
from faraflow.desktop.adapter import DesktopAdapter
from faraflow.desktop.bridge import DesktopBridge, WindowInfo
from faraflow.desktop.policy import DesktopPolicy, DesktopPolicyError
from faraflow.desktop.protocol import (
    DesktopAction,
    build_desktop_response_format,
    build_desktop_system_prompt,
    parse_desktop_response,
)
from faraflow.runtime.chat_service import ChatService
from fastapi.testclient import TestClient


class FakeDesktopResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeDesktopClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    async def post(self, path: str, *, json: dict) -> FakeDesktopResponse:
        del path
        self.payloads.append(json)
        return FakeDesktopResponse(self.responses.pop(0))


def test_desktop_protocol_accepts_action_and_final_blocks() -> None:
    decision = parse_desktop_response(
        '<desktop_action>{"action":"click","x":100,"y":200}</desktop_action>'
    )
    assert decision.kind == "action"
    assert decision.action is not None
    assert decision.action.coordinate == (100.0, 200.0)

    final = parse_desktop_response("<final>Done</final>")
    assert final.kind == "final"
    assert final.answer == "Done"


def test_desktop_protocol_accepts_fara_native_tool_calls() -> None:
    direct = parse_desktop_response(
        "I will click File Explorer.\n"
        '<desktop_action>{"action":"left_click","coordinate":[345,978]}'
        "</desktop_action>"
    )
    assert direct.action is not None
    assert direct.action.action == "click"

    decision = parse_desktop_response(
        "The next step is to click File Explorer.\n"
        '<tool_call>{"name":"computer_use","arguments":'
        '{"action":"left_click","coordinate":[345,978]}}</tool_call>'
    )
    assert decision.kind == "action"
    assert decision.action is not None
    assert decision.action.action == "click"
    assert decision.action.coordinate == (345.0, 978.0)

    wait = parse_desktop_response(
        '<TOOL_CALL>```json\n{"name":"computer_use","arguments":'
        '{"action":"wait","time":2}}\n```</TOOL_CALL>'
    )
    assert wait.action is not None
    assert wait.action.action == "wait"
    assert wait.action.seconds == 2

    done = parse_desktop_response(
        '<tool_call>{"name":"computer_use","arguments":'
        '{"action":"terminate","answer":"Folder created"}}</tool_call>'
    )
    assert done.kind == "final"
    assert done.answer == "Folder created"


def test_desktop_protocol_defaults_harmless_wait_duration() -> None:
    decision = parse_desktop_response('{"action":"wait","coordinate":[735,976]}')

    assert decision.action is not None
    assert decision.action.action == "wait"
    assert decision.action.seconds == 1


def test_desktop_response_schema_requires_action_specific_arguments() -> None:
    schema = build_desktop_response_format()["json_schema"]["schema"]
    variants = {
        item["properties"]["action"]["enum"][0]: item for item in schema["oneOf"]
    }

    assert "seconds" in variants["wait"]["required"]
    assert "coordinate" in variants["click"]["required"]
    assert "answer" in variants["final"]["required"]
    assert "use key_press" in build_desktop_system_prompt()


def test_desktop_bridge_prepares_program_manager_as_real_desktop(monkeypatch) -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.messages: list[tuple[int, int, int, int]] = []

        def FindWindowW(self, class_name: str, title: None) -> int:
            assert class_name == "Shell_TrayWnd"
            assert title is None
            return 99

        def PostMessageW(self, hwnd: int, message: int, command: int, value: int) -> int:
            self.messages.append((hwnd, message, command, value))
            return 1

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    bridge._kernel32 = None
    focused: list[int] = []
    monkeypatch.setattr(bridge, "_focus_desktop_icons", focused.append)
    monkeypatch.setattr("faraflow.desktop.bridge.time.sleep", lambda _seconds: None)

    bridge.prepare_target(WindowInfo(1, "Program Manager", "explorer.exe", 0, 0, 1920, 1080))

    assert bridge._user32.messages == [(99, 0x0111, 419, 0)]
    assert focused == [1]


def test_desktop_bridge_uses_full_windows_input_structure() -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.structure_sizes: list[int] = []

        def SendInput(self, count: int, _entry, structure_size: int) -> int:
            assert count == 1
            self.structure_sizes.append(structure_size)
            return 1

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    bridge._kernel32 = None

    bridge._send_key(0, ord("a"), unicode=True)

    assert bridge._user32.structure_sizes == [40]


def test_desktop_bridge_falls_back_to_program_manager_on_modern_windows(
    monkeypatch,
) -> None:
    class FakeKernel32:
        @staticmethod
        def GetCurrentThreadId() -> int:
            return 7

    class FakeUser32:
        def __init__(self) -> None:
            self.foreground: list[int] = []
            self.focused: list[int] = []

        @staticmethod
        def GetWindowThreadProcessId(_hwnd: int, _process_id: None) -> int:
            return 7

        def SetForegroundWindow(self, hwnd: int) -> int:
            self.foreground.append(hwnd)
            return 0

        def SetFocus(self, hwnd: int) -> int:
            self.focused.append(hwnd)
            return 0

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    bridge._kernel32 = FakeKernel32()
    monkeypatch.setattr(bridge, "_desktop_list_view", lambda: 0)

    bridge._focus_desktop_icons(123)

    assert bridge._user32.foreground == [123]
    assert bridge._user32.focused == [123]


def test_desktop_bridge_moves_with_absolute_virtual_desktop_input() -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.events: list[tuple[int, int, int, int, int]] = []

        @staticmethod
        def GetSystemMetrics(index: int) -> int:
            return {76: -1920, 77: 0, 78: 3840, 79: 1080}[index]

        def mouse_event(
            self, flags: int, x: int, y: int, data: int, extra: int
        ) -> None:
            self.events.append((flags, x, y, data, extra))

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    bridge._kernel32 = None

    bridge._move((0, 540))

    flags, x, y, data, extra = bridge._user32.events[0]
    assert flags == 0x0001 | 0x8000 | 0x4000
    assert x == pytest.approx(32776, abs=1)
    assert y == pytest.approx(32798, abs=1)
    assert (data, extra) == (0, 0)


def test_desktop_double_click_sends_exactly_two_clicks(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    bridge._user32 = object()
    bridge._kernel32 = None
    buttons: list[tuple[str, bool]] = []
    monkeypatch.setattr(bridge, "_move", lambda _point: None)
    monkeypatch.setattr(
        bridge,
        "_mouse_button",
        lambda button, *, down: buttons.append((button, down)),
    )
    monkeypatch.setattr("faraflow.desktop.bridge.time.sleep", lambda _seconds: None)

    bridge.execute(
        DesktopAction(action="double_click", coordinate=(100, 200)),
        WindowInfo(1, "Editor", "editor.exe", 0, 0, 1920, 1080),
    )

    assert buttons == [
        ("left", True),
        ("left", False),
        ("left", True),
        ("left", False),
    ]


def test_desktop_pointer_uses_folder_view_messages(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    bridge._user32 = object()
    bridge._kernel32 = None
    posted: list[tuple[str, tuple[int, int]]] = []
    monkeypatch.setattr(bridge, "_move", lambda _point: None)
    monkeypatch.setattr(bridge, "_desktop_surface_is_foreground", lambda: True)
    monkeypatch.setattr(
        bridge,
        "_post_desktop_pointer",
        lambda action, point: posted.append((action, point)),
    )

    bridge.execute(
        DesktopAction(action="double_click", coordinate=(100, 200)),
        WindowInfo(1, "Program Manager", "explorer.exe", 0, 0, 1920, 1080),
    )

    assert posted == [("double_click", (192, 216))]


@pytest.mark.asyncio
async def test_desktop_adapter_requests_structured_json() -> None:
    settings = Settings(
        _env_file=None,
        desktop_base_url="http://127.0.0.1:9/v1",
        desktop_model="fake-desktop-model",
    )
    adapter = DesktopAdapter(settings)
    await adapter._client.aclose()
    fake = FakeDesktopClient(['{"action":"click","coordinate":[345,978]}'])
    adapter._client = fake  # type: ignore[assignment]

    decision = await adapter.next_decision([])

    assert decision.action is not None
    assert decision.action.action == "click"
    response_format = fake.payloads[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_desktop_adapter_keeps_only_recent_screenshots() -> None:
    settings = Settings(
        _env_file=None,
        desktop_base_url="http://127.0.0.1:9/v1",
        desktop_model="fake-desktop-model",
        desktop_max_screenshots=3,
    )
    adapter = DesktopAdapter(settings)
    await adapter._client.aclose()
    fake = FakeDesktopClient(['{"action":"wait","seconds":1}'])
    adapter._client = fake  # type: ignore[assignment]
    conversation = [adapter.observation_message(str(index), b"png") for index in range(5)]

    await adapter.next_decision(conversation)

    image_count = sum(
        1
        for message in fake.payloads[0]["messages"]
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "image_url"
    )
    assert image_count == 3


def test_desktop_policy_requires_approval_for_sensitive_or_dangerous_actions() -> None:
    policy = DesktopPolicy(require_confirmation=True)
    sensitive = DesktopAction(action="type_text", text="password", sensitive=True)
    dangerous = DesktopAction(action="key_press", keys=["ALT", "F4"])
    safe = DesktopAction(action="click", coordinate=(500, 500))

    assert policy.requires_approval(sensitive)
    assert policy.requires_approval(dangerous)
    assert not policy.requires_approval(safe)
    assert policy.audit_arguments(sensitive)["text"] == "<redacted length=8>"


def test_desktop_policy_keeps_system_shortcuts_guarded_in_auto_mode() -> None:
    policy = DesktopPolicy(require_confirmation=False)

    assert policy.requires_approval(DesktopAction(action="key_press", keys=["WIN"]))
    assert policy.requires_approval(
        DesktopAction(action="key_press", keys=["WIN", "R"])
    )
    assert not policy.requires_approval(
        DesktopAction(action="key_press", keys=["ENTER"])
    )


def test_desktop_policy_cannot_focus_another_window() -> None:
    policy = DesktopPolicy()
    target = WindowInfo(10, "Editor", "editor.exe", 0, 0, 800, 600)
    action = DesktopAction(action="focus_window", window_id=11)

    try:
        policy.validate_window_target(action, target)
    except DesktopPolicyError:
        pass
    else:
        raise AssertionError("expected selected-window escape to be rejected")


def test_desktop_route_is_opt_in() -> None:
    assert ChatService.parse_route(
        "", "控制我的电脑打开记事本", has_desktop=False
    ).mode == "chat"
    assert ChatService.parse_route(
        "", "控制我的电脑打开记事本", has_desktop=True
    ).mode == "desktop"


def test_desktop_api_is_disabled_by_default(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        api_host="127.0.0.1",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'desktop.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
        code_work_root=tmp_path / "code-workspaces",
        enable_desktop_control=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/desktop-runs",
            json={"instruction": "open the calculator"},
        )
    assert response.status_code == 409


def test_desktop_chat_creates_waiting_run_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        api_host="127.0.0.1",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'desktop-enabled.db').as_posix()}",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
        code_work_root=tmp_path / "code-workspaces",
        enable_desktop_control=True,
        desktop_base_url="http://127.0.0.1:9/v1",
        desktop_model="fake-desktop-model",
    )
    with TestClient(create_app(settings)) as client:
        chat = client.post("/v1/chats", json={"title": "desktop test"}).json()
        response = client.post(
            f"/v1/chats/{chat['chat_id']}/messages",
            json={
                "content": "请控制我的电脑打开记事本",
                "requested_mode": "desktop",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "desktop"
    assert body["desktop_run"]["status"] == "WAITING_CAPTURE_CONSENT"
    assert body["desktop_run"]["target_window_id"] is None
    assert any(
        message["mode"] == "desktop" and message["desktop_run_id"]
        for message in body["chat"]["messages"]
    )
