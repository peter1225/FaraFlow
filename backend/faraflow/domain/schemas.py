from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import ApprovalStatus, CodeRunStatus, RiskLevel, SessionState


class ApprovalPolicy(BaseModel):
    before_submit: bool = True
    before_delete: bool = True
    before_purchase: bool = True
    before_send: bool = True
    before_sign_in: bool = True


class RuntimePolicy(BaseModel):
    max_model_actions: int = Field(default=100, ge=1, le=500)
    max_runtime_minutes: int = Field(default=30, ge=1, le=1440)
    max_step_retries: int = Field(default=3, ge=0, le=10)


class TaskCreate(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(default="default", min_length=1, max_length=100)
    user_id: str = Field(default="local-user", min_length=1, max_length=100)
    description: str = Field(min_length=3, max_length=10_000)
    start_url: str = Field(default="https://www.bing.com/")
    allowed_domains: List[str] = Field(default_factory=lambda: ["bing.com"])
    risk_level: RiskLevel = RiskLevel.MEDIUM
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, value: List[str]) -> List[str]:
        normalized = []
        for domain in value:
            item = domain.strip().lower().rstrip(".")
            if "://" in item:
                raise ValueError("allowed_domains must contain hostnames, not URLs")
            if item and item not in normalized:
                normalized.append(item)
        if not normalized:
            raise ValueError("at least one allowed domain is required")
        return normalized


class ChatCreate(BaseModel):
    tenant_id: str = Field(default="default", min_length=1, max_length=100)
    user_id: str = Field(default="local-user", min_length=1, max_length=100)
    title: str = Field(default="新对话", min_length=1, max_length=200)
    workspace_id: Optional[str] = None


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    auto_start: bool = True
    requested_mode: Literal["auto", "chat", "automation", "code"] = "auto"
    enable_thinking: bool = False


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1, max_length=2000)


class WorkspaceDirectorySelection(BaseModel):
    path: Optional[str] = None
    name: Optional[str] = None


class WorkspaceView(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    repository_kind: Literal["git", "directory"]
    git_root: Optional[str] = None
    branch: Optional[str] = None
    is_dirty: bool = False
    created_at: datetime
    updated_at: datetime


class WorkspaceTreeEntry(BaseModel):
    path: str
    name: str
    kind: Literal["file", "directory"]
    size: Optional[int] = None


class WorkspaceFileView(BaseModel):
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int


class CodeRunCreate(BaseModel):
    workspace_id: str
    instruction: str = Field(min_length=1, max_length=10_000)
    chat_id: Optional[str] = None
    auto_start: bool = True


class ToolCallView(BaseModel):
    tool_call_id: str
    step_no: int
    tool_name: str
    status: str
    affected_paths: List[str] = Field(default_factory=list)
    diff_summary: List[str] = Field(default_factory=list)
    before_hashes: Dict[str, Optional[str]] = Field(default_factory=dict)
    after_hashes: Dict[str, Optional[str]] = Field(default_factory=dict)
    unified_diff: str = ""
    result_excerpt: str = ""
    created_at: datetime


class CodeRunView(BaseModel):
    code_run_id: str
    workspace_id: str
    chat_id: Optional[str] = None
    session_id: str
    instruction: str
    status: CodeRunStatus
    isolation_kind: Optional[Literal["worktree", "snapshot"]] = None
    base_revision: Optional[str] = None
    final_summary: Optional[str] = None
    changed_paths: List[str] = Field(default_factory=list)
    diff_ref: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    tool_calls: List[ToolCallView] = Field(default_factory=list)


class CodeDiffView(BaseModel):
    code_run_id: str
    status: CodeRunStatus
    changed_paths: List[str] = Field(default_factory=list)
    diff: str = ""


class PlanStep(BaseModel):
    step_id: str
    order: int
    name: str
    step_type: str
    executor: str
    status: str = "pending"


class SessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: str
    state: SessionState
    resume_token: str
    current_step_id: Optional[str] = None
    last_screenshot_ref: Optional[str] = None
    runtime_state: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TaskView(BaseModel):
    task_id: str
    session_id: str
    tenant_id: str
    user_id: str
    task_name: str
    description: str
    start_url: str
    status: SessionState
    risk_level: RiskLevel
    allowed_domains: List[str]
    approval_policy: ApprovalPolicy
    runtime_policy: RuntimePolicy
    plan: List[PlanStep]
    final_result: Optional[Dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    session: Optional[SessionSummary] = None


class ChatMessageView(BaseModel):
    message_id: str
    chat_id: str
    role: Literal["user", "assistant"]
    content: str
    mode: Literal["chat", "automation", "code"] = "chat"
    task_id: Optional[str] = None
    code_run_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ChatSummary(BaseModel):
    chat_id: str
    tenant_id: str
    user_id: str
    title: str
    workspace_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ChatView(ChatSummary):
    messages: List[ChatMessageView] = Field(default_factory=list)


class ChatReply(BaseModel):
    route: Literal["chat", "automation", "code"]
    chat: ChatView
    task: Optional[TaskView] = None
    code_run: Optional[CodeRunView] = None


class SessionEvent(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    message: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalView(BaseModel):
    approval_id: str
    task_id: str
    session_id: str
    action_summary: str
    risk_description: str
    status: ApprovalStatus
    pending_payload: Dict[str, Any]
    requested_at: datetime
    decided_at: Optional[datetime] = None
    comment: Optional[str] = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: Optional[str] = Field(default=None, max_length=1000)
    resume_token: str = Field(min_length=8)


class UserResponse(BaseModel):
    response: str = Field(min_length=1, max_length=10_000)
    resume_token: str = Field(min_length=8)


class SkillManifest(BaseModel):
    skill_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,99}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    category: Literal["deterministic", "browser", "data", "control"]
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    side_effect_level: Literal["none", "low", "medium", "high", "critical"]
    idempotency: Literal["idempotent", "safe_retry", "non_idempotent"]
    approval_policy: str
    sandbox: str
    permissions: List[str]
    audit_tags: List[str] = Field(default_factory=list)
    enabled: bool = True
