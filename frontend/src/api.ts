import type { Approval, BrowserAction, SessionEvent, Task } from "./types";

const configuredBase = import.meta.env.VITE_API_BASE_URL as string | undefined;
export const API_BASE = (configuredBase ?? "").replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as T;
}

export const api = {
  listTasks: () => request<Task[]>("/v1/tasks"),
  getTask: (taskId: string) => request<Task>(`/v1/tasks/${taskId}`),
  createTask: (payload: Record<string, unknown>) =>
    request<Task>("/v1/tasks", { method: "POST", body: JSON.stringify(payload) }),
  startTask: (taskId: string) =>
    request<Task>(`/v1/tasks/${taskId}/start`, { method: "POST" }),
  pauseTask: (taskId: string) =>
    request<Task>(`/v1/tasks/${taskId}/pause`, { method: "POST" }),
  terminateTask: (taskId: string) =>
    request<Task>(`/v1/tasks/${taskId}/terminate`, { method: "POST" }),
  respond: (taskId: string, response: string, resumeToken: string) =>
    request<Task>(`/v1/tasks/${taskId}/respond`, {
      method: "POST",
      body: JSON.stringify({ response, resume_token: resumeToken }),
    }),
  events: (taskId: string) =>
    request<SessionEvent[]>(`/v1/tasks/${taskId}/events`),
  actions: (taskId: string) =>
    request<BrowserAction[]>(`/v1/tasks/${taskId}/actions`),
  approvals: (taskId: string) =>
    request<Approval[]>(`/v1/tasks/${taskId}/approvals`),
  decideApproval: (
    taskId: string,
    approvalId: string,
    decision: "approve" | "reject",
    resumeToken: string,
  ) =>
    request<Approval>(`/v1/tasks/${taskId}/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ decision, resume_token: resumeToken }),
    }),
};

export function eventWebSocketUrl(sessionId: string): string {
  const base = API_BASE || window.location.origin;
  const url = new URL(base, window.location.origin);
  const protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${url.host}/v1/sessions/${sessionId}/events`;
}

export function artifactUrl(reference?: string): string | undefined {
  return reference ? `${API_BASE}${reference}` : undefined;
}

