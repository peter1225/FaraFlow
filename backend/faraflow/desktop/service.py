from typing import Any, List, Optional

from faraflow.domain.enums import ApprovalStatus, DesktopRunStatus
from faraflow.domain.schemas import (
    DesktopApprovalDecision,
    DesktopApprovalView,
    DesktopRunCreate,
    DesktopRunView,
    DesktopWindowView,
)
from faraflow.infra.repository import ConflictError, Repository

from .bridge import DesktopBridge
from .policy import DesktopPolicy
from .protocol import DesktopAction
from .runtime import DesktopRuntime


class DesktopRunService:
    def __init__(
        self,
        settings: Any,
        repository: Repository,
        runtime: DesktopRuntime,
        bridge: DesktopBridge,
        policy: DesktopPolicy,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.runtime = runtime
        self.bridge = bridge
        self.policy = policy

    def assert_enabled(self) -> None:
        if not self.settings.enable_desktop_control:
            raise ConflictError("desktop control is disabled")

    async def create(self, request: DesktopRunCreate) -> DesktopRunView:
        self.assert_enabled()
        record = await self.repository.create_desktop_run(
            instruction=request.instruction,
            chat_id=request.chat_id,
            target_window_id=request.target_window_id,
        )
        await self.runtime.emit(
            record.session_id,
            "desktop.run.created",
            "桌面任务已创建",
            {"desktop_run_id": record.desktop_run_id},
        )
        if request.auto_start:
            await self.runtime.start(record.desktop_run_id)
        return await self.get(record.desktop_run_id)

    async def get(self, desktop_run_id: str) -> DesktopRunView:
        record = await self.repository.get_desktop_run(desktop_run_id)
        return self._view(record, await self.repository.list_desktop_actions(desktop_run_id))

    async def list(self, chat_id: Optional[str] = None) -> List[DesktopRunView]:
        records = await self.repository.list_desktop_runs(chat_id)
        return [await self.get(record.desktop_run_id) for record in records]

    async def windows(self) -> List[DesktopWindowView]:
        self.assert_enabled()
        windows = await self.runtime.list_windows()
        return [DesktopWindowView(**window.as_dict()) for window in windows]

    async def select_window(self, desktop_run_id: str, window_id: int) -> DesktopRunView:
        self.assert_enabled()
        await self.runtime.select_window(desktop_run_id, window_id)
        return await self.get(desktop_run_id)

    async def start(self, desktop_run_id: str) -> DesktopRunView:
        self.assert_enabled()
        await self.runtime.start(desktop_run_id)
        return await self.get(desktop_run_id)

    async def pause(self, desktop_run_id: str) -> DesktopRunView:
        self.assert_enabled()
        await self.runtime.pause(desktop_run_id)
        return await self.get(desktop_run_id)

    async def terminate(self, desktop_run_id: str) -> DesktopRunView:
        self.assert_enabled()
        await self.runtime.terminate(desktop_run_id)
        return await self.get(desktop_run_id)

    async def approvals(self, desktop_run_id: str) -> List[DesktopApprovalView]:
        self.assert_enabled()
        await self.repository.get_desktop_run(desktop_run_id)
        records = await self.repository.list_desktop_approvals(desktop_run_id)
        return [self._approval_view(item) for item in records]

    async def decide_approval(
        self,
        desktop_run_id: str,
        approval_id: str,
        decision: DesktopApprovalDecision,
    ) -> DesktopApprovalView:
        self.assert_enabled()
        run = await self.repository.get_desktop_run(desktop_run_id)
        approval = await self.repository.get_desktop_approval(approval_id)
        if approval.desktop_run_id != desktop_run_id:
            raise ConflictError("desktop approval does not belong to this run")
        status = (
            ApprovalStatus.APPROVED
            if decision.decision == "approve"
            else ApprovalStatus.REJECTED
        )
        approval = await self.repository.decide_desktop_approval(
            approval_id, status, decision.comment
        )
        if status == ApprovalStatus.REJECTED:
            await self.repository.update_desktop_run(
                desktop_run_id,
                status=DesktopRunStatus.TERMINATED,
                error={"reason": "desktop_approval_rejected", "approval_id": approval_id},
            )
            await self.runtime.emit(
                run.session_id,
                "desktop.approval.rejected",
                "桌面审批已拒绝，任务终止",
                {"approval_id": approval_id},
            )
            return self._approval_view(approval)
        pending = dict(run.pending_action or {})
        pending.pop("approval_id", None)
        action_id = pending.pop("action_id", None)
        if not pending:
            raise ConflictError("desktop run has no pending action to approve")
        await self.repository.update_desktop_run(desktop_run_id, pending_action={})
        await self.runtime.emit(
            run.session_id,
            "desktop.approval.approved",
            "桌面审批已通过，任务恢复执行",
            {"approval_id": approval_id},
        )
        await self.runtime.start(
            desktop_run_id,
            DesktopAction.model_validate(pending),
            approval_action_id=action_id,
        )
        return self._approval_view(approval)

    @staticmethod
    def _view(record: Any, actions: List[Any]) -> DesktopRunView:
        from faraflow.domain.schemas import DesktopActionView

        return DesktopRunView(
            desktop_run_id=record.desktop_run_id,
            chat_id=record.chat_id,
            session_id=record.session_id,
            instruction=record.instruction,
            status=DesktopRunStatus(record.status),
            target_window_id=record.target_window_id,
            target_title=record.target_title,
            target_process=record.target_process,
            final_summary=record.final_summary,
            last_screenshot_ref=record.last_screenshot_ref,
            error=record.error,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            actions=[
                DesktopActionView(
                    action_id=item.action_id,
                    step_no=item.step_no,
                    action_name=item.action_name,
                    status=item.status,
                    arguments=item.arguments,
                    screenshot_before=item.screenshot_before,
                    screenshot_after=item.screenshot_after,
                    execution_result=item.execution_result,
                    approval_required=item.approval_required,
                    created_at=item.created_at,
                )
                for item in actions
            ],
        )

    @staticmethod
    def _approval_view(record: Any) -> DesktopApprovalView:
        return DesktopApprovalView(
            approval_id=record.approval_id,
            desktop_run_id=record.desktop_run_id,
            session_id=record.session_id,
            action_summary=record.action_summary,
            risk_description=record.risk_description,
            status=ApprovalStatus(record.status),
            pending_payload=record.pending_payload,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            comment=record.comment,
        )
