import re
from typing import Any, Dict, Iterable, Optional, Tuple

from .bridge import WindowInfo
from .protocol import DesktopAction


class DesktopPolicyError(PermissionError):
    pass


class DesktopPolicy:
    """Allow-list and approval policy for host desktop actions."""

    _dangerous_keys = re.compile(
        r"(?:ALT\s*\+\s*F4|CTRL\s*\+\s*(?:W|Q|DELETE|SHIFT\s*\+\s*ESC)|"
        r"WIN(?:\s*\+\s*[A-Z0-9]+)?|CTRL\s*\+\s*ALT\s*\+\s*DELETE)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        allowed_apps: Iterable[str] = (),
        max_actions: int = 50,
        require_confirmation: bool = True,
    ) -> None:
        self.allowed_apps = {item.strip().lower() for item in allowed_apps if item.strip()}
        self.max_actions = max_actions
        self.require_confirmation = require_confirmation

    def validate_target(self, target: Optional[WindowInfo]) -> WindowInfo:
        if target is None:
            raise DesktopPolicyError("a target window must be selected before starting")
        if not target.title.strip():
            raise DesktopPolicyError("target window has no visible title")
        if self.allowed_apps and target.process_name.lower() not in self.allowed_apps:
            raise DesktopPolicyError(
                f"target application {target.process_name or '<unknown>'!r} is not allowed"
            )
        if target.width <= 0 or target.height <= 0:
            raise DesktopPolicyError("target window has an invalid rectangle")
        return target

    def check_action(self, action: DesktopAction, step_no: int) -> None:
        if step_no > self.max_actions:
            raise DesktopPolicyError("desktop run exceeded the maximum action count")
        if action.action in {"screenshot", "list_windows", "focus_window", "wait", "scroll"}:
            return
        if action.action == "type_text" and action.sensitive:
            return
        if action.action == "key_press":
            keys = "+".join(action.keys)
            if self._dangerous_keys.search(keys):
                return
        if action.action in {
            "click",
            "double_click",
            "right_click",
            "drag",
            "type_text",
            "key_press",
        }:
            return
        raise DesktopPolicyError(f"desktop action {action.action!r} is not permitted")

    @staticmethod
    def validate_window_target(action: DesktopAction, target: WindowInfo) -> None:
        if action.action == "focus_window" and action.window_id not in {
            None,
            target.window_id,
        }:
            raise DesktopPolicyError("focus_window cannot escape the selected target window")

    def requires_approval(self, action: DesktopAction) -> bool:
        if action.sensitive:
            return True
        # System-level shortcuts can escape the selected application, expose
        # the Run dialog, close windows, or lock/change the desktop. Keep this
        # boundary even when routine desktop actions are set to auto-approve.
        if action.action == "key_press" and self._dangerous_keys.search(
            "+".join(action.keys)
        ):
            return True
        if not self.require_confirmation:
            return False
        if action.action in {"right_click", "drag"}:
            return True
        return False

    @staticmethod
    def audit_arguments(action: DesktopAction) -> Dict[str, Any]:
        values = action.model_dump(exclude_none=True)
        if action.action == "type_text" and "text" in values:
            text = str(values["text"])
            values["text"] = f"<redacted length={len(text)}>"
        return values

    @staticmethod
    def approval_summary(action: DesktopAction) -> Tuple[str, str]:
        if action.sensitive:
            return "输入敏感信息", "此动作可能向当前窗口传输密码、验证码或其他敏感数据。"
        if action.action in {"right_click", "drag"}:
            return f"执行桌面{action.action}操作", "该动作可能移动、删除或改变本地对象。"
        return (
            f"执行桌面快捷键：{'+'.join(action.keys)}",
            "该快捷键可能关闭窗口、删除内容或改变系统状态。",
        )
