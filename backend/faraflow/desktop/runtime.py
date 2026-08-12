import asyncio
import time
from typing import Any, Dict, List, Optional

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
                        result = await asyncio.to_thread(
                            self.bridge.execute, approved_action, target
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
                        decision = await self.adapter.next_decision(conversation)
                    pending = None
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
                        conversation.append(
                            {"role": "assistant", "content": decision.raw_response}
                        )
                        conversation.append(
                            self.adapter.observation_message(
                                (
                                    "The exact same action was already executed and did not "
                                    "advance the task. Do not repeat it. If the intended desktop "
                                    "item is selected, use key_press with ENTER; otherwise choose "
                                    "a different safe action."
                                ),
                                corrective_screenshot,
                            )
                        )
                        await self.emit(
                            record.session_id,
                            "desktop.action.duplicate_blocked",
                            "已阻止重复桌面动作，并要求模型改用其他操作",
                            {"step_no": step_no, "action": action.action},
                        )
                        continue
                    if repeated_action_count >= 3:
                        raise DesktopProtocolError(
                            f"desktop model repeated the same {action.action} action 3 times"
                        )
                    self.policy.check_action(action, step_no)
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
                        result = await asyncio.to_thread(self.bridge.execute, action, target)
                        # Native desktop applications often update asynchronously
                        # after input. Capturing immediately feeds the model the
                        # pre-action frame and makes it repeat the same click.
                        if action.action not in {"screenshot", "list_windows", "wait"}:
                            await asyncio.sleep(0.75)
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
                    conversation.append({"role": "assistant", "content": decision.raw_response})
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
