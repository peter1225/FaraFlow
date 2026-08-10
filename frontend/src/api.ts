import type {
  Approval,
  BrowserAction,
  Chat,
  ChatReply,
  ChatStreamEvent,
  ChatSummary,
  CodeDiff,
  CodeRun,
  RequestedMode,
  SessionEvent,
  Task,
  Workspace,
  WorkspaceDirectorySelection,
  WorkspaceFile,
  WorkspaceTreeEntry,
} from "./types";

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
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function streamChatRequest(
  chatId: string,
  content: string,
  requestedMode: RequestedMode,
  autoStart: boolean,
  enableThinking: boolean,
  onDelta: (content: string) => void,
  onReasoningDelta: (content: string) => void,
  onReasoningDone: () => void,
): Promise<ChatReply> {
  const response = await fetch(`${API_BASE}/v1/chats/${chatId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content,
      requested_mode: requestedMode,
      auto_start: autoStart,
      enable_thinking: enableThinking,
    }),
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `请求失败（HTTP ${response.status}）`);
  }
  if (!response.body) throw new Error("浏览器不支持流式响应");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: ChatReply | undefined;

  const consumeLine = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as ChatStreamEvent;
    if (event.type === "delta") onDelta(event.content);
    if (event.type === "reasoning_delta") onReasoningDelta(event.content);
    if (event.type === "reasoning_done") onReasoningDone();
    if (event.type === "done") result = event.reply;
    if (event.type === "error") throw new Error(event.message);
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      consumeLine(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf("\n");
    }
    if (done) break;
  }
  consumeLine(buffer);
  if (!result) throw new Error("流式响应未正常结束");
  return result;
}

export const api = {
  listChats: () => request<ChatSummary[]>("/v1/chats"),
  getChat: (chatId: string) => request<Chat>(`/v1/chats/${chatId}`),
  createChat: (payload: Record<string, unknown> = {}) =>
    request<Chat>("/v1/chats", { method: "POST", body: JSON.stringify(payload) }),
  sendChatMessage: (
    chatId: string,
    content: string,
    requestedMode: RequestedMode = "auto",
    autoStart = true,
    enableThinking = false,
  ) =>
    request<ChatReply>(`/v1/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content,
        requested_mode: requestedMode,
        auto_start: autoStart,
        enable_thinking: enableThinking,
      }),
    }),
  streamChatMessage: (
    chatId: string,
    content: string,
    requestedMode: RequestedMode = "auto",
    autoStart = true,
    enableThinking = false,
    onDelta: (content: string) => void = () => undefined,
    onReasoningDelta: (content: string) => void = () => undefined,
    onReasoningDone: () => void = () => undefined,
  ) => streamChatRequest(
    chatId,
    content,
    requestedMode,
    autoStart,
    enableThinking,
    onDelta,
    onReasoningDelta,
    onReasoningDone,
  ),
  listWorkspaces: () => request<Workspace[]>("/v1/workspaces"),
  getWorkspace: (workspaceId: string) =>
    request<Workspace>(`/v1/workspaces/${workspaceId}`),
  createWorkspace: (payload: { name: string; root_path: string }) =>
    request<Workspace>("/v1/workspaces", { method: "POST", body: JSON.stringify(payload) }),
  pickWorkspaceDirectory: () =>
    request<WorkspaceDirectorySelection>("/v1/workspaces/pick-directory", {
      method: "POST",
      headers: { "X-FaraFlow-Local-Action": "pick-directory" },
    }),
  deleteWorkspace: (workspaceId: string) =>
    request<void>(`/v1/workspaces/${workspaceId}`, { method: "DELETE" }),
  workspaceTree: (workspaceId: string, path = "") =>
    request<WorkspaceTreeEntry[]>(
      `/v1/workspaces/${workspaceId}/tree?path=${encodeURIComponent(path)}`,
    ),
  workspaceFile: (workspaceId: string, path: string) =>
    request<WorkspaceFile>(
      `/v1/workspaces/${workspaceId}/files?path=${encodeURIComponent(path)}`,
    ),
  listCodeRuns: (workspaceId?: string) =>
    request<CodeRun[]>(
      `/v1/code-runs${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ""}`,
    ),
  getCodeRun: (codeRunId: string) => request<CodeRun>(`/v1/code-runs/${codeRunId}`),
  codeRunDiff: (codeRunId: string) =>
    request<CodeDiff>(`/v1/code-runs/${codeRunId}/diff`),
  codeRunEvents: (codeRunId: string) =>
    request<SessionEvent[]>(`/v1/code-runs/${codeRunId}/events`),
  applyCodeRun: (codeRunId: string) =>
    request<CodeRun>(`/v1/code-runs/${codeRunId}/apply`, { method: "POST" }),
  revertCodeRun: (codeRunId: string) =>
    request<CodeRun>(`/v1/code-runs/${codeRunId}/revert`, { method: "POST" }),
  discardCodeRun: (codeRunId: string) =>
    request<CodeRun>(`/v1/code-runs/${codeRunId}/discard`, { method: "POST" }),
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

