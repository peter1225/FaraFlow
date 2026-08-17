from pathlib import Path

import pytest
from faraflow.api.main import create_app
from faraflow.config import Settings
from faraflow.desktop.adapter import DesktopAdapter
from faraflow.desktop.bridge import DesktopBridge, DesktopBridgeError, WindowInfo
from faraflow.desktop.policy import DesktopPolicy, DesktopPolicyError
from faraflow.desktop.protocol import (
    DesktopAction,
    build_desktop_response_format,
    build_desktop_system_prompt,
    parse_desktop_response,
)
from faraflow.desktop.runtime import DesktopRuntime
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

    handoff = parse_desktop_response(
        '{"action":"handoff","answer":"Verification code required"}'
    )
    assert handoff.kind == "handoff"
    assert handoff.answer == "Verification code required"


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
    assert "answer" in variants["handoff"]["required"]
    assert "use key_press" in build_desktop_system_prompt()
    assert "visible Login/Sign in button" in build_desktop_system_prompt()
    assert "must click the visible login" in build_desktop_system_prompt()

    forced_schema = build_desktop_response_format(allow_terminal=False)["json_schema"][
        "schema"
    ]
    forced_actions = {
        item["properties"]["action"]["enum"][0]
        for item in forced_schema["oneOf"]
    }
    assert "click" in forced_actions
    assert "final" not in forced_actions
    assert "handoff" not in forced_actions


def test_desktop_runtime_detects_blocker_summaries() -> None:
    assert DesktopRuntime._looks_like_blocker("Please manually log in first")
    assert DesktopRuntime._looks_like_blocker("需要您输入验证码")
    assert not DesktopRuntime._looks_like_blocker(
        "Message sent to up_up and confirmed visible in the conversation"
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Escape", 0x1B),
        ("ESC", 0x1B),
        ("Return", 0x0D),
        ("Control", 0x11),
        ("Page Down", 0x22),
        ("Arrow_Left", 0x25),
    ],
)
def test_desktop_bridge_accepts_common_key_aliases(name: str, expected: int) -> None:
    bridge = object.__new__(DesktopBridge)

    assert bridge._virtual_key(name) == expected


def test_desktop_bridge_accepts_compound_key_notation(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    events: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        bridge,
        "_send_key",
        lambda key, _scan, *, key_up=False, **_kwargs: events.append((key, key_up)),
    )

    bridge._key_press(["Ctrl+L"])

    assert events == [(0x11, False), (ord("L"), False), (ord("L"), True), (0x11, True)]


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


