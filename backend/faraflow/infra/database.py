from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class WorkspaceRecord(Base):
    __tablename__ = "ff_workspaces"
    __table_args__ = (UniqueConstraint("tenant_id", "root_path"),)

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    root_path: Mapped[str] = mapped_column(Text)
    repository_kind: Mapped[str] = mapped_column(String(20))
    git_root: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ChatThreadRecord(Base):
    __tablename__ = "ff_chat_threads"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    workspace_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_workspaces.workspace_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )


class ChatMessageRecord(Base):
    __tablename__ = "ff_chat_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_chat_threads.chat_id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="chat")
    task_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_tasks.task_id", ondelete="SET NULL"), nullable=True
    )
    code_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_code_runs.code_run_id", ondelete="SET NULL"), nullable=True
    )
    desktop_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_desktop_runs.desktop_run_id", ondelete="SET NULL"), nullable=True
    )
    message_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskRecord(Base):
    __tablename__ = "ff_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    task_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    start_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    risk_level: Mapped[str] = mapped_column(String(20))
    allowed_domains: Mapped[List[str]] = mapped_column(JSON, default=list)
    approval_policy: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    runtime_policy: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    plan: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    final_result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionRecord(Base):
    __tablename__ = "ff_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_tasks.task_id", ondelete="CASCADE"), unique=True
    )
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    current_step_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resume_token: Mapped[str] = mapped_column(String(128))
    allowed_domains: Mapped[List[str]] = mapped_column(JSON, default=list)
    runtime_state: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_screenshot_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ActionRecord(Base):
    __tablename__ = "ff_actions"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_tasks.task_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(60))
    action_parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    page_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executor_type: Mapped[str] = mapped_column(String(30), default="fara")
    screenshot_before: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenshot_after: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_result: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_result: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalRecord(Base):
    __tablename__ = "ff_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_tasks.task_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    action_summary: Mapped[str] = mapped_column(Text)
    risk_description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    pending_payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EventRecord(Base):
    __tablename__ = "ff_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeRunRecord(Base):
    __tablename__ = "ff_code_runs"

    code_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_workspaces.workspace_id", ondelete="CASCADE"), index=True
    )
    chat_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_chat_threads.chat_id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    isolation_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    isolated_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_revision: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    baseline_manifest: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    applied_manifest: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    final_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_paths: Mapped[List[str]] = mapped_column(JSON, default=list)
    diff_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCallRecord(Base):
    __tablename__ = "ff_tool_calls"
    __table_args__ = (UniqueConstraint("code_run_id", "step_no"),)

    tool_call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_code_runs.code_run_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30))
    result_excerpt: Mapped[str] = mapped_column(Text, default="")
    affected_paths: Mapped[List[str]] = mapped_column(JSON, default=list)
    diff_summary: Mapped[List[str]] = mapped_column(JSON, default=list)
    before_hashes: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    after_hashes: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    unified_diff: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DesktopRunRecord(Base):
    __tablename__ = "ff_desktop_runs"

    desktop_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("ff_chat_threads.chat_id", ondelete="SET NULL"), nullable=True
    )
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    target_window_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_process: Mapped[Optional[str]] = mapped_column(String(260), nullable=True)
    target_rect: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    pending_action: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    final_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_screenshot_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DesktopActionRecord(Base):
    __tablename__ = "ff_desktop_actions"

    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    desktop_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_desktop_runs.desktop_run_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    action_name: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30))
    screenshot_before: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    screenshot_after: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_result: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DesktopApprovalRecord(Base):
    __tablename__ = "ff_desktop_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    desktop_run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ff_desktop_runs.desktop_run_id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    action_summary: Mapped[str] = mapped_column(Text)
    risk_description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    pending_payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SkillRecord(Base):
    __tablename__ = "ff_skills"
    __table_args__ = (UniqueConstraint("skill_name", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(String(30), index=True)
    manifest: Mapped[Dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as db_session:
            yield db_session

    async def dispose(self) -> None:
        await self.engine.dispose()
