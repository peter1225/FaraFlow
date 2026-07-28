from typing import Any, Dict, List, Optional

from faraflow.domain.enums import ApprovalStatus, RiskLevel, SessionState
from faraflow.domain.schemas import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalView,
    PlanStep,
    RuntimePolicy,
    SessionEvent,
    SessionSummary,
    SkillManifest,
    TaskCreate,
    TaskView,
    UserResponse,
)
from faraflow.infra.repository import ConflictError, Repository, new_id

from .agent_runtime import AgentRuntime


class TaskService:
    def __init__(self, repository: Repository, runtime: AgentRuntime) -> None:
        self.repository = repository
        self.runtime = runtime

    @staticmethod
    def _plan() -> List[Dict[str, Any]]:
        return [
            PlanStep(
                step_id=new_id("step"),
                order=1,
                name="隔离浏览器执行",
                step_type="browser_use",
                executor="fara",
            ).model_dump(),
            PlanStep(
                step_id=new_id("step"),
                order=2,
                name="结果验证与关键节点审批",
                step_type="verify",
                executor="coordinator",
            ).model_dump(),
            PlanStep(
                step_id=new_id("step"),
                order=3,
                name="归档轨迹并生成结果",
                step_type="archive",
                executor="coordinator",
            ).model_dump(),
        ]

    async def create(self, request: TaskCreate) -> TaskView:
        task, _ = await self.repository.create_task(request, self._plan())
        await self.runtime.emit(
            task.session_id,
            "session.planned",
            "任务已创建并生成执行计划",
            {"plan": task.plan},
        )
        return await self.get(task.task_id)

    async def get(self, task_id: str) -> TaskView:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        return TaskView(
            task_id=task.task_id,
            session_id=task.session_id,
            tenant_id=task.tenant_id,
            user_id=task.user_id,
            task_name=task.task_name,
            description=task.description,
            start_url=task.start_url,
            status=SessionState(task.status),
            risk_level=RiskLevel(task.risk_level),
            allowed_domains=list(task.allowed_domains),
            approval_policy=ApprovalPolicy.model_validate(task.approval_policy),
            runtime_policy=RuntimePolicy.model_validate(task.runtime_policy),
            plan=[PlanStep.model_validate(item) for item in task.plan],
            final_result=task.final_result,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            session=SessionSummary(
                session_id=session.session_id,
                state=SessionState(session.state),
                resume_token=session.resume_token,
                current_step_id=session.current_step_id,
                last_screenshot_ref=session.last_screenshot_ref,
                runtime_state=session.runtime_state,
                created_at=session.created_at,
                updated_at=session.updated_at,
            ),
        )

    async def list(self, tenant_id: Optional[str] = None) -> List[TaskView]:
        records = await self.repository.list_tasks(tenant_id=tenant_id)
        return [await self.get(record.task_id) for record in records]

    async def start(self, task_id: str) -> TaskView:
        await self.runtime.start(task_id)
        return await self.get(task_id)

    async def pause(self, task_id: str) -> TaskView:
        await self.runtime.pause(task_id)
        return await self.get(task_id)

    async def terminate(self, task_id: str) -> TaskView:
        await self.runtime.terminate(task_id)
        return await self.get(task_id)

    async def respond(self, task_id: str, response: UserResponse) -> TaskView:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        if SessionState(session.state) != SessionState.WAITING_USER_INPUT:
            raise ConflictError("task is not waiting for user input")
        if not secrets_equal(session.resume_token, response.resume_token):
            raise ConflictError("invalid or stale resume token")
        runtime_state = dict(session.runtime_state)
        runtime_state["user_response"] = response.response
        runtime_state.pop("pending_question", None)
        await self.repository.update_runtime_state(session.session_id, runtime_state)
        await self.repository.rotate_resume_token(session.session_id)
        await self.runtime.start(task_id)
        return await self.get(task_id)

    async def decide_approval(
        self,
        task_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalView:
        task = await self.repository.get_task(task_id)
        session = await self.repository.get_session(task.session_id)
        approval = await self.repository.get_approval(approval_id)
        if approval.task_id != task_id:
            raise ConflictError("approval does not belong to this task")
        if not secrets_equal(session.resume_token, decision.resume_token):
            raise ConflictError("invalid or stale resume token")
        status = (
            ApprovalStatus.APPROVED if decision.decision == "approve" else ApprovalStatus.REJECTED
        )
        approval = await self.repository.decide_approval(approval_id, status, decision.comment)
        await self.repository.rotate_resume_token(session.session_id)
        if status == ApprovalStatus.APPROVED:
            await self.runtime.emit(
                session.session_id,
                "approval.approved",
                "审批已通过，任务即将恢复",
                {"approval_id": approval_id},
            )
            await self.runtime.start(task_id)
        else:
            await self.repository.update_state(
                task_id,
                session.session_id,
                SessionState.TERMINATED,
                final_result={"reason": "approval_rejected", "approval_id": approval_id},
            )
            await self.runtime.emit(
                session.session_id,
                "approval.rejected",
                "审批已拒绝，任务终止",
                {"approval_id": approval_id},
            )
        return self._approval_view(approval)

    async def approvals(self, task_id: str) -> List[ApprovalView]:
        task = await self.repository.get_task(task_id)
        records = await self.repository.list_approvals(task.session_id)
        return [self._approval_view(record) for record in records]

    async def events(self, task_id: str) -> List[SessionEvent]:
        task = await self.repository.get_task(task_id)
        records = await self.repository.list_events(task.session_id)
        return [
            SessionEvent(
                event_id=item.event_id,
                session_id=item.session_id,
                event_type=item.event_type,
                message=item.message,
                payload=item.payload,
                created_at=item.created_at,
            )
            for item in records
        ]

    @staticmethod
    def _approval_view(record: Any) -> ApprovalView:
        return ApprovalView(
            approval_id=record.approval_id,
            task_id=record.task_id,
            session_id=record.session_id,
            action_summary=record.action_summary,
            risk_description=record.risk_description,
            status=ApprovalStatus(record.status),
            pending_payload=record.pending_payload,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            comment=record.comment,
        )

    async def register_skill(self, manifest: SkillManifest) -> SkillManifest:
        record = await self.repository.register_skill(manifest)
        return SkillManifest.model_validate(record.manifest)

    async def skills(
        self, category: Optional[str] = None, enabled: Optional[bool] = True
    ) -> List[SkillManifest]:
        rows = await self.repository.list_skills(category, enabled)
        return [SkillManifest.model_validate(row.manifest) for row in rows]

    async def seed_skills(self) -> None:
        defaults = [
            SkillManifest(
                skill_name="browser.computer_use",
                version="1.0.0",
                category="browser",
                description="Fara1.5 screenshot-driven browser action tool",
                input_schema={"$ref": "fara://computer_use/v1"},
                output_schema={"type": "object"},
                side_effect_level="high",
                idempotency="non_idempotent",
                approval_policy="platform-critical-point-policy",
                sandbox="isolated_browser_context",
                permissions=["browser:read", "browser:interact"],
                audit_tags=["fara", "browser", "user-visible"],
            ),
            SkillManifest(
                skill_name="control.request_approval",
                version="1.0.0",
                category="control",
                description="Pause a session and request one-time human approval",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect_level="none",
                idempotency="idempotent",
                approval_policy="coordinator-only",
                sandbox="control-plane",
                permissions=["session:interrupt"],
                audit_tags=["approval", "control"],
            ),
        ]
        for manifest in defaults:
            try:
                await self.repository.register_skill(manifest)
            except ConflictError:
                pass


def secrets_equal(left: str, right: str) -> bool:
    import secrets

    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
