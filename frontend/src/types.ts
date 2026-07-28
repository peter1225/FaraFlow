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

