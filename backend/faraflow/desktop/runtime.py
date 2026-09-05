import asyncio
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from faraflow.domain.enums import DesktopRunStatus
from faraflow.domain.schemas import SessionEvent
from faraflow.infra.artifacts import ArtifactStore
from faraflow.infra.events import EventBus
from faraflow.infra.repository import ConflictError, Repository
from faraflow.model.fara_adapter import ModelEndpointError

from .adapter import DesktopAdapter
from .bridge import DesktopBridge, DesktopBridgeError, WindowInfo
from .policy import DesktopPolicy, DesktopPolicyError
from .protocol import DesktopAction, DesktopProtocolError


class DesktopRuntime:
    def __init__(
        self,
        settings: Any,
        repository: Repository,
        artifacts: ArtifactStore,
        adapter: DesktopAdapter,
        bridge: DesktopBridge,
        policy: DesktopPolicy,
        event_bus: EventBus,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.artifacts = artifacts
        self.adapter = adapter
        self.bridge = bridge
        self.policy = policy
        self.event_bus = event_bus
        self._jobs: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(settings.desktop_max_concurrent_runs)

    async def emit(
        self,
        session_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SessionEvent:
        record = await self.repository.append_event(session_id, event_type, message, payload)
        event = SessionEvent(
            event_id=record.event_id,
            session_id=record.session_id,
            event_type=record.event_type,
            message=record.message,
            payload=record.payload,
            created_at=record.created_at,
        )
        await self.event_bus.publish(event)
        return event

    async def list_windows(self) -> List[WindowInfo]:
        return await asyncio.to_thread(self.bridge.list_windows)

    async def select_window(self, desktop_run_id: str, window_id: int) -> WindowInfo:
        record = await self.repository.get_desktop_run(desktop_run_id)
        target = await asyncio.to_thread(self.bridge.get_window, window_id)
        self.policy.validate_target(target)
        await self.repository.update_desktop_run(
            desktop_run_id,
            target_window_id=target.window_id,
            target_title=target.title,
            target_process=target.process_name,
            target_rect={
                "x": target.x,
                "y": target.y,
                "width": target.width,
                "height": target.height,
            },
            status=(
                DesktopRunStatus.CREATED
                if record.status == DesktopRunStatus.WAITING_CAPTURE_CONSENT.value
                else None
            ),
        )
        await self.emit(
            record.session_id,
            "desktop.target.selected",
            "已选择桌面目标窗口",
            target.as_dict(),
        )
        return target

    async def start(
        self,
        desktop_run_id: str,
        approved_action: Optional[DesktopAction] = None,
        approval_action_id: Optional[str] = None,
    ) -> None:
        record = await self.repository.get_desktop_run(desktop_run_id)
        if record.target_window_id is None:
            await self.repository.update_desktop_run(
                desktop_run_id, status=DesktopRunStatus.WAITING_CAPTURE_CONSENT
            )
            await self.emit(
                record.session_id,
                "desktop.capture.consent_required",
                "请选择一个可控的桌面窗口后再开始任务",
                {"desktop_run_id": desktop_run_id},
            )
            return
        async with self._lock:
            existing = self._jobs.get(desktop_run_id)
            if existing and not existing.done():
                raise ConflictError("desktop run is already running")
            job = asyncio.create_task(
                self._run(desktop_run_id, approved_action, approval_action_id),
                name=f"faraflow-desktop:{desktop_run_id}",
            )
            self._jobs[desktop_run_id] = job
            job.add_done_callback(lambda finished: self._forget_job(desktop_run_id, finished))

    def _forget_job(self, desktop_run_id: str, finished: asyncio.Task) -> None:
        if self._jobs.get(desktop_run_id) is finished:
            self._jobs.pop(desktop_run_id, None)

    async def pause(self, desktop_run_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(desktop_run_id)
            if job and not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        record = await self.repository.get_desktop_run(desktop_run_id)
        await self.repository.update_desktop_run(desktop_run_id, status=DesktopRunStatus.PAUSED)
        await self.emit(record.session_id, "desktop.run.paused", "桌面任务已暂停")

    async def terminate(self, desktop_run_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(desktop_run_id)
            if job and not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        record = await self.repository.get_desktop_run(desktop_run_id)
        await self.repository.update_desktop_run(desktop_run_id, status=DesktopRunStatus.TERMINATED)
        await self.emit(record.session_id, "desktop.run.terminated", "桌面任务已终止")

    async def close(self) -> None:
        jobs = list(self._jobs.values())
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _run(
        self,
        desktop_run_id: str,
        approved_action: Optional[DesktopAction] = None,
        approval_action_id: Optional[str] = None,
    ) -> None:
        async with self._capacity:
            record = await self.repository.get_desktop_run(desktop_run_id)
            deadline = time.monotonic() + self.settings.desktop_max_runtime_minutes * 60
            try:
                target = await asyncio.to_thread(self.bridge.get_window, record.target_window_id)
                self.policy.validate_target(target)
                await self.repository.update_desktop_run(
                    desktop_run_id,
                    status=DesktopRunStatus.RUNNING,
                    target_title=target.title,
                    target_process=target.process_name,
                    target_rect={
                        "x": target.x,
                        "y": target.y,
                        "width": target.width,
                        "height": target.height,
                    },
                )
                await self.emit(
                    record.session_id,
                    "desktop.run.started",
                    "桌面任务开始执行",
                    {"desktop_run_id": desktop_run_id, "target": target.as_dict()},
                )
                visible_windows = await asyncio.to_thread(self.bridge.list_windows)
                known_window_ids = {window.window_id for window in visible_windows}
                if approved_action is not None:
                    # Resume on the current desktop after approval. Re-running
                    # prepare_target and the initial screenshot would make the
                    # model rediscover the old state and request approval again.
                    current = await asyncio.to_thread(self.bridge.capture, target)
                    action_rows = await self.repository.list_desktop_actions(desktop_run_id)
                    pending_record = next(
                        (
                            item
                            for item in reversed(action_rows)
                            if item.action_id == approval_action_id
                        ),
                        None,
                    )
                    resume_step = pending_record.step_no if pending_record else max(
                        (item.step_no for item in action_rows), default=0
                    )
                    before_ref = self.artifacts.save_bytes(
                        record.session_id,
                        f"step_{resume_step:03d}_resume_before.png",
                        current,
                        subdirectory="desktop",
                    )
                    try:
                        foreground_before = await asyncio.to_thread(
                            self.bridge.foreground_window
                        )
                        result = await asyncio.to_thread(
                            self.bridge.execute, approved_action, target
                        )
                        if approved_action.action not in {
                            "screenshot",
                            "list_windows",
                            "wait",
                        }:
                            await asyncio.sleep(0.75)
                        target, switched = await self._follow_target_after_action(
                            desktop_run_id,
                            record.session_id,
                            target,
                            foreground_before,
                            known_window_ids,
                        )
                        if switched:
                            result["target_switched"] = target.as_dict()
                        visible_windows = await asyncio.to_thread(self.bridge.list_windows)
                        known_window_ids.update(
                            window.window_id for window in visible_windows
                        )
                        after = await asyncio.to_thread(self.bridge.capture, target)
                    except (DesktopBridgeError, DesktopPolicyError) as exc:
                        if approval_action_id:
                            await self.repository.update_desktop_action(
                                approval_action_id,
                                status="FAILED",
                                screenshot_before=before_ref,
                                execution_result={"error": str(exc)},
                            )
                        raise
                    after_ref = self.artifacts.save_bytes(
                        record.session_id,
                        f"step_{resume_step:03d}_resume_after.png",
                        after,
                        subdirectory="desktop",
                    )
                    if approval_action_id:
                        await self.repository.update_desktop_action(
                            approval_action_id,
                            status="COMPLETED",
                            screenshot_before=before_ref,
                            screenshot_after=after_ref,
                            execution_result=result,
                        )
                    await self.repository.update_desktop_run(
                        desktop_run_id, last_screenshot_ref=after_ref
                    )
                    await self.emit(
                        record.session_id,
                        "desktop.action.completed",
                        f"Approved desktop action executed: {approved_action.action}",
                        {"step_no": resume_step, "action": approved_action.action},
                    )
                    conversation = [
                        self.adapter.initial_message(record.instruction, after),
                        {
                            "role": "assistant",
                            "content": approved_action.model_dump_json(),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"The approved {approved_action.action} action was executed. "
                                "Continue from the current screenshot and do not repeat it."
                            ),
                        },
                    ]
                    pending = None
                    first_step = resume_step + 1
                else:
                    await asyncio.to_thread(self.bridge.prepare_target, target)
                    initial = await asyncio.to_thread(self.bridge.capture, target)
                    initial_ref = self.artifacts.save_bytes(
                        record.session_id, "step_000_initial.png", initial, subdirectory="desktop"
                    )
                    await self.repository.update_desktop_run(
                        desktop_run_id, last_screenshot_ref=initial_ref
                    )
                    conversation = [self.adapter.initial_message(record.instruction, initial)]
                    pending = None
                    first_step = 1
                last_action_signature: Optional[str] = None
                repeated_action_count = 0
                rejected_blocker_finals = 0
                rejected_handoffs = 0
                force_action_next = False
                excluded_action_next: Optional[str] = None
                for step_no in range(first_step, self.settings.desktop_max_steps + 1):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("desktop run exceeded maximum runtime")
                    if pending is not None:
                        decision = type(
                            "ApprovedDecision",
                            (),
                            {
                                "kind": "action",
                                "action": pending,
                                "raw_response": "approved",
                            },
                        )()
                    else:
                        excluded_for_decision: List[str] = []
                        if excluded_action_next:
                            excluded_for_decision.append(excluded_action_next)
                        if not self._allows_double_click(target):
                            excluded_for_decision.append("double_click")
                        decision = await self.adapter.next_decision(
                            conversation,
                            allow_terminal=not force_action_next,
                            excluded_actions=excluded_for_decision or None,
                        )
                        force_action_next = False
                        excluded_action_next = None
                    pending = None
                    if decision.kind == "handoff":
                        summary = decision.answer or "Desktop task requires user assistance."
                        if rejected_handoffs == 0:
                            rejected_handoffs = 1
                            force_action_next = True
                            corrective_screenshot = await asyncio.to_thread(
                                self.bridge.capture, target
                            )
                            conversation.append(
                                {"role": "assistant", "content": decision.raw_response}
                            )
                            conversation.append(
                                self.adapter.observation_message(
                                    (
                                        "Re-check the latest screenshot before requesting user "
                                        "assistance. A saved account/avatar with a visible Login "
                                        "or Sign in button does not require entering a secret: "
                                        "click that button now and inspect the next screenshot. "
                                        "Only return handoff if the updated screen visibly asks "
                                        "for a password, verification code, QR confirmation, UAC, "
                                        "or another user-only interaction."
                                    ),
                                    corrective_screenshot,
                                )
                            )
                            await self.emit(
                                record.session_id,
                                "desktop.handoff.rejected",
                                (
                                    "The first handoff request was rejected so the model can "
                                    "try the visible login action."
                                ),
                                {"desktop_run_id": desktop_run_id},
                            )
                            continue
                        await self.repository.update_desktop_run(
                            desktop_run_id,
                            status=DesktopRunStatus.HANDOFF,
                            final_summary=summary,
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.run.handoff",
                            summary,
                            {"desktop_run_id": desktop_run_id},
                        )
                        return
                    if decision.kind == "final" and self._looks_like_blocker(
                        decision.answer
                    ):
                        summary = decision.answer or "Desktop task requires user assistance."
                        if rejected_blocker_finals == 0:
                            rejected_blocker_finals = 1
                            corrective_screenshot = await asyncio.to_thread(
                                self.bridge.capture, target
                            )
                            conversation.append(
                                {"role": "assistant", "content": decision.raw_response}
                            )
                            conversation.append(
                                self.adapter.observation_message(
                                    (
                                        "This is a blocker, not successful completion. Do not "
                                        "return final. If the screenshot shows a saved account "
                                        "and a visible Login/Sign in button, click it before "
                                        "assuming a password or verification is required. Only "
                                        "return handoff when the latest screenshot actually "
                                        "shows a user-only credential or confirmation prompt."
                                    ),
                                    corrective_screenshot,
                                )
                            )
                            await self.emit(
                                record.session_id,
                                "desktop.final.rejected",
                                (
                                    "The model reported a blocker as completion and was "
                                    "asked to continue."
                                ),
                                {"desktop_run_id": desktop_run_id},
                            )
                            continue
                        await self.repository.update_desktop_run(
                            desktop_run_id,
                            status=DesktopRunStatus.HANDOFF,
                            final_summary=summary,
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.run.handoff",
                            summary,
                            {"desktop_run_id": desktop_run_id},
                        )
                        return
                    if decision.kind == "final":
                        summary = decision.answer or "桌面任务已完成"
                        await self.repository.update_desktop_run(
                            desktop_run_id,
                            status=DesktopRunStatus.COMPLETED,
                            final_summary=summary,
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.run.completed",
                            summary,
                            {"desktop_run_id": desktop_run_id},
                        )
                        return
                    if decision.action is None:
                        raise DesktopProtocolError("desktop model returned no action")
                    action = decision.action
                    effective_raw_response = decision.raw_response
                    if action.action == "double_click" and not self._allows_double_click(target):
                        action = action.model_copy(update={"action": "click"})
                        effective_raw_response = action.model_dump_json(exclude_none=True)
                        await self.emit(
                            record.session_id,
                            "desktop.action.normalized",
                            "已将应用内双击降级为单击，避免重复切换控件状态",
                            {
                                "step_no": step_no,
                                "from": "double_click",
                                "to": "click",
                                "target_process": target.process_name,
                            },
                        )
                    rejected_blocker_finals = 0
                    rejected_handoffs = 0
                    signature = action.model_dump_json(exclude_none=True)
                    if signature == last_action_signature:
                        repeated_action_count += 1
                    else:
                        last_action_signature = signature
                        repeated_action_count = 1
                    if repeated_action_count == 2:
                        corrective_screenshot = await asyncio.to_thread(
                            self.bridge.capture, target
                        )
                        blocked_ref = self.artifacts.save_bytes(
                            record.session_id,
                            f"step_{step_no:03d}_blocked.png",
                            corrective_screenshot,
                            subdirectory="desktop",
                        )
                        excluded_action_next = action.action
                        await self.repository.append_desktop_action(
                            desktop_run_id=desktop_run_id,
                            session_id=record.session_id,
                            step_no=step_no,
                            action_name=action.action,
                            arguments=self.policy.audit_arguments(action),
                            status="BLOCKED",
                            screenshot_before=blocked_ref,
                            screenshot_after=blocked_ref,
                            execution_result={
                                "reason": "duplicate_action",
                                "repeat_count": repeated_action_count,
                                "excluded_next": action.action,
                            },
                        )
                        await self.repository.update_desktop_run(
                            desktop_run_id, last_screenshot_ref=blocked_ref
                        )
                        conversation.append(
                            {"role": "assistant", "content": effective_raw_response}
                        )
                        conversation.append(
                            self.adapter.observation_message(
                                (
                                    "The exact same action was already executed and did not "
                                    "advance the task. That action type is unavailable for the "
                                    "next decision. Inspect the current screenshot and choose a "
                                    "different visible control or another safe action type. For "
                                    "long keyboard navigation, prefer clicking the visible target."
                                ),
                                corrective_screenshot,
                            )
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.action.duplicate_blocked",
                            "已阻止重复桌面动作，并要求模型改用其他操作",
                            {
                                "step_no": step_no,
                                "action": action.action,
                                "excluded_next": action.action,
                            },
                        )
                        continue
                    if repeated_action_count >= 3:
                        raise DesktopProtocolError(
                            f"desktop model repeated the same {action.action} action 3 times"
                        )
                    self.policy.check_action(action, step_no)
                    if (
                        action.action == "focus_window"
                        and action.window_id is not None
                        and action.window_id != target.window_id
                    ):
                        candidate = await asyncio.to_thread(
                            self.bridge.get_window, action.window_id
                        )
                        self.policy.validate_focus_transition(target, candidate)
                        target = candidate
                        await self.repository.update_desktop_run(
                            desktop_run_id,
                            target_window_id=target.window_id,
                            target_title=target.title,
                            target_process=target.process_name,
                            target_rect={
                                "x": target.x,
                                "y": target.y,
                                "width": target.width,
                                "height": target.height,
                            },
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.target.focused",
                            f"已切换到同一应用窗口：{target.title}",
                            target.as_dict(),
                        )
                    self.policy.validate_window_target(action, target)
                    if self.policy.requires_approval(action):
                        summary, risk = self.policy.approval_summary(action)
                        approval = await self.repository.create_desktop_approval(
                            desktop_run_id=desktop_run_id,
                            session_id=record.session_id,
                            action_summary=summary,
                            risk_description=risk,
                            pending_payload=action.model_dump(),
                        )
                        action_record = await self.repository.append_desktop_action(
                            desktop_run_id=desktop_run_id,
                            session_id=record.session_id,
                            step_no=step_no,
                            action_name=action.action,
                            arguments=self.policy.audit_arguments(action),
                            status="WAITING_APPROVAL",
                            screenshot_before=record.last_screenshot_ref,
                            screenshot_after=None,
                            execution_result={"approval_id": approval.approval_id},
                            approval_required=True,
                        )
                        await self.repository.update_desktop_run(
                            desktop_run_id,
                            status=DesktopRunStatus.WAITING_APPROVAL,
                            pending_action={
                                "approval_id": approval.approval_id,
                                "action_id": action_record.action_id,
                                **action.model_dump(),
                            },
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.approval.requested",
                            "桌面任务等待人工审批",
                            {"approval_id": approval.approval_id, "action": summary},
                        )
                        return
                    before = await asyncio.to_thread(self.bridge.capture, target)
                    before_ref = self.artifacts.save_bytes(
                        record.session_id,
                        f"step_{step_no:03d}_before.png",
                        before,
                        subdirectory="desktop",
                    )
                    try:
                        foreground_before = await asyncio.to_thread(
                            self.bridge.foreground_window
                        )
                        result = await asyncio.to_thread(self.bridge.execute, action, target)
                        # Native desktop applications often update asynchronously
                        # after input. Capturing immediately feeds the model the
                        # pre-action frame and makes it repeat the same click.
                        if action.action not in {"screenshot", "list_windows", "wait"}:
                            await asyncio.sleep(0.75)
                        target, switched = await self._follow_target_after_action(
                            desktop_run_id,
                            record.session_id,
                            target,
                            foreground_before,
                            known_window_ids,
                        )
                        if switched:
                            result["target_switched"] = target.as_dict()
                            last_action_signature = None
                            repeated_action_count = 0
                        visible_windows = await asyncio.to_thread(self.bridge.list_windows)
                        known_window_ids.update(
                            window.window_id for window in visible_windows
                        )
                        after = await asyncio.to_thread(self.bridge.capture, target)
                    except (DesktopBridgeError, DesktopPolicyError) as exc:
                        await self.repository.append_desktop_action(
                            desktop_run_id=desktop_run_id,
                            session_id=record.session_id,
                            step_no=step_no,
                            action_name=action.action,
                            arguments=self.policy.audit_arguments(action),
                            status="FAILED",
                            screenshot_before=before_ref,
                            screenshot_after=None,
                            execution_result={"error": str(exc)},
                        )
                        raise
                    after_ref = self.artifacts.save_bytes(
                        record.session_id,
                        f"step_{step_no:03d}_after.png",
                        after,
                        subdirectory="desktop",
                    )
                    await self.repository.append_desktop_action(
                        desktop_run_id=desktop_run_id,
                        session_id=record.session_id,
                        step_no=step_no,
                        action_name=action.action,
                        arguments=self.policy.audit_arguments(action),
                        status="COMPLETED",
                        screenshot_before=before_ref,
                        screenshot_after=after_ref,
                        execution_result=result,
                    )
                    await self.repository.update_desktop_run(
                        desktop_run_id, last_screenshot_ref=after_ref
                    )
                    await self.emit(
                        record.session_id,
                        "desktop.action.completed",
                        f"已执行桌面动作：{action.action}",
                        {"step_no": step_no, "action": action.action},
                    )
                    conversation.append(
                        {"role": "assistant", "content": effective_raw_response}
                    )
                    conversation.append(
                        self.adapter.observation_message(str(result), after)
                    )
                raise RuntimeError("desktop run exceeded maximum model steps")
            except asyncio.CancelledError:
                return
            except (
                ModelEndpointError,
                DesktopBridgeError,
                DesktopPolicyError,
                DesktopProtocolError,
                TimeoutError,
                RuntimeError,
            ) as exc:
                await self.repository.update_desktop_run(
                    desktop_run_id,
                    status=DesktopRunStatus.FAILED,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
                await self.emit(
                    record.session_id,
                    "desktop.run.failed",
                    "桌面任务执行失败",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )

    @staticmethod
    def _allows_double_click(target: WindowInfo) -> bool:
        process_name = target.process_name.replace("/", "\\").rsplit("\\", 1)[-1]
        return process_name.casefold() == "explorer.exe"

    @staticmethod
    def _looks_like_blocker(summary: str) -> bool:
        normalized = " ".join(summary.casefold().split())
        blocker_markers = (
            "cannot",
            "can't",
            "unable",
            "need you",
            "requires your",
            "manual login",
            "manually log",
            "password",
            "verification code",
            "qr code",
            "无法",
            "不能",
            "需要您",
            "请您",
            "手动登录",
            "密码",
            "验证码",
            "二维码",
            "敏感信息",
        )
        return any(marker in normalized for marker in blocker_markers)

    async def _follow_target_after_action(
        self,
        desktop_run_id: str,
        session_id: str,
        current: WindowInfo,
        previous_foreground: Optional[WindowInfo],
        known_window_ids: Set[int],
    ) -> Tuple[WindowInfo, bool]:
        if not self.settings.desktop_unattended_mode:
            return current, False
        candidate = await asyncio.to_thread(
            self.bridge.followup_window,
            current,
            previous_foreground_id=(
                previous_foreground.window_id if previous_foreground else None
            ),
            known_window_ids=known_window_ids,
        )
        if candidate is None:
            return current, False
        self.policy.validate_target(candidate)
        await self.repository.update_desktop_run(
            desktop_run_id,
            target_window_id=candidate.window_id,
            target_title=candidate.title,
            target_process=candidate.process_name,
            target_rect={
                "x": candidate.x,
                "y": candidate.y,
                "width": candidate.width,
                "height": candidate.height,
            },
        )
        await self.emit(
            session_id,
            "desktop.target.followed",
            f"已自动切换到新前台窗口：{candidate.title}",
            candidate.as_dict(),
        )
        return candidate, True
