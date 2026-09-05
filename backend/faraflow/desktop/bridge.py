import ctypes
import ctypes.wintypes
import io
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .protocol import DesktopAction


class DesktopBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    title: str
    process_name: str
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DesktopBridge:
    """Small Windows-only control bridge with a safe, testable interface.

    The bridge never launches a process and never elevates privileges. It only
    acts on the HWND selected by the user and uses UI-visible input events.
    """

    _KEYS = {
        "BACKSPACE": 0x08,
        "TAB": 0x09,
        "ENTER": 0x0D,
        "SHIFT": 0x10,
        "CTRL": 0x11,
        "ALT": 0x12,
        "PAUSE": 0x13,
        "ESC": 0x1B,
        "SPACE": 0x20,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "END": 0x23,
        "HOME": 0x24,
        "LEFT": 0x25,
        "UP": 0x26,
        "RIGHT": 0x27,
        "DOWN": 0x28,
        "INSERT": 0x2D,
        "DELETE": 0x2E,
        "WIN": 0x5B,
        "F1": 0x70,
        "F2": 0x71,
        "F3": 0x72,
        "F4": 0x73,
        "F5": 0x74,
        "F6": 0x75,
        "F7": 0x76,
        "F8": 0x77,
        "F9": 0x78,
        "F10": 0x79,
        "F11": 0x7A,
        "F12": 0x7B,
    }
    _KEY_ALIASES = {
        "ESCAPE": "ESC",
        "RETURN": "ENTER",
        "CONTROL": "CTRL",
        "WINDOWS": "WIN",
        "WINDOWSKEY": "WIN",
        "META": "WIN",
        "SUPER": "WIN",
        "SPACEBAR": "SPACE",
        "PGUP": "PAGEUP",
        "PGDN": "PAGEDOWN",
        "DEL": "DELETE",
        "INS": "INSERT",
        "ARROWLEFT": "LEFT",
        "ARROWRIGHT": "RIGHT",
        "ARROWUP": "UP",
        "ARROWDOWN": "DOWN",
    }

    def __init__(self) -> None:
        self._user32 = None
        self._kernel32 = None
        if os.name == "nt":
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32

    def _require_windows(self) -> None:
        if self._user32 is None:
            raise DesktopBridgeError("desktop control is only supported on Windows")

    def list_windows(self) -> List[WindowInfo]:
        self._require_windows()
        user32 = self._user32
        result: List[WindowInfo] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.strip()
            if not title:
                return True
            rect = self._rect(hwnd)
            if rect[2] <= 0 or rect[3] <= 0:
                return True
            result.append(
                WindowInfo(
                    window_id=int(hwnd),
                    title=title,
                    process_name=self._process_name(hwnd),
                    x=rect[0],
                    y=rect[1],
                    width=rect[2],
                    height=rect[3],
                )
            )
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return result

    def get_window(self, window_id: int) -> WindowInfo:
        for window in self.list_windows():
            if window.window_id == window_id:
                return window
        raise DesktopBridgeError(f"window {window_id} is not visible")

    def foreground_window(self) -> Optional[WindowInfo]:
        """Return the visible top-level foreground window, when it is controllable."""
        self._require_windows()
        window_id = int(self._user32.GetForegroundWindow() or 0)
        if not window_id or not self._user32.IsWindowVisible(window_id):
            return None
        length = self._user32.GetWindowTextLengthW(window_id)
        if length <= 0:
            return None
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(window_id, title_buffer, length + 1)
        title = title_buffer.value.strip()
        rect = self._rect(window_id)
        if not title or rect[2] <= 0 or rect[3] <= 0:
            return None
        return WindowInfo(
            window_id=window_id,
            title=title,
            process_name=self._process_name(window_id),
            x=rect[0],
            y=rect[1],
            width=rect[2],
            height=rect[3],
        )

    def followup_window(
        self,
        current: WindowInfo,
        *,
        previous_foreground_id: Optional[int],
        known_window_ids: Iterable[int],
    ) -> Optional[WindowInfo]:
        """Find an app window opened or activated by the most recent action.

        A foreground transition is preferred because it also handles an existing
        minimized application. If Windows does not grant foreground focus, a new
        sufficiently large top-level window is used as a conservative fallback.
        """
        if not self._is_desktop_target(current):
            # Stay inside the selected application process, but follow a new
            # main window that replaces a launcher/splash window.
            same_process = [
                window
                for window in self.list_windows()
                if window.window_id != current.window_id
                and current.process_name
                and window.process_name.casefold() == current.process_name.casefold()
            ]
            foreground = self.foreground_window()
            if foreground is not None and any(
                window.window_id == foreground.window_id for window in same_process
            ):
                return foreground
            if not same_process:
                return None
            largest = max(same_process, key=lambda window: window.width * window.height)
            if largest.width * largest.height > current.width * current.height:
                return largest
            return None
        foreground = self.foreground_window()
        if (
            foreground is not None
            and foreground.window_id != current.window_id
            and foreground.window_id != previous_foreground_id
            and not self._is_desktop_target(foreground)
        ):
            return foreground

        known = set(known_window_ids)
        candidates = [
            window
            for window in self.list_windows()
            if window.window_id not in known
            and window.window_id != current.window_id
            and not self._is_desktop_target(window)
            and window.width >= 240
            and window.height >= 160
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda window: window.width * window.height)

    def capture(self, window: WindowInfo) -> bytes:
        self._require_windows()
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise DesktopBridgeError(
                "desktop capture requires the optional Pillow dependency"
            ) from exc
        image = ImageGrab.grab(
            bbox=(window.x, window.y, window.x + window.width, window.y + window.height),
            all_screens=True,
        )
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def prepare_target(self, target: WindowInfo) -> None:
        """Bring the selected capture target into view before the first frame.

        The Windows shell exposes the desktop as the ``Program Manager``
        window, but capturing its full-screen rectangle alone would capture the
        currently foreground app. Use the shell's idempotent "minimize all"
        command for that special target; normal windows are focused directly.
        """
        self._require_windows()
        is_desktop = (
            target.process_name.casefold() == "explorer.exe"
            and target.title.casefold() in {"program manager", "windows desktop", "desktop"}
        )
        if is_desktop:
            shell_window = self._user32.FindWindowW("Shell_TrayWnd", None)
            if not shell_window:
                raise DesktopBridgeError("Windows desktop shell is not available")
            # WM_COMMAND / MIN_ALL. Unlike Win+D, this does not toggle existing
            # minimized windows back into the foreground on a retry.
            if not self._user32.PostMessageW(shell_window, 0x0111, 419, 0):
                raise DesktopBridgeError("failed to show the Windows desktop")
            time.sleep(0.5)
            self._focus_desktop_icons(target.window_id)
            time.sleep(0.2)
            return
        self._focus(target.window_id)
        time.sleep(0.2)

    def execute(self, action: DesktopAction, target: WindowInfo) -> Dict[str, Any]:
        self._require_windows()
        if action.action == "screenshot":
            return {"status": "ready"}
        if action.action == "list_windows":
            return {"windows": [item.as_dict() for item in self.list_windows()]}
        if action.action == "focus_window":
            target_id = action.window_id or target.window_id
            self._focus(target_id)
            return {"window_id": target_id, "status": "focused"}
        if action.action in {"click", "double_click", "right_click", "drag"}:
            assert action.coordinate is not None
            start = self._to_screen(action.coordinate, target)
            self._move(start)
            if (
                action.action != "drag"
                and self._is_desktop_target(target)
            ):
                # ``Program Manager`` is the explicitly selected target. Do not
                # gate its FolderView path on GetForegroundWindow(): Windows 11
                # frequently reports the desktop list view, WorkerW, the taskbar,
                # or a transient shell menu here even though the desktop is the
                # visible capture target. That caused desktop double-clicks to
                # fall back to unreliable generic mouse injection.
                self._focus_desktop_icons(target.window_id)
                if action.action == "double_click":
                    self._open_desktop_item(start)
                    return {
                        "coordinate": list(start),
                        "status": "executed",
                        "method": "desktop_context_open",
                    }
                self._post_desktop_pointer(action.action, start)
                return {"coordinate": list(start), "status": "executed"}
            if action.action == "drag":
                assert action.end_coordinate is not None
                self._mouse_button("left", down=True)
                end = self._to_screen(action.end_coordinate, target)
                self._move(end)
                self._mouse_button("left", down=False)
            else:
                button = "right" if action.action == "right_click" else "left"
                self._mouse_button(button, down=True)
                time.sleep(0.04)
                self._mouse_button(button, down=False)
                if action.action == "double_click":
                    # Give Explorer enough time to register two distinct clicks.
                    time.sleep(0.1)
                    self._mouse_button(button, down=True)
                    time.sleep(0.04)
                    self._mouse_button(button, down=False)
            return {"coordinate": list(start), "status": "executed"}
        if action.action == "scroll":
            self._move(self._to_screen(action.coordinate or (500, 500), target))
            self._wheel(int(action.pixels or 0))
            return {"pixels": action.pixels, "status": "executed"}
        if action.action == "type_text":
            self._type_text(action.text or "")
            return {"characters": len(action.text or ""), "status": "executed"}
        if action.action == "key_press":
            if self._is_desktop_target(target) and self._is_escape(action.keys):
                self._dismiss_desktop_menu(target.window_id)
            else:
                if self._is_desktop_target(target):
                    # Explorer must own keyboard focus for ENTER and navigation
                    # keys to activate the icon selected by pointer messages.
                    self._focus_desktop_icons(target.window_id)
                self._key_press(action.keys)
            return {"keys": list(action.keys), "status": "executed"}
        if action.action == "wait":
            time.sleep(action.seconds or 0)
            return {"seconds": action.seconds, "status": "executed"}
        raise DesktopBridgeError(f"unsupported desktop action: {action.action}")

    @staticmethod
    def _is_desktop_target(target: WindowInfo) -> bool:
        return (
            target.process_name.casefold() == "explorer.exe"
            and target.title.casefold()
            in {"program manager", "windows desktop", "desktop"}
        )

    def _desktop_surface_is_foreground(self) -> bool:
        foreground = self._user32.GetForegroundWindow()
        if not foreground:
            return False
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(foreground, buffer, len(buffer))
        return buffer.value in {"WorkerW", "Progman"}

    def _is_escape(self, keys: List[str]) -> bool:
        expanded = [part for key in keys for part in key.split("+") if part.strip()]
        return len(expanded) == 1 and self._virtual_key(expanded[0]) == self._KEYS["ESC"]

    def _dismiss_desktop_menu(self, program_manager_id: int) -> None:
        """Close an Explorer desktop context menu even when FolderView lost focus."""
        foreground = int(self._user32.GetForegroundWindow() or 0)
        view = int(self._desktop_list_view() or 0)
        recipients = []
        for window_id in (foreground, view, int(program_manager_id)):
            if window_id and window_id not in recipients:
                recipients.append(window_id)
        # WM_CANCELMODE asks the menu owner to leave its modal menu loop. The
        # explicit Escape messages cover modern shell surfaces whose popup menu
        # is implemented outside the classic FolderView window.
        for window_id in recipients:
            self._user32.PostMessageW(window_id, 0x001F, 0, 0)
            self._user32.PostMessageW(window_id, 0x0100, self._KEYS["ESC"], 0)
            self._user32.PostMessageW(window_id, 0x0101, self._KEYS["ESC"], 0)
        self._key_press(["ESC"])

    def _open_desktop_item(self, screen_point: Tuple[int, int]) -> None:
        """Select a desktop icon and invoke it inside Explorer's FolderView.

        Context menus are implemented differently across Windows 10/11 builds
        and may not expose a detectable top-level menu window. Explorer's icon
        list itself has stable semantics: select the model-chosen item, then
        deliver Return directly to that control.
        """
        view = self._desktop_list_view()
        if not view:
            raise DesktopBridgeError("Windows desktop icon view is not available")
        self._post_desktop_pointer("click", screen_point)
        time.sleep(0.08)
        # WM_KEYDOWN/WM_KEYUP are posted directly to FolderView, so activation
        # does not depend on Windows granting keyboard focus to the backend.
        for message in (0x0100, 0x0101):
            if not self._user32.PostMessageW(view, message, self._KEYS["ENTER"], 0):
                raise DesktopBridgeError("Explorer rejected the desktop open command")

    def _post_desktop_pointer(
        self, action_name: str, screen_point: Tuple[int, int]
    ) -> None:
        """Send pointer semantics to Explorer's real desktop FolderView.

        Absolute injected mouse movement works on modern Windows, but Explorer's
        WorkerW composition surface may consume button events after merely
        selecting an icon. Posting the canonical list-view messages preserves
        the model-selected coordinate while making click semantics deterministic.
        """
        view = self._desktop_list_view()
        if not view:
            raise DesktopBridgeError("Windows desktop icon view is not available")
        point = ctypes.wintypes.POINT(*screen_point)
        if not self._user32.ScreenToClient(view, ctypes.byref(point)):
            raise DesktopBridgeError("failed to map the desktop pointer coordinate")
        lparam = ((point.y & 0xFFFF) << 16) | (point.x & 0xFFFF)
        messages = {
            "click": ((0x0201, 1), (0x0202, 0)),
            "double_click": (
                (0x0201, 1),
                (0x0202, 0),
                (0x0203, 1),
                (0x0202, 0),
            ),
            "right_click": ((0x0204, 2), (0x0205, 0)),
        }[action_name]
        for message, button_state in messages:
            if not self._user32.PostMessageW(view, message, button_state, lparam):
                raise DesktopBridgeError("Windows rejected the desktop pointer action")

    @staticmethod
    def _to_screen(coordinate: Tuple[float, float], target: WindowInfo) -> Tuple[int, int]:
        x, y = coordinate
        return (
            target.x + round(target.width * x / 1000),
            target.y + round(target.height * y / 1000),
        )

    def _rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        rect = ctypes.wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (0, 0, 0, 0)
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def _process_name(self, hwnd: int) -> str:
        process_id = ctypes.wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return ""
        handle = self._kernel32.OpenProcess(0x1000 | 0x0400, False, process_id.value)
        if not handle:
            return ""
        try:
            size = ctypes.wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value.rsplit("\\", 1)[-1]
        finally:
            self._kernel32.CloseHandle(handle)

    def _focus(self, window_id: int) -> None:
        if not self._user32.IsWindow(window_id):
            raise DesktopBridgeError(f"window {window_id} no longer exists")
        self._user32.ShowWindow(window_id, 9)
        if self._wait_for_foreground(window_id, attempts=1):
            return

        # SetForegroundWindow is subject to Windows' foreground-lock policy.
        # Try the normal API first, but verify the actual foreground HWND rather
        # than treating its return value as authoritative.
        self._user32.SetForegroundWindow(window_id)
        if self._wait_for_foreground(window_id):
            return

        # A desktop run is initiated from the browser, so that browser commonly
        # owns the foreground input queue when the worker tries to reactivate the
        # explicitly selected application. Temporarily join the foreground and
        # target queues, activate only that selected HWND, and always detach.
        current_thread = (
            int(self._kernel32.GetCurrentThreadId()) if self._kernel32 is not None else 0
        )
        foreground_id = int(self._user32.GetForegroundWindow() or 0)
        attached_threads: List[int] = []
        if current_thread:
            candidate_threads: List[int] = []
            for candidate_window in (foreground_id, window_id):
                if not candidate_window:
                    continue
                thread_id = int(
                    self._user32.GetWindowThreadProcessId(candidate_window, None) or 0
                )
                if (
                    thread_id
                    and thread_id != current_thread
                    and thread_id not in candidate_threads
                ):
                    candidate_threads.append(thread_id)
            for thread_id in candidate_threads:
                if self._user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append(thread_id)

        try:
            self._user32.BringWindowToTop(window_id)
            self._user32.SetActiveWindow(window_id)
            self._user32.SetFocus(window_id)
            self._user32.SetForegroundWindow(window_id)
            if self._wait_for_foreground(window_id):
                return
        finally:
            for thread_id in reversed(attached_threads):
                self._user32.AttachThreadInput(current_thread, thread_id, False)

        foreground_id = int(self._user32.GetForegroundWindow() or 0)
        raise DesktopBridgeError(
            "Windows refused to focus the target window "
            f"(target={window_id}, foreground={foreground_id})"
        )

    def _wait_for_foreground(
        self, window_id: int, *, attempts: int = 3, delay: float = 0.05
    ) -> bool:
        for attempt in range(attempts):
            if int(self._user32.GetForegroundWindow() or 0) == window_id:
                return True
            if attempt + 1 < attempts:
                time.sleep(delay)
        return False

    def _focus_desktop_icons(self, program_manager_id: int) -> None:
        """Best-effort focus for both classic and modern Explorer desktops.

        Classic Explorer exposes ``SHELLDLL_DefView/SysListView32``. Recent
        Windows 11 builds can render the desktop through a modern shell surface
        without either child window, while still exposing ``Program Manager``.
        In that case the Program Manager HWND is the safest available fallback;
        the subsequent visible mouse click completes activation.
        """
        focus_target = self._desktop_list_view() or program_manager_id
        current_thread = self._kernel32.GetCurrentThreadId()
        desktop_thread = self._user32.GetWindowThreadProcessId(focus_target, None)
        attached = False
        try:
            if desktop_thread and desktop_thread != current_thread:
                attached = bool(
                    self._user32.AttachThreadInput(current_thread, desktop_thread, True)
                )
            # Both APIs are intentionally best-effort. SetForegroundWindow can
            # be denied by Windows foreground-lock rules, and SetFocus returns
            # the *previous* focus HWND (so zero is not an error).
            self._user32.SetForegroundWindow(program_manager_id)
            self._user32.SetFocus(focus_target)
        finally:
            if attached:
                self._user32.AttachThreadInput(current_thread, desktop_thread, False)

    def _desktop_list_view(self) -> int:
        result = 0
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def class_name(hwnd: int) -> str:
            buffer = ctypes.create_unicode_buffer(256)
            self._user32.GetClassNameW(hwnd, buffer, len(buffer))
            return buffer.value

        def window_title(hwnd: int) -> str:
            length = self._user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value

        def child_callback(hwnd: int, _: int) -> bool:
            nonlocal result
            hwnd = int(hwnd)
            if class_name(hwnd) == "SysListView32" and window_title(hwnd) == "FolderView":
                result = hwnd
                return False
            return not bool(result)

        def top_callback(hwnd: int, _: int) -> bool:
            hwnd = int(hwnd)
            if class_name(hwnd) not in {"Progman", "WorkerW"}:
                return True
            self._user32.EnumChildWindows(hwnd, enum_proc(child_callback), 0)
            return not bool(result)

        self._user32.EnumWindows(enum_proc(top_callback), 0)
        return result


    def _move(self, point: Tuple[int, int]) -> None:
        # SetCursorPos is rejected in some interactive Windows 11 sessions even
        # when screenshot capture and SendInput are available. Absolute mouse
        # input is the supported fallback and uses the complete virtual desktop
        # coordinate space, so it also works with secondary monitors.
        virtual_left = int(self._user32.GetSystemMetrics(76))
        virtual_top = int(self._user32.GetSystemMetrics(77))
        virtual_width = int(self._user32.GetSystemMetrics(78))
        virtual_height = int(self._user32.GetSystemMetrics(79))
        if virtual_width <= 1 or virtual_height <= 1:
            virtual_left = 0
            virtual_top = 0
            virtual_width = int(self._user32.GetSystemMetrics(0))
            virtual_height = int(self._user32.GetSystemMetrics(1))
        if virtual_width <= 1 or virtual_height <= 1:
            raise DesktopBridgeError("Windows returned an invalid desktop size")
        x, y = point
        if not (
            virtual_left <= x < virtual_left + virtual_width
            and virtual_top <= y < virtual_top + virtual_height
        ):
            raise DesktopBridgeError("mouse coordinate is outside the Windows desktop")
        normalized_x = round((x - virtual_left) * 65535 / (virtual_width - 1))
        normalized_y = round((y - virtual_top) * 65535 / (virtual_height - 1))
        self._user32.mouse_event(
            0x0001 | 0x8000 | 0x4000,
            normalized_x,
            normalized_y,
            0,
            0,
        )

    def _mouse_button(self, button: str, *, down: bool) -> None:
        flags = {
            ("left", True): 0x0002,
            ("left", False): 0x0004,
            ("right", True): 0x0008,
            ("right", False): 0x0010,
        }
        self._user32.mouse_event(flags[(button, down)], 0, 0, 0, 0)

    def _wheel(self, amount: int) -> None:
        self._user32.mouse_event(0x0800, 0, 0, amount * 120, 0)

    def _type_text(self, text: str) -> None:
        for char in text:
            self._send_key(0, ord(char), unicode=True)
            self._send_key(0, ord(char), unicode=True, key_up=True)

    def _key_press(self, keys: List[str]) -> None:
        # Vision models may return either ["CTRL", "L"] or ["Ctrl+L"].
        # Accept both representations while keeping policy checks on the
        # original action payload.
        expanded_keys = [part for key in keys for part in key.split("+") if part.strip()]
        virtual_keys = [self._virtual_key(key) for key in expanded_keys]
        for key in virtual_keys:
            self._send_key(key, 0)
        for key in reversed(virtual_keys):
            self._send_key(key, 0, key_up=True)

    def _virtual_key(self, key: str) -> int:
        normalized = key.strip().upper()
        compact = normalized.replace(" ", "").replace("_", "").replace("-", "")
        canonical = self._KEY_ALIASES.get(compact, compact)
        if canonical in self._KEYS:
            return self._KEYS[canonical]
        if len(normalized) == 1 and normalized.isprintable():
            return ord(normalized)
        raise DesktopBridgeError(f"unsupported key: {key}")

    def _send_key(
        self, virtual_key: int, scan_code: int, *, unicode: bool = False, key_up: bool = False
    ) -> None:
        # INPUT/KEYBDINPUT are declared locally to keep importing FaraFlow safe
        # on non-Windows test runners.
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.wintypes.WORD),
                ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.wintypes.LONG),
                ("dy", ctypes.wintypes.LONG),
                ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", ctypes.wintypes.DWORD),
                ("wParamL", ctypes.wintypes.WORD),
                ("wParamH", ctypes.wintypes.WORD),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [("type", ctypes.wintypes.DWORD), ("value", INPUTUNION)]

        flags = (0x0004 if unicode else 0) | (0x0002 if key_up else 0)
        entry = INPUT(type=1, value=INPUTUNION(ki=KEYBDINPUT(virtual_key, scan_code, flags, 0, 0)))
        if self._user32.SendInput(1, ctypes.byref(entry), ctypes.sizeof(INPUT)) != 1:
            raise DesktopBridgeError("Windows rejected the keyboard input")
