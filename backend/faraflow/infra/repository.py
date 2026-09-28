import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy import Select, case, desc, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faraflow.domain.enums import (
    ApprovalStatus,
    CodeApplyStatus,
    CodeRunStatus,
    CodeToolPhase,
    CodeTurnStatus,
    DesktopRunStatus,
    SessionState,
)
from faraflow.domain.schemas import ChatCreate, SkillManifest, TaskCreate, WorkspaceCreate

from .database import (
    ActionRecord,
    ApprovalRecord,
    ChatMessageRecord,
    ChatThreadRecord,
    CodeApplyJournalRecord,
    CodeReviewRecord,
    CodeRunRecord,
    CodeTurnRecord,
    DesktopActionRecord,
    DesktopApprovalRecord,
    DesktopRunRecord,
    EventRecord,
    SessionRecord,
    SkillRecord,
    TaskRecord,
    ToolCallRecord,
    WorkspaceRecord,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


_UNSET = object()


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class Repository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_chat(self, request: ChatCreate) -> ChatThreadRecord:
        async with self._session_factory() as db:
            if request.workspace_id is not None:
                workspace = await db.get(WorkspaceRecord, request.workspace_id)
                if workspace is None or not workspace.active:
                    raise NotFoundError(f"workspace {request.workspace_id} not found")
            chat = ChatThreadRecord(
                chat_id=new_id("chat"),
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                title=request.title,
                workspace_id=request.workspace_id,
            )
            db.add(chat)
            await db.commit()
            await db.refresh(chat)
            return chat

    async def list_chats(
        self, tenant_id: Optional[str] = None, limit: int = 100
    ) -> List[ChatThreadRecord]:
        statement: Select[Tuple[ChatThreadRecord]] = select(ChatThreadRecord).order_by(
            desc(ChatThreadRecord.updated_at)
        )
        if tenant_id:
            statement = statement.where(ChatThreadRecord.tenant_id == tenant_id)
        statement = statement.limit(limit)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())

    async def get_chat(self, chat_id: str) -> ChatThreadRecord:
        async with self._session_factory() as db:
            chat = await db.get(ChatThreadRecord, chat_id)
            if chat is None:
                raise NotFoundError(f"chat {chat_id} not found")
            return chat

    async def update_chat_title(self, chat_id: str, title: str) -> None:
        async with self._session_factory() as db:
            chat = await db.get(ChatThreadRecord, chat_id)
            if chat is None:
                raise NotFoundError(f"chat {chat_id} not found")
            chat.title = title
            chat.updated_at = now_utc()
            await db.commit()

    async def append_chat_message(
        self,
        *,
        chat_id: str,
        role: str,
        content: str,
        mode: str = "chat",
        task_id: Optional[str] = None,
        code_run_id: Optional[str] = None,
        desktop_run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatMessageRecord:
        async with self._session_factory() as db:
            chat = await db.get(ChatThreadRecord, chat_id)
            if chat is None:
                raise NotFoundError(f"chat {chat_id} not found")
            message = ChatMessageRecord(
                message_id=new_id("msg"),
                chat_id=chat_id,
                role=role,
                content=content,
                mode=mode,
                task_id=task_id,
                code_run_id=code_run_id,
                desktop_run_id=desktop_run_id,
                message_metadata=metadata or {},
            )
            chat.updated_at = now_utc()
            db.add(message)
            await db.commit()
            await db.refresh(message)
            return message

    async def list_chat_messages(
        self, chat_id: str, limit: int = 200
    ) -> List[ChatMessageRecord]:
        await self.get_chat(chat_id)
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(ChatMessageRecord)
                .where(ChatMessageRecord.chat_id == chat_id)
                .order_by(ChatMessageRecord.created_at)
                .limit(limit)
            )
            return list(rows.all())

    async def create_workspace(
        self,
        request: WorkspaceCreate,
        *,
        root_path: str,
        repository_kind: str,
        git_root: Optional[str],
        branch: Optional[str],
        tenant_id: str = "default",
        user_id: str = "local-user",
    ) -> WorkspaceRecord:
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.tenant_id == tenant_id,
                    WorkspaceRecord.root_path == root_path,
                )
            )
            if existing is not None:
                if existing.active:
                    raise ConflictError("workspace path is already registered")
                existing.active = True
                existing.name = request.name
                existing.repository_kind = repository_kind
                existing.git_root = git_root
                existing.branch = branch
                existing.updated_at = now_utc()
                await db.commit()
                await db.refresh(existing)
                return existing
            workspace = WorkspaceRecord(
                workspace_id=new_id("ws"),
                tenant_id=tenant_id,
                user_id=user_id,
                name=request.name,
                root_path=root_path,
                repository_kind=repository_kind,
                git_root=git_root,
                branch=branch,
            )
            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)
            return workspace

    async def list_workspaces(
        self, tenant_id: Optional[str] = None, limit: int = 100
    ) -> List[WorkspaceRecord]:
        statement = (
            select(WorkspaceRecord)
            .where(WorkspaceRecord.active.is_(True))
            .order_by(desc(WorkspaceRecord.updated_at))
            .limit(limit)
        )
        if tenant_id:
            statement = statement.where(WorkspaceRecord.tenant_id == tenant_id)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())

    async def get_workspace(
        self, workspace_id: str, *, active_only: bool = True
    ) -> WorkspaceRecord:
        async with self._session_factory() as db:
            workspace = await db.get(WorkspaceRecord, workspace_id)
            if workspace is None or (active_only and not workspace.active):
                raise NotFoundError(f"workspace {workspace_id} not found")
            return workspace

    async def deactivate_workspace(self, workspace_id: str) -> None:
        async with self._session_factory() as db:
            workspace = await db.get(WorkspaceRecord, workspace_id)
            if workspace is None or not workspace.active:
                raise NotFoundError(f"workspace {workspace_id} not found")
            active_run = await db.scalar(
                select(CodeRunRecord.code_run_id).where(
                    CodeRunRecord.workspace_id == workspace_id,
                    CodeRunRecord.status.in_(
                        [
                            CodeRunStatus.CREATED.value,
                            CodeRunStatus.RUNNING.value,
                            CodeRunStatus.PAUSED.value,
                            CodeRunStatus.REVIEW_REQUIRED.value,
                            CodeRunStatus.FAILED.value,
                            CodeRunStatus.INTERRUPTED.value,
                        ]
                    ),
                )
            )
            if active_run is not None:
                raise ConflictError(
                    "workspace has an active code run; apply or discard it before unregistering"
                )
            workspace.active = False
            workspace.updated_at = now_utc()
            await db.execute(
                update(ChatThreadRecord)
                .where(ChatThreadRecord.workspace_id == workspace_id)
                .values(workspace_id=None, updated_at=now_utc())
            )
            await db.commit()

    async def create_code_run(
        self,
        *,
        workspace_id: str,
        instruction: str,
        chat_id: Optional[str] = None,
        engine: str = "native",
        model: str = "",
    ) -> CodeRunRecord:
        async with self._session_factory() as db:
            workspace = await db.get(WorkspaceRecord, workspace_id)
            if workspace is None or not workspace.active:
                raise NotFoundError(f"workspace {workspace_id} not found")
            if chat_id is not None:
                chat = await db.get(ChatThreadRecord, chat_id)
                if chat is None:
                    raise NotFoundError(f"chat {chat_id} not found")
                if chat.workspace_id != workspace_id:
                    raise ConflictError("chat is not bound to this workspace")
            record = CodeRunRecord(
                code_run_id=new_id("code"),
                workspace_id=workspace_id,
                chat_id=chat_id,
                session_id=new_id("sess"),
                instruction=instruction,
                status=CodeRunStatus.CREATED.value,
                engine=engine,
                model=model,
                next_turn_ordinal=2,
            )
            db.add(record)
            await db.flush()
            db.add(
                CodeTurnRecord(
                    turn_id=new_id("turn"),
                    code_run_id=record.code_run_id,
                    ordinal=1,
                    instruction=instruction,
                    status=CodeTurnStatus.QUEUED.value,
                )
            )
            await db.commit()
            await db.refresh(record)
            return record

    async def get_code_run(self, code_run_id: str) -> CodeRunRecord:
        async with self._session_factory() as db:
            record = await db.get(CodeRunRecord, code_run_id)
            if record is None:
                raise NotFoundError(f"code run {code_run_id} not found")
            return record

    async def get_code_run_by_session(self, session_id: str) -> CodeRunRecord:
        async with self._session_factory() as db:
            record = await db.scalar(
                select(CodeRunRecord).where(CodeRunRecord.session_id == session_id)
            )
            if record is None:
                raise NotFoundError(f"code run session {session_id} not found")
            return record

    async def acquire_code_run_lease(
        self, code_run_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        now = now_utc()
        expires = now + timedelta(seconds=ttl_seconds)
        async with self._session_factory() as db:
            result = await db.execute(
                update(CodeRunRecord)
                .where(
                    CodeRunRecord.code_run_id == code_run_id,
                    or_(
                        CodeRunRecord.lease_owner == owner,
                        CodeRunRecord.lease_owner.is_(None),
                        CodeRunRecord.lease_expires_at.is_(None),
                        CodeRunRecord.lease_expires_at < now,
                    ),
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=expires,
                    state_version=CodeRunRecord.state_version + 1,
                )
            )
            await db.commit()
            return bool(cast(Any, result).rowcount)

    async def renew_code_run_lease(
        self, code_run_id: str, owner: str, ttl_seconds: int
    ) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CodeRunRecord)
                .where(
                    CodeRunRecord.code_run_id == code_run_id,
                    CodeRunRecord.lease_owner == owner,
                )
                .values(
                    lease_expires_at=now_utc() + timedelta(seconds=ttl_seconds),
                    state_version=CodeRunRecord.state_version + 1,
                )
            )
            await db.commit()
            return bool(cast(Any, result).rowcount)

    async def release_code_run_lease(self, code_run_id: str, owner: str) -> None:
        async with self._session_factory() as db:
            await db.execute(
                update(CodeRunRecord)
                .where(
                    CodeRunRecord.code_run_id == code_run_id,
                    CodeRunRecord.lease_owner == owner,
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                    active_turn_id=None,
                    state_version=CodeRunRecord.state_version + 1,
                )
            )
            await db.commit()

    async def list_code_runs(
        self, workspace_id: Optional[str] = None, limit: int = 100
    ) -> List[CodeRunRecord]:
        statement = select(CodeRunRecord).order_by(desc(CodeRunRecord.created_at)).limit(limit)
        if workspace_id:
            statement = statement.where(CodeRunRecord.workspace_id == workspace_id)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())

    async def get_latest_open_code_run(self, chat_id: str) -> Optional[CodeRunRecord]:
        async with self._session_factory() as db:
            return cast(
                Optional[CodeRunRecord],
                await db.scalar(
                    select(CodeRunRecord)
                    .where(
                        CodeRunRecord.chat_id == chat_id,
                        CodeRunRecord.status.in_(
                            [
                                CodeRunStatus.CREATED.value,
                                CodeRunStatus.RUNNING.value,
                                CodeRunStatus.PAUSED.value,
                                CodeRunStatus.REVIEW_REQUIRED.value,
                                CodeRunStatus.FAILED.value,
                                CodeRunStatus.INTERRUPTED.value,
                            ]
                        ),
                    )
                    .order_by(desc(CodeRunRecord.created_at))
                    .limit(1)
                )
            )

    async def create_code_turn(self, code_run_id: str, instruction: str) -> CodeTurnRecord:
        allowed = {
            CodeRunStatus.CREATED.value,
            CodeRunStatus.RUNNING.value,
            CodeRunStatus.PAUSED.value,
            CodeRunStatus.REVIEW_REQUIRED.value,
            CodeRunStatus.FAILED.value,
            CodeRunStatus.INTERRUPTED.value,
        }
        async with self._session_factory() as db:
            run = await db.get(CodeRunRecord, code_run_id)
            if run is None:
                raise NotFoundError(f"code run {code_run_id} not found")
            if run.status not in allowed:
                raise ConflictError(f"cannot continue code run in state {run.status}")
            next_ordinal = await db.scalar(
                update(CodeRunRecord)
                .where(
                    CodeRunRecord.code_run_id == code_run_id,
                    CodeRunRecord.status.in_(allowed),
                )
                .values(
                    next_turn_ordinal=CodeRunRecord.next_turn_ordinal + 1,
                    state_version=CodeRunRecord.state_version + 1,
                    status=case(
                        (
                            CodeRunRecord.status == CodeRunStatus.RUNNING.value,
                            CodeRunRecord.status,
                        ),
                        else_=CodeRunStatus.CREATED.value,
                    ),
                    finished_at=None,
                    error=None,
                )
                .returning(CodeRunRecord.next_turn_ordinal)
            )
            if next_ordinal is None:
                raise ConflictError("code run changed while queuing a turn")
            ordinal = int(next_ordinal) - 1
            turn = CodeTurnRecord(
                turn_id=new_id("turn"),
                code_run_id=code_run_id,
                ordinal=ordinal,
                instruction=instruction,
                status=CodeTurnStatus.QUEUED.value,
            )
            db.add(turn)
            await db.commit()
            await db.refresh(turn)
            return turn

    async def list_code_turns(self, code_run_id: str) -> List[CodeTurnRecord]:
        await self.get_code_run(code_run_id)
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(CodeTurnRecord)
                .where(CodeTurnRecord.code_run_id == code_run_id)
                .order_by(CodeTurnRecord.ordinal)
            )
            return list(rows.all())

    async def next_queued_code_turn(self, code_run_id: str) -> Optional[CodeTurnRecord]:
        async with self._session_factory() as db:
            return cast(
                Optional[CodeTurnRecord],
                await db.scalar(
                    select(CodeTurnRecord)
                    .where(
                        CodeTurnRecord.code_run_id == code_run_id,
                        CodeTurnRecord.status == CodeTurnStatus.QUEUED.value,
                    )
                    .order_by(CodeTurnRecord.ordinal)
                    .limit(1)
                )
            )

    async def update_code_turn(
        self,
        turn_id: str,
        status: CodeTurnStatus,
        *,
        summary: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> CodeTurnRecord:
        async with self._session_factory() as db:
            turn = await db.get(CodeTurnRecord, turn_id)
            if turn is None:
                raise NotFoundError(f"code turn {turn_id} not found")
            turn.status = status.value
            if status == CodeTurnStatus.RUNNING:
                turn.started_at = now_utc()
                turn.finished_at = None
            if status in {
                CodeTurnStatus.COMPLETED,
                CodeTurnStatus.FAILED,
                CodeTurnStatus.CANCELLED,
            }:
                turn.finished_at = now_utc()
            if summary is not None:
                turn.summary = summary
            if error is not None:
                turn.error = error
            await db.commit()
            await db.refresh(turn)
            return turn

    async def cancel_open_code_turns(self, code_run_id: str) -> int:
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CodeTurnRecord).where(
                            CodeTurnRecord.code_run_id == code_run_id,
                            CodeTurnRecord.status.in_(
                                [CodeTurnStatus.QUEUED.value, CodeTurnStatus.RUNNING.value]
                            ),
                        )
                    )
                ).all()
            )
            for turn in rows:
                turn.status = CodeTurnStatus.CANCELLED.value
                turn.finished_at = now_utc()
            await db.commit()
            return len(rows)

    async def update_code_run(
        self,
        code_run_id: str,
        *,
        status: Optional[CodeRunStatus] = None,
        isolation_kind: Optional[str] = None,
        isolated_path: Optional[str] = None,
        base_revision: Optional[str] = None,
        baseline_manifest: Optional[Dict[str, Any]] = None,
        applied_manifest: Optional[Dict[str, Any]] = None,
        final_summary: Optional[str] = None,
        changed_paths: Optional[List[str]] = None,
        diff_ref: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
        review_revision: Optional[int] = None,
        review_manifest: Optional[Dict[str, Any]] = None,
        active_turn_id: Any = _UNSET,
    ) -> CodeRunRecord:
        async with self._session_factory() as db:
            record = await db.get(CodeRunRecord, code_run_id)
            if record is None:
                raise NotFoundError(f"code run {code_run_id} not found")
            if status is not None:
                record.status = status.value
                if status == CodeRunStatus.RUNNING and record.started_at is None:
                    record.started_at = now_utc()
                if status == CodeRunStatus.RUNNING:
                    record.finished_at = None
                    record.error = None
                if status in {
                    CodeRunStatus.APPLIED,
                    CodeRunStatus.DISCARDED,
                    CodeRunStatus.REVERTED,
                    CodeRunStatus.FAILED,
                    CodeRunStatus.INTERRUPTED,
                }:
                    record.finished_at = now_utc()
            if isolation_kind is not None:
                record.isolation_kind = isolation_kind
            if isolated_path is not None:
                record.isolated_path = isolated_path
            if base_revision is not None:
                record.base_revision = base_revision
            if baseline_manifest is not None:
                record.baseline_manifest = baseline_manifest
            if applied_manifest is not None:
                record.applied_manifest = applied_manifest
            if final_summary is not None:
                record.final_summary = final_summary
            if changed_paths is not None:
                record.changed_paths = changed_paths
            if diff_ref is not None:
                record.diff_ref = diff_ref
            if error is not None:
                record.error = error
            if review_revision is not None:
                record.review_revision = review_revision
            if review_manifest is not None:
                record.review_manifest = review_manifest
            if active_turn_id is not _UNSET:
                record.active_turn_id = active_turn_id
            record.state_version = int(record.state_version or 0) + 1
            await db.commit()
            await db.refresh(record)
            return record

    async def interrupt_running_code_runs(self) -> int:
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CodeRunRecord).where(
                            CodeRunRecord.status == CodeRunStatus.RUNNING.value
                        )
                    )
                ).all()
            )
            for record in rows:
                record.status = CodeRunStatus.INTERRUPTED.value
                record.finished_at = now_utc()
                record.error = {"reason": "server_restarted"}
                record.lease_owner = None
                record.lease_expires_at = None
                record.active_turn_id = None
                record.state_version = int(record.state_version or 0) + 1
                await db.execute(
                    update(ToolCallRecord)
                    .where(
                        ToolCallRecord.code_run_id == record.code_run_id,
                        ToolCallRecord.phase == CodeToolPhase.RUNNING.value,
                    )
                    .values(phase=CodeToolPhase.UNKNOWN.value, status="unknown")
                )
                turns = list(
                    (
                        await db.scalars(
                            select(CodeTurnRecord).where(
                                CodeTurnRecord.code_run_id == record.code_run_id,
                                CodeTurnRecord.status == CodeTurnStatus.RUNNING.value,
                            )
                        )
                    ).all()
                )
                for turn in turns:
                    turn.status = CodeTurnStatus.CANCELLED.value
                    turn.finished_at = now_utc()
            await db.commit()
            return len(rows)

    async def create_code_review(
        self,
        code_run_id: str,
        *,
        revision: int,
        changed_paths: List[str],
        manifest: Dict[str, Any],
        diff_ref: str,
        status: CodeRunStatus,
        final_summary: str,
    ) -> CodeReviewRecord:
        async with self._session_factory() as db:
            run = await db.get(CodeRunRecord, code_run_id)
            if run is None:
                raise NotFoundError(f"code run {code_run_id} not found")
            if revision != int(run.review_revision or 0) + 1:
                raise ConflictError("review revision is stale")
            review = CodeReviewRecord(
                review_id=new_id("review"),
                code_run_id=code_run_id,
                revision=revision,
                changed_paths=changed_paths,
                manifest=manifest,
                diff_ref=diff_ref,
            )
            run.status = status.value
            run.final_summary = final_summary
            run.changed_paths = changed_paths
            run.diff_ref = diff_ref
            run.review_revision = revision
            run.review_manifest = manifest
            db.add(review)
            await db.commit()
            await db.refresh(review)
            return review

    async def get_code_review(self, code_run_id: str, revision: int) -> CodeReviewRecord:
        async with self._session_factory() as db:
            review = await db.scalar(
                select(CodeReviewRecord).where(
                    CodeReviewRecord.code_run_id == code_run_id,
                    CodeReviewRecord.revision == revision,
                )
            )
            if review is None:
                raise NotFoundError(
                    f"review revision {revision} for code run {code_run_id} not found"
                )
            return review

    async def begin_tool_call(
        self,
        *,
        code_run_id: str,
        session_id: str,
        turn_id: Optional[str],
        external_call_id: str,
        step_no: int,
        tool_name: str,
        arguments: Dict[str, Any],
        request_digest: str,
        before_hashes: Dict[str, Optional[str]],
        expected_after_hashes: Dict[str, Optional[str]],
    ) -> Tuple[ToolCallRecord, bool]:
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(ToolCallRecord).where(
                    ToolCallRecord.code_run_id == code_run_id,
                    ToolCallRecord.external_call_id == external_call_id,
                )
            )
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise ConflictError("tool call id was reused with different arguments")
                return existing, False
            record = ToolCallRecord(
                tool_call_id=new_id("tool"),
                code_run_id=code_run_id,
                session_id=session_id,
                turn_id=turn_id,
                external_call_id=external_call_id,
                step_no=step_no,
                tool_name=tool_name,
                arguments=arguments,
                status="running",
                phase=CodeToolPhase.RUNNING.value,
                request_digest=request_digest,
                before_hashes=before_hashes,
                expected_after_hashes=expected_after_hashes,
                started_at=now_utc(),
                result_excerpt="",
                affected_paths=list(before_hashes),
                diff_summary=[],
                after_hashes={},
                unified_diff="",
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record, True

    async def complete_tool_call(
        self,
        tool_call_id: str,
        *,
        phase: CodeToolPhase,
        status: str,
        result_excerpt: str,
        result_digest: str,
        affected_paths: List[str],
        diff_summary: List[str],
        before_hashes: Dict[str, Optional[str]],
        after_hashes: Dict[str, Optional[str]],
        unified_diff: str,
    ) -> ToolCallRecord:
        async with self._session_factory() as db:
            record = await db.get(ToolCallRecord, tool_call_id)
            if record is None:
                raise NotFoundError(f"tool call {tool_call_id} not found")
            record.phase = phase.value
            record.status = status
            record.result_excerpt = result_excerpt
            record.result_digest = result_digest
            record.affected_paths = affected_paths
            record.diff_summary = diff_summary
            record.before_hashes = before_hashes
            record.after_hashes = after_hashes
            record.unified_diff = unified_diff
            record.finished_at = now_utc()
            await db.commit()
            await db.refresh(record)
            return record

    async def list_incomplete_tool_calls(self, code_run_id: str) -> List[ToolCallRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(ToolCallRecord)
                .where(
                    ToolCallRecord.code_run_id == code_run_id,
                    ToolCallRecord.phase.in_(
                        [CodeToolPhase.RUNNING.value, CodeToolPhase.UNKNOWN.value]
                    ),
                )
                .order_by(ToolCallRecord.step_no)
            )
            return list(rows.all())

    async def resolve_incomplete_tool_call(
        self,
        tool_call_id: str,
        *,
        phase: CodeToolPhase,
        status: str,
        result_excerpt: str,
        after_hashes: Dict[str, Optional[str]],
    ) -> None:
        async with self._session_factory() as db:
            record = await db.get(ToolCallRecord, tool_call_id)
            if record is None:
                raise NotFoundError(f"tool call {tool_call_id} not found")
            record.phase = phase.value
            record.status = status
            record.result_excerpt = result_excerpt
            record.after_hashes = after_hashes
            record.finished_at = now_utc()
            await db.commit()

    async def mark_running_tool_calls_unknown(self, code_run_id: str) -> int:
        async with self._session_factory() as db:
            result = await db.execute(
                update(ToolCallRecord)
                .where(
                    ToolCallRecord.code_run_id == code_run_id,
                    ToolCallRecord.phase == CodeToolPhase.RUNNING.value,
                )
                .values(phase=CodeToolPhase.UNKNOWN.value, status="unknown")
            )
            await db.commit()
            return int(cast(Any, result).rowcount or 0)

    async def append_tool_call(
        self,
        *,
        code_run_id: str,
        session_id: str,
        turn_id: Optional[str] = None,
        step_no: int,
        tool_name: str,
        arguments: Dict[str, Any],
        status: str,
        result_excerpt: str,
        affected_paths: List[str],
        diff_summary: List[str],
        before_hashes: Dict[str, Optional[str]],
        after_hashes: Dict[str, Optional[str]],
        unified_diff: str,
    ) -> ToolCallRecord:
        async with self._session_factory() as db:
            record = ToolCallRecord(
                tool_call_id=new_id("tool"),
                code_run_id=code_run_id,
                session_id=session_id,
                turn_id=turn_id,
                step_no=step_no,
                tool_name=tool_name,
                arguments=arguments,
                status=status,
                phase=(
                    CodeToolPhase.FAILED.value
                    if status == "error"
                    else CodeToolPhase.SUCCEEDED.value
                ),
                started_at=now_utc(),
                finished_at=now_utc(),
                result_excerpt=result_excerpt,
                affected_paths=affected_paths,
                diff_summary=diff_summary,
                before_hashes=before_hashes,
                after_hashes=after_hashes,
                unified_diff=unified_diff,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def list_tool_calls(self, code_run_id: str) -> List[ToolCallRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(ToolCallRecord)
                .where(ToolCallRecord.code_run_id == code_run_id)
                .order_by(ToolCallRecord.step_no)
            )
            return list(rows.all())

    async def create_apply_journal(
        self,
        *,
        code_run_id: str,
        workspace_id: str,
        review_revision: int,
        paths: List[str],
    ) -> CodeApplyJournalRecord:
        async with self._session_factory() as db:
            record = CodeApplyJournalRecord(
                journal_id=new_id("apply"),
                code_run_id=code_run_id,
                workspace_id=workspace_id,
                review_revision=review_revision,
                status=CodeApplyStatus.PREPARED.value,
                paths=paths,
                completed_paths=[],
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def update_apply_journal(
        self,
        journal_id: str,
        *,
        status: Optional[CodeApplyStatus] = None,
        completed_paths: Optional[List[str]] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> CodeApplyJournalRecord:
        async with self._session_factory() as db:
            record = await db.get(CodeApplyJournalRecord, journal_id)
            if record is None:
                raise NotFoundError(f"apply journal {journal_id} not found")
            if status is not None:
                record.status = status.value
            if completed_paths is not None:
                record.completed_paths = completed_paths
            if error is not None:
                record.error = error
            record.updated_at = now_utc()
            await db.commit()
            await db.refresh(record)
            return record

    async def list_incomplete_apply_journals(self) -> List[CodeApplyJournalRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(CodeApplyJournalRecord)
                .where(
                    CodeApplyJournalRecord.status.in_(
                        [
                            CodeApplyStatus.PREPARED.value,
                            CodeApplyStatus.APPLYING.value,
                            CodeApplyStatus.ROLLING_BACK.value,
                        ]
                    )
                )
                .order_by(CodeApplyJournalRecord.created_at)
            )
            return list(rows.all())

    async def create_desktop_run(
        self,
        *,
        instruction: str,
        chat_id: Optional[str] = None,
        target_window_id: Optional[int] = None,
    ) -> DesktopRunRecord:
        async with self._session_factory() as db:
            if chat_id is not None:
                chat = await db.get(ChatThreadRecord, chat_id)
                if chat is None:
                    raise NotFoundError(f"chat {chat_id} not found")
            record = DesktopRunRecord(
                desktop_run_id=new_id("desktop"),
                chat_id=chat_id,
                session_id=new_id("sess"),
                instruction=instruction,
                status=DesktopRunStatus.CREATED.value,
                target_window_id=target_window_id,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def get_desktop_run(self, desktop_run_id: str) -> DesktopRunRecord:
        async with self._session_factory() as db:
            record = await db.get(DesktopRunRecord, desktop_run_id)
            if record is None:
                raise NotFoundError(f"desktop run {desktop_run_id} not found")
            return record

    async def get_desktop_run_by_session(self, session_id: str) -> DesktopRunRecord:
        async with self._session_factory() as db:
            record = await db.scalar(
                select(DesktopRunRecord).where(DesktopRunRecord.session_id == session_id)
            )
            if record is None:
                raise NotFoundError(f"desktop run session {session_id} not found")
            return record

    async def list_desktop_runs(
        self, chat_id: Optional[str] = None, limit: int = 100
    ) -> List[DesktopRunRecord]:
        statement = (
            select(DesktopRunRecord)
            .order_by(desc(DesktopRunRecord.created_at))
            .limit(limit)
        )
        if chat_id:
            statement = statement.where(DesktopRunRecord.chat_id == chat_id)
        async with self._session_factory() as db:
            return list((await db.scalars(statement)).all())

    async def update_desktop_run(
        self,
        desktop_run_id: str,
        *,
        status: Optional[DesktopRunStatus] = None,
        target_window_id: Optional[int] = None,
        target_title: Optional[str] = None,
        target_process: Optional[str] = None,
        target_rect: Optional[Dict[str, Any]] = None,
        pending_action: Optional[Dict[str, Any]] = None,
        final_summary: Optional[str] = None,
        last_screenshot_ref: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> DesktopRunRecord:
        async with self._session_factory() as db:
            record = await db.get(DesktopRunRecord, desktop_run_id)
            if record is None:
                raise NotFoundError(f"desktop run {desktop_run_id} not found")
            if status is not None:
                record.status = status.value
                if status == DesktopRunStatus.RUNNING:
                    record.started_at = now_utc()
                    record.finished_at = None
                    record.error = None
                    record.pending_action = {}
                    record.final_summary = None
                if status in {
                    DesktopRunStatus.COMPLETED,
                    DesktopRunStatus.FAILED,
                    DesktopRunStatus.TERMINATED,
                    DesktopRunStatus.INTERRUPTED,
                }:
                    record.finished_at = now_utc()
            if target_window_id is not None:
                record.target_window_id = target_window_id
            if target_title is not None:
                record.target_title = target_title
            if target_process is not None:
                record.target_process = target_process
            if target_rect is not None:
                record.target_rect = target_rect
            if pending_action is not None:
                record.pending_action = pending_action
            if final_summary is not None:
                record.final_summary = final_summary
            if last_screenshot_ref is not None:
                record.last_screenshot_ref = last_screenshot_ref
            if error is not None:
                record.error = error
            await db.commit()
            await db.refresh(record)
            return record

    async def interrupt_running_desktop_runs(self) -> int:
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(DesktopRunRecord).where(
                            DesktopRunRecord.status.in_(
                                [
                                    DesktopRunStatus.RUNNING.value,
                                    DesktopRunStatus.WAITING_APPROVAL.value,
                                    DesktopRunStatus.WAITING_CAPTURE_CONSENT.value,
                                ]
                            )
                        )
                    )
                ).all()
            )
            for record in rows:
                record.status = DesktopRunStatus.INTERRUPTED.value
                record.finished_at = now_utc()
                record.error = {"reason": "server_restarted"}
            await db.commit()
            return len(rows)

    async def append_desktop_action(
        self,
        *,
        desktop_run_id: str,
        session_id: str,
        step_no: int,
        action_name: str,
        arguments: Dict[str, Any],
        status: str,
        screenshot_before: Optional[str],
        screenshot_after: Optional[str],
        execution_result: Dict[str, Any],
        approval_required: bool = False,
    ) -> DesktopActionRecord:
        async with self._session_factory() as db:
            record = DesktopActionRecord(
                action_id=new_id("dact"),
                desktop_run_id=desktop_run_id,
                session_id=session_id,
                step_no=step_no,
                action_name=action_name,
                arguments=arguments,
                status=status,
                screenshot_before=screenshot_before,
                screenshot_after=screenshot_after,
                execution_result=execution_result,
                approval_required=approval_required,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def list_desktop_actions(self, desktop_run_id: str) -> List[DesktopActionRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(DesktopActionRecord)
                .where(DesktopActionRecord.desktop_run_id == desktop_run_id)
                .order_by(DesktopActionRecord.step_no)
            )
            return list(rows.all())

    async def update_desktop_action(
        self,
        action_id: str,
        *,
        status: str,
        screenshot_before: Optional[str] = None,
        screenshot_after: Optional[str] = None,
        execution_result: Optional[Dict[str, Any]] = None,
    ) -> DesktopActionRecord:
        async with self._session_factory() as db:
            record = await db.get(DesktopActionRecord, action_id)
            if record is None:
                raise NotFoundError(f"desktop action {action_id} not found")
            record.status = status
            if screenshot_before is not None:
                record.screenshot_before = screenshot_before
            if screenshot_after is not None:
                record.screenshot_after = screenshot_after
            if execution_result is not None:
                record.execution_result = execution_result
            await db.commit()
            await db.refresh(record)
            return record

    async def create_desktop_approval(
        self,
        *,
        desktop_run_id: str,
        session_id: str,
        action_summary: str,
        risk_description: str,
        pending_payload: Dict[str, Any],
    ) -> DesktopApprovalRecord:
        async with self._session_factory() as db:
            record = DesktopApprovalRecord(
                approval_id=new_id("dapproval"),
                desktop_run_id=desktop_run_id,
                session_id=session_id,
                action_summary=action_summary,
                risk_description=risk_description,
                status=ApprovalStatus.PENDING.value,
                pending_payload=pending_payload,
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)
            return record

    async def get_desktop_approval(self, approval_id: str) -> DesktopApprovalRecord:
        async with self._session_factory() as db:
            record = await db.get(DesktopApprovalRecord, approval_id)
            if record is None:
                raise NotFoundError(f"desktop approval {approval_id} not found")
            return record

    async def list_desktop_approvals(self, desktop_run_id: str) -> List[DesktopApprovalRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(DesktopApprovalRecord)
                .where(DesktopApprovalRecord.desktop_run_id == desktop_run_id)
                .order_by(DesktopApprovalRecord.requested_at)
            )
            return list(rows.all())

    async def decide_desktop_approval(
        self, approval_id: str, status: ApprovalStatus, comment: Optional[str]
    ) -> DesktopApprovalRecord:
        async with self._session_factory() as db:
            record = await db.get(DesktopApprovalRecord, approval_id)
            if record is None:
                raise NotFoundError(f"desktop approval {approval_id} not found")
            if record.status != ApprovalStatus.PENDING.value:
                raise ConflictError("desktop approval is no longer pending")
            record.status = status.value
            record.comment = comment
            record.decided_at = now_utc()
            await db.commit()
            await db.refresh(record)
            return record

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
            # There is intentionally no ORM relationship between these records.
            # Flush the parent explicitly so SQLite foreign-key enforcement does
            # not depend on unit-of-work mapper ordering.
            db.add(task)
            await db.flush()
            db.add(session)
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
            sequence = int(
                (
                    await db.execute(
                        text(
                            "INSERT INTO ff_event_sequences (session_id, next_sequence) "
                            "VALUES (:session_id, 1) "
                            "ON CONFLICT(session_id) DO UPDATE SET "
                            "next_sequence=ff_event_sequences.next_sequence + 1 "
                            "RETURNING next_sequence"
                        ),
                        {"session_id": session_id},
                    )
                ).scalar_one()
            )
            event_payload = dict(payload or {})
            event_payload["sequence"] = sequence
            event = EventRecord(
                event_id=new_id("evt"),
                session_id=session_id,
                sequence=sequence,
                event_type=event_type,
                message=message,
                payload=event_payload,
            )
            db.add(event)
            await db.commit()
            await db.refresh(event)
            return event

    async def list_events(
        self, session_id: str, limit: int = 500, after_sequence: int = 0
    ) -> List[EventRecord]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(EventRecord)
                .where(
                    EventRecord.session_id == session_id,
                    EventRecord.sequence > after_sequence,
                )
                .order_by(EventRecord.sequence)
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
