from enum import Enum


class SessionState(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_USER_INPUT = "WAITING_USER_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    HANDOFF = "HANDOFF"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ExecutorRoute(str, Enum):
    API = "api"
    SELECTOR = "selector"
    FARA = "fara"
    APPROVAL = "approval"
    HUMAN = "human_takeover"


class CodeRunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPLIED = "APPLIED"
    DISCARDED = "DISCARDED"
    REVERTED = "REVERTED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class CodeTurnStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CodeToolPhase(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class CodeApplyStatus(str, Enum):
    PREPARED = "PREPARED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class DesktopRunStatus(str, Enum):
    CREATED = "CREATED"
    WAITING_CAPTURE_CONSENT = "WAITING_CAPTURE_CONSENT"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    HANDOFF = "HANDOFF"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"
    INTERRUPTED = "INTERRUPTED"
