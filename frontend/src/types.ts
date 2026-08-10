export type SessionState =
  | "CREATED"
  | "PLANNED"
  | "RUNNING"
  | "PAUSED"
  | "WAITING_USER_INPUT"
  | "WAITING_APPROVAL"
  | "HANDOFF"
  | "RESUMING"
  | "COMPLETED"
  | "FAILED"
  | "TERMINATED"
  | "EXPIRED";

export interface PlanStep {
  step_id: string;
  order: number;
  name: string;
  step_type: string;
  executor: string;
  status: string;
}

export interface Task {
  task_id: string;
  session_id: string;
  tenant_id: string;
  user_id: string;
  task_name: string;
  description: string;
  start_url: string;
  status: SessionState;
  risk_level: "low" | "medium" | "high" | "critical";
  allowed_domains: string[];
  plan: PlanStep[];
  final_result?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  session: {
    session_id: string;
    state: SessionState;
    resume_token: string;
    current_step_id?: string;
    last_screenshot_ref?: string;
    runtime_state: Record<string, unknown>;
    created_at: string;
    updated_at: string;
  };
}

export interface SessionEvent {
  event_id: string;
  session_id: string;
  event_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Approval {
  approval_id: string;
  task_id: string;
  session_id: string;
  action_summary: string;
  risk_description: string;
  status: "pending" | "approved" | "rejected" | "expired";
  pending_payload: Record<string, unknown>;
  requested_at: string;
  decided_at?: string;
  comment?: string;
}

export interface BrowserAction {
  action_id: string;
  step_no: number;
  action_type: string;
  action_parameters: Record<string, unknown>;
  page_url?: string;
  screenshot_after?: string;
  execution_result: Record<string, unknown>;
  created_at: string;
}

export interface ChatMessage {
  message_id: string;
  chat_id: string;
  role: "user" | "assistant";
  content: string;
  mode: "chat" | "automation" | "code";
  task_id?: string;
  code_run_id?: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ChatSummary {
  chat_id: string;
  tenant_id: string;
  user_id: string;
  title: string;
  workspace_id?: string;
  created_at: string;
  updated_at: string;
}

export interface Chat extends ChatSummary {
  messages: ChatMessage[];
}

export interface ChatReply {
  route: "chat" | "automation" | "code";
  chat: Chat;
  task?: Task;
  code_run?: CodeRun;
}

export type ChatStreamEvent =
  | { type: "route"; route: "chat" | "automation" | "code" }
  | { type: "reasoning_delta"; content: string }
  | { type: "reasoning_done" }
  | { type: "delta"; content: string }
  | { type: "done"; reply: ChatReply }
  | { type: "error"; message: string };

export type RequestedMode = "auto" | "chat" | "automation" | "code";

export interface Workspace {
  workspace_id: string;
  name: string;
  root_path: string;
  repository_kind: "git" | "directory";
  git_root?: string;
  branch?: string;
  is_dirty: boolean;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceDirectorySelection {
  path?: string;
  name?: string;
}

export interface WorkspaceTreeEntry {
  path: string;
  name: string;
  kind: "file" | "directory";
  size?: number;
}

export interface WorkspaceFile {
  path: string;
  content: string;
  start_line: number;
  end_line: number;
  total_lines: number;
}

export type CodeRunStatus =
  | "CREATED"
  | "RUNNING"
  | "REVIEW_REQUIRED"
  | "APPLIED"
  | "DISCARDED"
  | "REVERTED"
  | "FAILED"
  | "INTERRUPTED";

export interface ToolCall {
  tool_call_id: string;
  step_no: number;
  tool_name: string;
  status: string;
  affected_paths: string[];
  diff_summary: string[];
  before_hashes: Record<string, string | null>;
  after_hashes: Record<string, string | null>;
  unified_diff: string;
  result_excerpt: string;
  created_at: string;
}

export interface CodeRun {
  code_run_id: string;
  workspace_id: string;
  chat_id?: string;
  session_id: string;
  instruction: string;
  status: CodeRunStatus;
  isolation_kind?: string;
  base_revision?: string;
  final_summary?: string;
  changed_paths: string[];
  diff_ref?: string;
  error?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  tool_calls: ToolCall[];
}

export interface CodeDiff {
  code_run_id: string;
  status: CodeRunStatus;
  changed_paths: string[];
  diff: string;
}