def test_desktop_double_click_uses_context_open_on_folder_view(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    bridge._user32 = object()
    bridge._kernel32 = None
    opened: list[tuple[int, int]] = []
    focused: list[int] = []
    monkeypatch.setattr(bridge, "_move", lambda _point: None)
    monkeypatch.setattr(bridge, "_desktop_surface_is_foreground", lambda: True)
    monkeypatch.setattr(bridge, "_focus_desktop_icons", focused.append)
    monkeypatch.setattr(bridge, "_open_desktop_item", opened.append)

    bridge.execute(
        DesktopAction(action="double_click", coordinate=(100, 200)),
        WindowInfo(1, "Program Manager", "explorer.exe", 0, 0, 1920, 1080),
    )

    assert opened == [(192, 216)]
    assert focused == [1]


def test_desktop_context_open_selects_item_and_posts_enter(
    monkeypatch,
) -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.messages: list[tuple[int, int, int, int]] = []

        def PostMessageW(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
            self.messages.append((hwnd, message, wparam, lparam))
            return 1

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    actions: list[tuple[str, tuple[int, int]]] = []
    monkeypatch.setattr(bridge, "_desktop_list_view", lambda: 77)
    monkeypatch.setattr(
        bridge,
        "_post_desktop_pointer",
        lambda action, point: actions.append((action, point)),
    )
    monkeypatch.setattr("faraflow.desktop.bridge.time.sleep", lambda _seconds: None)

    bridge._open_desktop_item((115, 649))

    assert actions == [("click", (115, 649))]
    assert bridge._user32.messages == [
        (77, 0x0100, 0x0D, 0),
        (77, 0x0101, 0x0D, 0),
    ]


def test_desktop_context_open_fails_without_folder_view(
    monkeypatch,
) -> None:
    bridge = object.__new__(DesktopBridge)
    monkeypatch.setattr(bridge, "_desktop_list_view", lambda: 0)

    with pytest.raises(DesktopBridgeError, match="icon view is not available"):
        bridge._open_desktop_item((115, 649))


def test_desktop_escape_cancels_shell_menu_and_sends_escape(monkeypatch) -> None:
    class FakeUser32:
        def __init__(self) -> None:
            self.messages: list[tuple[int, int, int, int]] = []

        @staticmethod
        def GetForegroundWindow() -> int:
            return 55

        def PostMessageW(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
            self.messages.append((hwnd, message, wparam, lparam))
            return 1

    bridge = object.__new__(DesktopBridge)
    bridge._user32 = FakeUser32()
    bridge._kernel32 = None
    keys: list[list[str]] = []
    monkeypatch.setattr(bridge, "_desktop_list_view", lambda: 66)
    monkeypatch.setattr(bridge, "_key_press", lambda value: keys.append(value))

    bridge.execute(
        DesktopAction(action="key_press", keys=["Escape"]),
        WindowInfo(77, "Program Manager", "explorer.exe", 0, 0, 1920, 1080),
    )

    assert keys == [["ESC"]]
    assert bridge._user32.messages == [
        (55, 0x001F, 0, 0),
        (55, 0x0100, 0x1B, 0),
        (55, 0x0101, 0x1B, 0),
        (66, 0x001F, 0, 0),
        (66, 0x0100, 0x1B, 0),
        (66, 0x0101, 0x1B, 0),
        (77, 0x001F, 0, 0),
        (77, 0x0100, 0x1B, 0),
        (77, 0x0101, 0x1B, 0),
    ]


def test_desktop_enter_refocuses_folder_view(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    bridge._user32 = object()
    bridge._kernel32 = None
    focused: list[int] = []
    keys: list[list[str]] = []
    monkeypatch.setattr(bridge, "_focus_desktop_icons", focused.append)
    monkeypatch.setattr(bridge, "_key_press", lambda value: keys.append(value))

    bridge.execute(
        DesktopAction(action="key_press", keys=["ENTER"]),
        WindowInfo(77, "Program Manager", "explorer.exe", 0, 0, 1920, 1080),
    )

    assert focused == [77]
    assert keys == [["ENTER"]]


def test_desktop_bridge_follows_new_foreground_application(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    desktop = WindowInfo(1, "Program Manager", "explorer.exe", 0, 0, 1920, 1080)
    qq = WindowInfo(2, "QQ", "QQ.exe", 400, 200, 900, 700)
    monkeypatch.setattr(bridge, "foreground_window", lambda: qq)

    assert bridge.followup_window(
        desktop,
        previous_foreground_id=desktop.window_id,
        known_window_ids={desktop.window_id},
    ) == qq


def test_desktop_bridge_ignores_unchanged_foreground(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    desktop = WindowInfo(1, "Program Manager", "explorer.exe", 0, 0, 1920, 1080)
    browser = WindowInfo(2, "FaraFlow", "chrome.exe", 100, 100, 1200, 800)
    monkeypatch.setattr(bridge, "foreground_window", lambda: browser)
    monkeypatch.setattr(bridge, "list_windows", lambda: [desktop, browser])

    assert (
        bridge.followup_window(
            desktop,
            previous_foreground_id=browser.window_id,
            known_window_ids={desktop.window_id, browser.window_id},
        )
        is None
    )


def test_desktop_bridge_does_not_follow_away_from_selected_application(
    monkeypatch,
) -> None:
    bridge = object.__new__(DesktopBridge)
    qq = WindowInfo(2, "QQ", "QQ.exe", 400, 200, 900, 700)
    browser = WindowInfo(3, "FaraFlow", "chrome.exe", 100, 100, 1200, 800)
    monkeypatch.setattr(bridge, "foreground_window", lambda: browser)
    monkeypatch.setattr(bridge, "list_windows", lambda: [qq, browser])

    assert bridge.followup_window(
        qq,
        previous_foreground_id=qq.window_id,
        known_window_ids={qq.window_id},
    ) is None


def test_desktop_bridge_follows_larger_window_of_same_application(monkeypatch) -> None:
    bridge = object.__new__(DesktopBridge)
    launcher = WindowInfo(2, "QQ", "QQ.exe", 700, 300, 320, 420)
    main = WindowInfo(3, "QQ", "QQ.exe", 400, 150, 1000, 760)
    monkeypatch.setattr(bridge, "foreground_window", lambda: main)
    monkeypatch.setattr(bridge, "list_windows", lambda: [launcher, main])

    assert bridge.followup_window(
        launcher,
        previous_foreground_id=launcher.window_id,
        known_window_ids={launcher.window_id},
    ) == main


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


def test_desktop_policy_unattended_mode_removes_approval_pauses() -> None:
    policy = DesktopPolicy(require_confirmation=False, unattended_mode=True)

    assert not policy.requires_approval(DesktopAction(action="key_press", keys=["WIN"]))
    assert not policy.requires_approval(
        DesktopAction(action="type_text", text="hello", sensitive=True)
    )
    assert not policy.requires_approval(
        DesktopAction(action="right_click", coordinate=(500, 500))
    )


@pytest.mark.parametrize("keys", [["WIN", "R"], ["WIN", "L"], ["CTRL", "ALT", "DELETE"]])
def test_desktop_policy_unattended_mode_still_blocks_security_shortcuts(
    keys: list[str],
) -> None:
    policy = DesktopPolicy(require_confirmation=False, unattended_mode=True)

    with pytest.raises(DesktopPolicyError, match="not available"):
        policy.check_action(DesktopAction(action="key_press", keys=keys), 1)


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


def test_desktop_policy_allows_focus_within_same_application() -> None:
    policy = DesktopPolicy()
    current = WindowInfo(10, "QQ Login", "QQ.exe", 0, 0, 320, 420)
    candidate = WindowInfo(11, "QQ", "QQ.exe", 100, 50, 1000, 760)

    policy.validate_focus_transition(current, candidate)


def test_desktop_policy_rejects_focus_to_different_application() -> None:
    policy = DesktopPolicy()
    current = WindowInfo(10, "QQ", "QQ.exe", 0, 0, 1000, 760)
    candidate = WindowInfo(11, "Terminal", "cmd.exe", 100, 50, 800, 600)

    with pytest.raises(DesktopPolicyError, match="selected application"):
        policy.validate_focus_transition(current, candidate)


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
