import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Select, desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faraflow.domain.enums import ApprovalStatus, SessionState
from faraflow.domain.schemas import SkillManifest, TaskCreate

from .database import (
    ActionRecord,
    ApprovalRecord,
    EventRecord,
    SessionRecord,
    SkillRecord,
    TaskRecord,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_task(
        self, request: TaskCreate, plan: List[Dict[str, Any]]
    ) -> Tuple[TaskRecord, SessionRecord]:
        task_id = new_id("task")
        session_id = new_id("sess")
        async with self._session_factory() as db:
            task = TaskRecord(
                task_id=task_id,
                session_id=session_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                task_name=request.task_name,
                description=request.description,
                start_url=request.start_url,
                status=SessionState.PLANNED.value,
                risk_level=request.risk_level.value,
                allowed_domains=request.allowed_domains,
                approval_policy=request.approval_policy.model_dump(),
                runtime_policy=request.runtime_policy.model_dump(),
                plan=plan,
            )
            session = SessionRecord(
                session_id=session_id,
                task_id=task_id,
                tenant_id=request.tenant_id,
                thread_id=new_id("thread"),
                state=SessionState.PLANNED.value,
                current_step_id=plan[0]["step_id"] if plan else None,
                resume_token=secrets.token_urlsafe(32),
                allowed_domains=request.allowed_domains,
                runtime_state={
                    "action_count": 0,
                    "facts": [],
                    "last_observation": "",
                },
            )
            db.add_all([task, session])
            await db.commit()
            await db.refresh(task)
            await db.refresh(session)
            return task, session

    async def list_tasks(
        self, tenant_id: Optional[str] = None, limit: int = 100
    ) -> List[TaskRecord]:
        statement: Select[Tuple[TaskRecord]] = select(TaskRecord).order_by(
            desc(TaskRecord.created_at)
        )
        if tenant_id:
            statement = statement.where(TaskRecord.tenant_id == tenant_id)
        statement = statement.limit(limit)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())

    async def get_task(self, task_id: str) -> TaskRecord:
        async with self._session_factory() as db:
            task = await db.get(TaskRecord, task_id)
            if task is None:
                raise NotFoundError(f"task {task_id} not found")
            return task

    async def get_session(self, session_id: str) -> SessionRecord:
        async with self._session_factory() as db:
            session = await db.get(SessionRecord, session_id)
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            return session

    async def get_session_for_task(self, task_id: str) -> SessionRecord:
        async with self._session_factory() as db:
            session = await db.scalar(select(SessionRecord).where(SessionRecord.task_id == task_id))
            if session is None:
                raise NotFoundError(f"session for task {task_id} not found")
            return session

    async def update_state(
        self,
        task_id: str,
        session_id: str,
        state: SessionState,
        *,
        runtime_state: Optional[Dict[str, Any]] = None,
        last_screenshot_ref: Optional[str] = None,
        final_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._session_factory() as db:
            task = await db.get(TaskRecord, task_id)
            session = await db.get(SessionRecord, session_id)
            if task is None or session is None:
                raise NotFoundError("task or session not found")
            task.status = state.value
            session.state = state.value
            session.updated_at = now_utc()
            if state == SessionState.RUNNING and task.started_at is None:
                task.started_at = now_utc()
            if state in {
                SessionState.COMPLETED,
                SessionState.FAILED,
                SessionState.TERMINATED,
                SessionState.EXPIRED,
            }:
                task.finished_at = now_utc()
            if runtime_state is not None:
                session.runtime_state = runtime_state
            if last_screenshot_ref is not None:
                session.last_screenshot_ref = last_screenshot_ref
            if final_result is not None:
                task.final_result = final_result
            await db.commit()

    async def rotate_resume_token(self, session_id: str) -> str:
        async with self._session_factory() as db:
            session = await db.get(SessionRecord, session_id)
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            session.resume_token = secrets.token_urlsafe(32)
            session.updated_at = now_utc()
            await db.commit()
            return session.resume_token

    async def update_runtime_state(self, session_id: str, runtime_state: Dict[str, Any]) -> None:
        async with self._session_factory() as db:
            session = await db.get(SessionRecord, session_id)
            if session is None:
                raise NotFoundError(f"session {session_id} not found")
            session.runtime_state = runtime_state
            session.updated_at = now_utc()
            await db.commit()

    async def append_action(
        self,
        *,
        task_id: str,
        session_id: str,
        step_no: int,
        action_type: str,
        parameters: Dict[str, Any],
        page_url: Optional[str],
        screenshot_before: Optional[str],
        screenshot_after: Optional[str],
        result: Dict[str, Any],
        verification: Optional[Dict[str, Any]] = None,
    ) -> ActionRecord:
        async with self._session_factory() as db:
            action = ActionRecord(
                action_id=new_id("act"),
                task_id=task_id,
                session_id=session_id,
                step_no=step_no,
                action_type=action_type,
                action_parameters=parameters,
                page_url=page_url,
                screenshot_before=screenshot_before,
                screenshot_after=screenshot_after,
                execution_result=result,
                verification_result=verification or {},
            )
            db.add(action)
            await db.commit()
            await db.refresh(action)
            return action

    async def list_actions(self, session_id: str, limit: int = 500) -> List[ActionRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(ActionRecord)
                .where(ActionRecord.session_id == session_id)
                .order_by(ActionRecord.step_no)
                .limit(limit)
            )
            return list(rows.all())

    async def create_approval(
        self,
        *,
        task_id: str,
        session_id: str,
        action_summary: str,
        risk_description: str,
        pending_payload: Dict[str, Any],
    ) -> ApprovalRecord:
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.session_id == session_id,
                    ApprovalRecord.status == ApprovalStatus.PENDING.value,
                )
            )
            if existing is not None:
                return existing
            approval = ApprovalRecord(
                approval_id=new_id("apr"),
                task_id=task_id,
                session_id=session_id,
                action_summary=action_summary,
                risk_description=risk_description,
                status=ApprovalStatus.PENDING.value,
                pending_payload=pending_payload,
            )
            db.add(approval)
            await db.commit()
            await db.refresh(approval)
            return approval

    async def get_approval(self, approval_id: str) -> ApprovalRecord:
        async with self._session_factory() as db:
            approval = await db.get(ApprovalRecord, approval_id)
            if approval is None:
                raise NotFoundError(f"approval {approval_id} not found")
            return approval

    async def list_approvals(self, session_id: str, limit: int = 100) -> List[ApprovalRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(ApprovalRecord)
                .where(ApprovalRecord.session_id == session_id)
                .order_by(desc(ApprovalRecord.requested_at))
                .limit(limit)
            )
            return list(rows.all())

    async def decide_approval(
        self, approval_id: str, decision: ApprovalStatus, comment: Optional[str]
    ) -> ApprovalRecord:
        if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("decision must be approved or rejected")
        async with self._session_factory() as db:
            approval = await db.get(ApprovalRecord, approval_id)
            if approval is None:
                raise NotFoundError(f"approval {approval_id} not found")
            if approval.status != ApprovalStatus.PENDING.value:
                raise ConflictError("approval has already been decided")
            approval.status = decision.value
            approval.comment = comment
            approval.decided_at = now_utc()
            await db.commit()
            await db.refresh(approval)
            return approval

    async def append_event(
        self,
        session_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> EventRecord:
        async with self._session_factory() as db:
            event = EventRecord(
                event_id=new_id("evt"),
                session_id=session_id,
                event_type=event_type,
                message=message,
                payload=payload or {},
            )
            db.add(event)
            await db.commit()
            await db.refresh(event)
            return event

    async def list_events(self, session_id: str, limit: int = 500) -> List[EventRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(EventRecord)
                .where(EventRecord.session_id == session_id)
                .order_by(EventRecord.created_at)
                .limit(limit)
            )
            return list(rows.all())

    async def register_skill(self, manifest: SkillManifest) -> SkillRecord:
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(SkillRecord).where(
                    SkillRecord.skill_name == manifest.skill_name,
                    SkillRecord.version == manifest.version,
                )
            )
            if existing:
                raise ConflictError("skill version already exists")
            record = SkillRecord(
                skill_name=manifest.skill_name,
                version=manifest.version,
                category=manifest.category,
                manifest=manifest.model_dump(),
                enabled=manifest.enabled,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def list_skills(
        self, category: Optional[str] = None, enabled: Optional[bool] = True
    ) -> List[SkillRecord]:
        statement = select(SkillRecord).order_by(SkillRecord.skill_name, SkillRecord.version)
        if category:
            statement = statement.where(SkillRecord.category == category)
        if enabled is not None:
            statement = statement.where(SkillRecord.enabled == enabled)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())
