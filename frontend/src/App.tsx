import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, artifactUrl, eventWebSocketUrl } from "./api";
import { CodeWorkspace } from "./CodeWorkspace";
import type {
  Approval,
  BrowserAction,
  Chat,
  ChatMessage,
  ChatSummary,
  CodeRun,
  DesktopApproval,
  DesktopRun,
  DesktopWindow,
  RequestedMode,
  SessionEvent,
  SessionState,
  Task,
  Workspace,
} from "./types";
import { WorkspaceModal } from "./WorkspaceModal";

const STATUS_LABELS: Record<SessionState, string> = {
  CREATED: "已创建",
  PLANNED: "待启动",
  RUNNING: "执行中",
  PAUSED: "已暂停",
  WAITING_USER_INPUT: "等待信息",
  WAITING_APPROVAL: "等待审批",
  HANDOFF: "人工接管",
  RESUMING: "恢复中",
  COMPLETED: "已完成",
  FAILED: "失败",
  TERMINATED: "已终止",
  EXPIRED: "已过期",
};

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const saved = window.localStorage.getItem("faraflow-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function formatTime(value?: string): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function StatusBadge({ state }: { state: SessionState }) {
  return <span className={`status status-${state.toLowerCase()}`}>{STATUS_LABELS[state]}</span>;
}

function Logo() {
  return (
    <div className="brand">
      <div className="brand-mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div>
        <div className="brand-name">FaraFlow</div>
        <div className="brand-caption">AI 工作台</div>
      </div>
    </div>
  );
}

function CreateTask({
  onCreated,
  onClose,
}: {
  onCreated: (task: Task) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("网页信息检索");
  const [description, setDescription] = useState("");
  const [startUrl, setStartUrl] = useState("https://www.bing.com/");
  const [domains, setDomains] = useState("bing.com");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const task = await api.createTask({
        task_name: name,
        description,
        start_url: startUrl,
        allowed_domains: domains
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      });
      onCreated(task);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-heading">
          <div>
            <div className="eyebrow">NEW AUTOMATION</div>
            <h2>创建浏览器任务</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <form onSubmit={submit}>
          <label>
            任务名称
            <input value={name} onChange={(event) => setName(event.target.value)} required />
          </label>
          <label>
            自然语言目标
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              rows={6}
              placeholder="例如：在官网查找最新版本说明，整理前三项主要更新。不要登录或提交任何表单。"
              required
            />
          </label>
          <div className="form-grid">
            <label>
              起始网址
              <input
                value={startUrl}
                onChange={(event) => setStartUrl(event.target.value)}
                required
              />
            </label>
            <label>
              域名白名单（逗号分隔）
              <input
                value={domains}
                onChange={(event) => setDomains(event.target.value)}
                required
              />
            </label>
          </div>
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              取消
            </button>
            <button className="button primary" disabled={saving}>
              {saving ? "创建中…" : "创建任务"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function EmptyState({ onChat, onCreate }: { onChat: () => void; onCreate: () => void }) {
  return (
    <main className="empty-state">
      <div className="empty-visual" aria-hidden="true">
        <div className="orbit orbit-a" />
        <div className="orbit orbit-b" />
        <div className="core">F</div>
      </div>
      <div className="eyebrow">CONVERSATION FIRST · BROWSER ON DEMAND</div>
      <h1>先聊清楚，需要网页时再让浏览器行动</h1>
      <p>普通问题直接回答；需要实时网页信息或页面操作时，FaraFlow 会自动创建受控浏览器任务。</p>
      <div className="empty-actions">
        <button className="button primary large" onClick={onChat}>
          开始对话 <span>→</span>
        </button>
        <button className="button secondary large" onClick={onCreate}>
          直接创建自动化
        </button>
      </div>
    </main>
  );
}

function taskResult(task?: Task): string | undefined {
  if (!task?.final_result) return undefined;
  const answer = task.final_result.answer;
  if (typeof answer === "string") return answer;
  const reason = task.final_result.reason;
  if (typeof reason === "string") return reason;
  return JSON.stringify(task.final_result);
}

function ReasoningDisclosure({
  reasoning,
  streaming,
  hasAnswer,
}: {
  reasoning: string;
  streaming: boolean;
  hasAnswer: boolean;
}) {
  const [open, setOpen] = useState(streaming && !hasAnswer);
  const previousHasAnswer = useRef(hasAnswer);

  useEffect(() => {
    if (streaming && !hasAnswer) {
      setOpen(true);
    } else if (!previousHasAnswer.current && hasAnswer) {
      setOpen(false);
    }
    previousHasAnswer.current = hasAnswer;
  }, [hasAnswer, streaming]);

  return (
    <details
      className="reasoning-disclosure"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="reasoning-chevron" aria-hidden="true">›</span>
        <span>思考过程</span>
        <small>{streaming && !hasAnswer ? "思考中…" : open ? "收起" : "展开"}</small>
      </summary>
      <pre className="reasoning-body">{reasoning}</pre>
    </details>
  );
}

const desktopStatusLabels: Record<DesktopRun["status"], string> = {
  CREATED: "准备中",
  WAITING_CAPTURE_CONSENT: "请选择要控制的窗口",
  RUNNING: "正在执行",
  WAITING_APPROVAL: "等待你的确认",
  PAUSED: "已暂停",
  HANDOFF: "需要人工接管",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  TERMINATED: "已停止",
  INTERRUPTED: "执行被中断",
};

function desktopTargetLabel(title?: string, processName?: string) {
  if (title === "Program Manager" && processName?.toLowerCase() === "explorer.exe") {
    return "Windows 桌面";
  }
  return title ?? "等待选择要控制的窗口";
}

function desktopErrorMessage(error?: Record<string, unknown>) {
  const message = typeof error?.message === "string" ? error.message : "";
  if (message.includes("did not contain a desktop action or final block")) {
    return "模型没有返回可执行的桌面动作。请点击“重新尝试”；如果仍失败，请检查桌面模型输出协议。";
  }
  if (message.includes("invalid desktop action JSON")) {
    return "模型返回的桌面动作 JSON 不完整。系统现已启用结构化输出，请点击“重新尝试”。";
  }
  if (message.includes("validation error for DesktopAction")) {
    return "模型返回的桌面动作缺少必要参数。系统已增强动作参数约束，请点击“重新尝试”。";
  }
  if (message.includes("HTTP 400 from desktop model")) {
    return `桌面模型拒绝了请求：${message}`;
  }
  if (message.includes("repeated the same")) {
    return "模型连续返回了相同桌面动作，系统已停止重复操作以保护桌面。请检查目标图标是否可见后重新尝试。";
  }
  if (message.includes("desktop model call failed")) {
    return `桌面模型调用失败：${message}`;
  }
  return message || "桌面任务执行失败，请重新尝试。";
}

function DesktopRunCard({
  run,
  onChanged,
  onTerminate,
}: {
  run: DesktopRun;
  onChanged: (run: DesktopRun) => void;
  onTerminate?: (desktopRunId: string) => Promise<void>;
}) {
  const [windows, setWindows] = useState<DesktopWindow[]>([]);
  const [approvals, setApprovals] = useState<DesktopApproval[]>([]);
  const [showWindows, setShowWindows] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function loadWindows() {
    setBusy(true);
    setError("");
    try {
      setWindows(await api.listDesktopWindows());
      setShowWindows(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取可控窗口");
    } finally {
      setBusy(false);
    }
  }

  async function selectWindow(windowId: number) {
    setBusy(true);
    setError("");
    try {
      const selected = await api.selectDesktopWindow(run.desktop_run_id, windowId);
      onChanged(selected);
      const started = await api.startDesktopRun(run.desktop_run_id);
      onChanged(started);
      setShowWindows(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "选择窗口失败");
    } finally {
      setBusy(false);
    }
  }

  async function retryRun() {
    setBusy(true);
    setError("");
    try {
      const started = await api.startDesktopRun(run.desktop_run_id);
      onChanged(started);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重新启动桌面任务失败");
    } finally {
      setBusy(false);
    }
  }

  async function loadApprovals() {
    try {
      setApprovals(await api.desktopApprovals(run.desktop_run_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取审批状态");
    }
  }

  async function decide(approval: DesktopApproval, decision: "approve" | "reject") {
    setBusy(true);
    setError("");
    try {
      await api.decideDesktopApproval(run.desktop_run_id, approval.approval_id, decision);
      await loadApprovals();
      onChanged(await api.getDesktopRun(run.desktop_run_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审批操作失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (run.status === "WAITING_APPROVAL") void loadApprovals();
  }, [run.desktop_run_id, run.status]);

  const pendingApproval = approvals.find((item) => item.status === "pending");
  return (
    <div className="automation-message-card desktop-message-card">
      <div className="automation-card-head">
        <div>
          <span className="automation-icon">▣</span>
          <div>
            <small>LOCAL DESKTOP RUN</small>
            <strong>{desktopTargetLabel(run.target_title, run.target_process)}</strong>
          </div>
        </div>
        <span className={`code-status state-${run.status.toLowerCase()}`}>
          {desktopStatusLabels[run.status]}
        </span>
      </div>
      <div className="automation-card-detail">
        <span>{run.actions.length} 个桌面动作</span>
        <span>{run.target_process ?? "未绑定进程"}</span>
      </div>
      {run.last_screenshot_ref && (
        <img
          className="desktop-screenshot"
          src={artifactUrl(run.last_screenshot_ref)}
          alt="最近一次桌面截图"
        />
      )}
      {run.status === "WAITING_CAPTURE_CONSENT" && !showWindows && (
        <p className="desktop-guidance">
          第 1 步：点击“选择应用窗口”；第 2 步：选择要授权的窗口。选择后任务会自动开始。
        </p>
      )}
      {run.status === "FAILED" && (
        <div className="desktop-error" role="alert">
          <strong>为什么失败？</strong>
          <p>{desktopErrorMessage(run.error)}</p>
        </div>
      )}
      {pendingApproval && (
        <div className="desktop-approval">
          <strong>{pendingApproval.action_summary}</strong>
          <p>{pendingApproval.risk_description}</p>
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void decide(pendingApproval, "reject")}
            >
              拒绝
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => void decide(pendingApproval, "approve")}
            >
              允许执行
            </button>
          </div>
        </div>
      )}
      {showWindows && (
        <div className="desktop-window-list">
          {windows.length === 0 && <p>当前没有检测到可见窗口。</p>}
          {windows.map((window) => (
            <button
              className="button secondary"
              key={window.window_id}
              disabled={busy}
              onClick={() => void selectWindow(window.window_id)}
            >
              {desktopTargetLabel(window.title, window.process_name)} ·{" "}
              {window.process_name || "未知进程"}
            </button>
          ))}
        </div>
      )}
      {error && <p className="automation-result">{error}</p>}
      <div className="desktop-card-actions">
        {run.status === "WAITING_CAPTURE_CONSENT" && (
          <button className="button secondary" disabled={busy} onClick={() => void loadWindows()}>
            选择应用窗口
          </button>
        )}
        {run.status === "FAILED" && run.target_window_id && (
          <button className="button primary" disabled={busy} onClick={() => void retryRun()}>
            重新尝试
          </button>
        )}
        {onTerminate &&
          ["RUNNING", "WAITING_APPROVAL", "WAITING_CAPTURE_CONSENT"].includes(run.status) && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => void onTerminate(run.desktop_run_id)}
            >
              停止桌面任务
            </button>
          )}
      </div>
    </div>
  );
}

export function ChatPanel({
  chat,
  tasks,
  codeRuns,
  desktopRuns = [],
  sending,
  onSend,
  onOpenTask,
  onOpenWorkspace,
  onTerminateDesktopRun,
  onDesktopRunChanged,
}: {
  chat: Chat;
  tasks: Task[];
  codeRuns: CodeRun[];
  desktopRuns?: DesktopRun[];
  sending: boolean;
  onSend: (content: string, mode: RequestedMode, enableThinking: boolean) => Promise<void>;
  onOpenTask: (taskId: string) => void;
  onOpenWorkspace: (workspaceId: string) => void;
  onTerminateDesktopRun?: (desktopRunId: string) => Promise<void>;
  onDesktopRunChanged?: (run: DesktopRun) => void;
}) {
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<RequestedMode>(chat.workspace_id ? "code" : "auto");
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    stickToBottomRef.current = true;
    setShowScrollToBottom(false);
    endRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);

  const updateScrollPosition = useCallback(() => {
    const container = messagesRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    const isNearBottom = distanceFromBottom <= 48;
    stickToBottomRef.current = isNearBottom;
    setShowScrollToBottom(!isNearBottom);
  }, []);

  useEffect(() => {
    setMode(chat.workspace_id ? "code" : "auto");
    setThinkingEnabled(false);
    stickToBottomRef.current = true;
    setShowScrollToBottom(false);
    window.requestAnimationFrame(() => scrollToBottom("auto"));
  }, [chat.chat_id, chat.workspace_id, scrollToBottom]);

  useEffect(() => {
    if (stickToBottomRef.current) {
      window.requestAnimationFrame(() => scrollToBottom("auto"));
    } else {
      setShowScrollToBottom(true);
    }
  }, [chat.messages, sending, scrollToBottom]);

  async function submit() {
    const content = draft.trim();
    if (!content || sending) return;
    stickToBottomRef.current = true;
    setShowScrollToBottom(false);
    setDraft("");
    await onSend(
      content,
      mode,
      thinkingEnabled && (mode === "auto" || mode === "chat"),
    );
  }

  return (
    <main className="chat-page">
      <header className="chat-header">
        <div>
          <div className="breadcrumb">
            CHAT <span>/</span> {chat.chat_id.slice(0, 13)}
          </div>
          <h1>{chat.title}</h1>
          <p>普通问题由模型直接回答；需要实时信息时自动接管浏览器。</p>
        </div>
        <div className="chat-route-legend">
          <span><i className="route-dot direct" />直接回答</span>
          <span><i className="route-dot browser" />浏览器任务</span>
          {chat.workspace_id && <span><i className="route-dot code" />本地代码</span>}
        </div>
      </header>

      <section className="chat-surface">
        <div className="chat-scroll-region">
          <div
            className="chat-messages"
            data-testid="chat-messages"
            ref={messagesRef}
            onScroll={updateScrollPosition}
          >
          {chat.messages.length === 0 && (
            <div className="chat-welcome">
              <div className="assistant-avatar">F</div>
              <div>
                <h2>你好，我是 FaraFlow</h2>
                <p>可以直接问我问题，也可以让我查询网页。只有确实需要时，我才会启动浏览器。</p>
                <div className="prompt-suggestions">
                  <button onClick={() => setDraft("介绍一下这个项目的主要功能")}>介绍这个项目</button>
                  <button onClick={() => setDraft("帮我搜索最新的 vLLM 文档并总结")}>查询最新文档</button>
                  <button onClick={() => setDraft("设计一个安全的网页自动化测试任务")}>设计测试任务</button>
                </div>
              </div>
            </div>
          )}

          {chat.messages.map((message) => {
            const linkedTask = message.task_id
              ? tasks.find((item) => item.task_id === message.task_id)
              : undefined;
            const result = taskResult(linkedTask);
            const linkedCodeRun = message.code_run_id
              ? codeRuns.find((item) => item.code_run_id === message.code_run_id)
              : undefined;
            const linkedDesktopRun = message.desktop_run_id
              ? desktopRuns.find((item) => item.desktop_run_id === message.desktop_run_id)
              : undefined;
            const reasoning = typeof message.metadata.reasoning === "string"
              ? message.metadata.reasoning
              : "";
            return (
              <article
                className={`chat-message ${message.role}${message.metadata.streaming === true ? " streaming" : ""}`}
                key={message.message_id}
              >
                <div className="message-avatar">{message.role === "assistant" ? "F" : "U"}</div>
                <div className="message-content">
                  <div className="message-meta">
                    <strong>{message.role === "assistant" ? "FaraFlow" : "你"}</strong>
                    <time>{formatTime(message.created_at)}</time>
                  </div>
                  {reasoning && (
                    <ReasoningDisclosure
                      reasoning={reasoning}
                      streaming={message.metadata.streaming === true}
                      hasAnswer={message.content.trim().length > 0}
                    />
                  )}
                  {message.content && <p>{message.content}</p>}
                  {message.mode === "automation" && message.task_id && (
                    <div className="automation-message-card">
                      <div className="automation-card-head">
                        <div>
                          <span className="automation-icon">◎</span>
                          <div>
                            <small>BROWSER AUTOMATION</small>
                            <strong>{linkedTask?.task_name ?? "浏览器任务"}</strong>
                          </div>
                        </div>
                        {linkedTask && <StatusBadge state={linkedTask.status} />}
                      </div>
                      {linkedTask && (
                        <div className="automation-card-detail">
                          <span>{linkedTask.allowed_domains.join(" · ")}</span>
                          <span>{Number(linkedTask.session.runtime_state.action_count ?? 0)} actions</span>
                        </div>
                      )}
                      {result && <p className="automation-result">{result}</p>}
                      <button className="button secondary" onClick={() => onOpenTask(message.task_id!)}>
                        查看执行过程 <span>↗</span>
                      </button>
                    </div>
                  )}
                  {message.mode === "code" && message.code_run_id && (
                    <div className="automation-message-card code-message-card">
                      <div className="automation-card-head">
                        <div>
                          <span className="automation-icon">⌘</span>
                          <div>
                            <small>LOCAL CODE RUN</small>
                            <strong>{linkedCodeRun?.final_summary ?? "正在隔离工作区执行"}</strong>
                          </div>
                        </div>
                        <span className={`code-status state-${linkedCodeRun?.status.toLowerCase() ?? "created"}`}>
                          {linkedCodeRun?.status ?? "CREATED"}
                        </span>
                      </div>
                      {linkedCodeRun?.changed_paths.length ? (
                        <div className="automation-card-detail">
                          <span>{linkedCodeRun.changed_paths.length} 个文件有变化</span>
                          <span>原目录尚未自动覆盖</span>
                        </div>
                      ) : null}
                      {chat.workspace_id && (
                        <button className="button secondary" onClick={() => onOpenWorkspace(chat.workspace_id!)}>
                          查看工具步骤与 Diff <span>↗</span>
                        </button>
                      )}
                    </div>
                  )}
                  {message.mode === "desktop" && linkedDesktopRun && (
                    <DesktopRunCard
                      run={linkedDesktopRun}
                      onChanged={(next) => onDesktopRunChanged?.(next)}
                      onTerminate={onTerminateDesktopRun}
                    />
                  )}
                </div>
              </article>
            );
          })}

          {sending && !chat.messages.some((message) => message.metadata.streaming === true) && (
              <article className="chat-message assistant pending">
                <div className="message-avatar">F</div>
                {thinkingEnabled && (mode === "auto" || mode === "chat") ? (
                  <div className="message-content thinking-status">
                    <span aria-hidden="true">✦</span>
                    <span>正在深度思考…</span>
                  </div>
                ) : (
                  <div className="message-content typing-indicator">
                    <span />
                    <span />
                    <span />
                  </div>
                )}
              </article>
            )}
            <div ref={endRef} />
          </div>
          {showScrollToBottom && (
            <button
              type="button"
              className="chat-scroll-to-bottom"
              aria-label="跳到最新消息"
              title="跳到最新消息"
              onClick={() => scrollToBottom("smooth")}
            >
              <span />
              <span />
              <span />
            </button>
          )}
        </div>

        <form
          className="chat-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            rows={3}
            placeholder="输入问题。需要实时网页信息时，我会自动启动浏览器…"
          />
          <div className="composer-footer">
            <label className="mode-select">
              <span>模式</span>
              <select
                value={mode}
                onChange={(event) => {
                  const nextMode = event.target.value as RequestedMode;
                  setMode(nextMode);
                  if (
                    nextMode === "automation" ||
                    nextMode === "code" ||
                    nextMode === "desktop"
                  ) {
                    setThinkingEnabled(false);
                  }
                }}
              >
                <option value="auto">自动</option>
                <option value="chat">聊天</option>
                <option value="automation">浏览器</option>
                <option value="code" disabled={!chat.workspace_id}>代码</option>
                <option value="desktop">桌面控制</option>
              </select>
            </label>
            <button
              type="button"
              className={`thinking-toggle${thinkingEnabled ? " active" : ""}`}
              aria-pressed={thinkingEnabled}
              disabled={sending || (mode !== "auto" && mode !== "chat")}
              title="开启后模型会先进行深度思考，再输出最终回答"
              onClick={() => setThinkingEnabled((current) => !current)}
            >
              <span aria-hidden="true">✦</span>
              深度思考
            </button>
            <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
            <button className="button primary" disabled={sending || !draft.trim()}>
              {sending ? "处理中…" : "发送"}
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}

function TaskDetail({
  task,
  events,
  actions,
  approvals,
  busy,
  onCommand,
  onRefresh,
}: {
  task: Task;
  events: SessionEvent[];
  actions: BrowserAction[];
  approvals: Approval[];
  busy: boolean;
  onCommand: (command: string, payload?: string) => Promise<void>;
  onRefresh: () => void;
}) {
  const [response, setResponse] = useState("");
  const pendingApproval = approvals.find((item) => item.status === "pending");
  const actionCount = Number(task.session.runtime_state.action_count ?? actions.length);
  const latestScreenshot = artifactUrl(task.session.last_screenshot_ref);

  return (
    <main className="task-detail">
      <header className="detail-header">
        <div>
          <div className="breadcrumb">
            TASKS <span>/</span> {task.task_id.slice(0, 13)}
          </div>
          <div className="title-line">
            <h1>{task.task_name}</h1>
            <StatusBadge state={task.status} />
          </div>
          <p>{task.description}</p>
        </div>
        <div className="command-bar">
          {["PLANNED", "PAUSED"].includes(task.status) && (
            <button className="button primary" disabled={busy} onClick={() => onCommand("start")}>
              ▶ {task.status === "PAUSED" ? "继续执行" : "启动任务"}
            </button>
          )}
          {task.status === "RUNNING" && (
            <button className="button secondary" disabled={busy} onClick={() => onCommand("pause")}>
              Ⅱ 暂停
            </button>
          )}
          {!["COMPLETED", "FAILED", "TERMINATED", "EXPIRED"].includes(task.status) && (
            <button className="button danger ghost" disabled={busy} onClick={() => onCommand("terminate")}>
              终止
            </button>
          )}
          <button className="icon-button" onClick={onRefresh} title="刷新">
            ↻
          </button>
        </div>
      </header>

      {(task.status === "WAITING_APPROVAL" || pendingApproval) && pendingApproval && (
        <section className="approval-callout">
          <div className="approval-icon">!</div>
          <div className="approval-copy">
            <div className="eyebrow">HUMAN APPROVAL REQUIRED</div>
            <h3>{pendingApproval.risk_description}</h3>
            <p>{pendingApproval.action_summary}</p>
          </div>
          <div className="approval-buttons">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => onCommand("reject", pendingApproval.approval_id)}
            >
              拒绝并终止
            </button>
            <button
              className="button warning"
              disabled={busy}
              onClick={() => onCommand("approve", pendingApproval.approval_id)}
            >
              确认执行
            </button>
          </div>
        </section>
      )}

      {task.status === "WAITING_USER_INPUT" && (
        <section className="approval-callout info">
          <div className="approval-icon">?</div>
          <div className="approval-copy">
            <div className="eyebrow">FARA NEEDS INPUT</div>
            <h3>{String(task.session.runtime_state.pending_question ?? "请补充任务信息")}</h3>
            <div className="inline-response">
              <input
                value={response}
                onChange={(event) => setResponse(event.target.value)}
                placeholder="输入回复…"
              />
              <button
                className="button primary"
                disabled={busy || !response.trim()}
                onClick={() => {
                  void onCommand("respond", response);
                  setResponse("");
                }}
              >
                回复并继续
              </button>
            </div>
          </div>
        </section>
      )}

      <section className="stats-grid">
        <div className="stat-card">
          <span>MODEL ACTIONS</span>
          <strong>{actionCount}</strong>
          <small>上限 100</small>
        </div>
        <div className="stat-card">
          <span>ALLOWED DOMAINS</span>
          <strong>{task.allowed_domains.length}</strong>
          <small>{task.allowed_domains[0]}</small>
        </div>
        <div className="stat-card">
          <span>APPROVALS</span>
          <strong>{approvals.length}</strong>
          <small>{approvals.filter((item) => item.status === "approved").length} 已通过</small>
        </div>
        <div className="stat-card">
          <span>RUNTIME</span>
          <strong>{task.started_at ? "LIVE" : "—"}</strong>
          <small>{formatTime(task.started_at)}</small>
        </div>
      </section>

      <section className="workspace-grid">
        <div className="browser-panel panel">
          <div className="panel-header">
            <div>
              <span className="live-dot" />
              浏览器画面
            </div>
            <span>1440 × 900</span>
          </div>
          <div className="browser-frame">
            {latestScreenshot ? (
              <img src={latestScreenshot} alt="最新浏览器截图" />
            ) : (
              <div className="screenshot-placeholder">
                <span>◎</span>
                <p>任务启动后，这里会显示最新截图</p>
              </div>
            )}
          </div>
          <div className="browser-footer">
            <span className="lock">⌾</span>
            <span>{String(task.session.runtime_state.current_url ?? task.start_url)}</span>
          </div>
        </div>

        <div className="panel timeline-panel">
          <div className="panel-header">
            <div>执行时间线</div>
            <span>{events.length} EVENTS</span>
          </div>
          <div className="timeline">
            {events.length === 0 && <div className="timeline-empty">等待事件…</div>}
            {[...events].reverse().map((event) => (
              <article className="event" key={event.event_id}>
                <div className={`event-node ${event.event_type.includes("security") ? "alert" : ""}`} />
                <div className="event-body">
                  <div className="event-meta">
                    <span>{event.event_type}</span>
                    <time>{formatTime(event.created_at)}</time>
                  </div>
                  <p>{event.message}</p>
                  {typeof event.payload.screenshot_ref === "string" && (
                    <a href={artifactUrl(event.payload.screenshot_ref)} target="_blank" rel="noreferrer">
                      查看截图 ↗
                    </a>
                  )}
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="panel plan-panel">
        <div className="panel-header">
          <div>执行计划</div>
          <span>COORDINATOR CONTROLLED</span>
        </div>
        <div className="plan-list">
          {task.plan.map((step) => (
            <div className="plan-step" key={step.step_id}>
              <span className="step-index">{String(step.order).padStart(2, "0")}</span>
              <div>
                <strong>{step.name}</strong>
                <small>
                  {step.executor} · {step.step_type}
                </small>
              </div>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [codeRuns, setCodeRuns] = useState<CodeRun[]>([]);
  const [desktopRuns, setDesktopRuns] = useState<DesktopRun[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [selected, setSelected] = useState<Task>();
  const [selectedChatId, setSelectedChatId] = useState<string>();
  const [selectedChat, setSelectedChat] = useState<Chat>();
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string>();
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [actions, setActions] = useState<BrowserAction[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [showWorkspaceCreate, setShowWorkspaceCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [sendingChat, setSendingChat] = useState(false);
  const sendingChatRef = useRef(false);
  const [error, setError] = useState("");
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("faraflow-theme", theme);
  }, [theme]);

  const loadNavigation = useCallback(async () => {
    try {
      const [taskRows, chatRows, workspaceRows] = await Promise.all([
        api.listTasks(),
        api.listChats(),
        api.listWorkspaces().catch(() => [] as Workspace[]),
      ]);
      const runRows = workspaceRows.length
        ? await api.listCodeRuns().catch(() => [] as CodeRun[])
        : [];
      const desktopRows = await api.listDesktopRuns().catch(() => [] as DesktopRun[]);
      setTasks(taskRows);
      setChats(chatRows);
      setWorkspaces(workspaceRows);
      setCodeRuns(runRows);
      setDesktopRuns(desktopRows);
      if (!selectedId && !selectedChatId && !selectedWorkspaceId) {
        if (chatRows.length > 0) setSelectedChatId(chatRows[0].chat_id);
        else if (taskRows.length > 0) setSelectedId(taskRows[0].task_id);
        else if (workspaceRows.length > 0) setSelectedWorkspaceId(workspaceRows[0].workspace_id);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载工作区");
    } finally {
      setLoading(false);
    }
  }, [selectedChatId, selectedId, selectedWorkspaceId]);

  const loadDetail = useCallback(async (taskId: string) => {
    const [task, eventRows, actionRows, approvalRows] = await Promise.all([
      api.getTask(taskId),
      api.events(taskId),
      api.actions(taskId),
      api.approvals(taskId),
    ]);
    setSelected(task);
    setEvents(eventRows);
    setActions(actionRows);
    setApprovals(approvalRows);
    setTasks((current) => current.map((item) => (item.task_id === task.task_id ? task : item)));
  }, []);

  useEffect(() => {
    void loadNavigation();
  }, [loadNavigation]);

  useEffect(() => {
    if (!selectedId) return;
    void loadDetail(selectedId).catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "无法加载任务详情"),
    );
    const timer = window.setInterval(() => {
      void loadDetail(selectedId).catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [loadDetail, selectedId]);

  useEffect(() => {
    if (!selectedChatId) return;
    const refresh = async () => {
      const [chat, taskRows, chatRows, runRows, desktopRows] = await Promise.all([
        api.getChat(selectedChatId),
        api.listTasks(),
        api.listChats(),
        api.listCodeRuns().catch(() => [] as CodeRun[]),
        api.listDesktopRuns(selectedChatId).catch(() => [] as DesktopRun[]),
      ]);
      if (sendingChatRef.current) return;
      setSelectedChat(chat);
      setTasks(taskRows);
      setChats(chatRows);
      setCodeRuns(runRows);
      setDesktopRuns(desktopRows);
    };
    void refresh().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "无法加载对话"),
    );
    const timer = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [selectedChatId]);

  useEffect(() => {
    if (!selected?.session_id) return;
    const socket = new WebSocket(eventWebSocketUrl(selected.session_id));
    socket.onmessage = (message) => {
      const incoming = JSON.parse(message.data as string) as SessionEvent;
      setEvents((current) => {
        if (current.some((item) => item.event_id === incoming.event_id)) return current;
        return [...current, incoming];
      });
      if (
        ["session.completed", "session.failed", "session.approval_required", "action.completed"].includes(
          incoming.event_type,
        )
      ) {
        void loadDetail(selected.task_id);
      }
    };
    return () => socket.close();
  }, [loadDetail, selected?.session_id, selected?.task_id]);

  const stats = useMemo(
    () => ({
      running: tasks.filter((task) => task.status === "RUNNING").length,
      waiting: tasks.filter((task) =>
        ["WAITING_APPROVAL", "WAITING_USER_INPUT", "HANDOFF"].includes(task.status),
      ).length,
    }),
    [tasks],
  );

  const selectedWorkspace = useMemo(
    () => workspaces.find((item) => item.workspace_id === selectedWorkspaceId),
    [selectedWorkspaceId, workspaces],
  );

  async function deleteWorkspace(workspaceId: string) {
    if (!window.confirm("只取消注册，不会删除原文件夹。确定继续吗？")) return;
    setBusy(true);
    setError("");
    try {
      await api.deleteWorkspace(workspaceId);
      setWorkspaces((current) => current.filter((item) => item.workspace_id !== workspaceId));
      setSelectedWorkspaceId(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消工作区注册失败");
    } finally {
      setBusy(false);
    }
  }

  function selectTask(taskId: string) {
    setSelectedWorkspaceId(undefined);
    setSelectedChatId(undefined);
    setSelectedChat(undefined);
    setSelectedId(taskId);
  }

  function selectChat(chatId: string) {
    setSelectedWorkspaceId(undefined);
    setSelectedId(undefined);
    setSelected(undefined);
    setSelectedChatId(chatId);
  }

  function selectWorkspace(workspaceId: string) {
    setSelectedId(undefined);
    setSelected(undefined);
    setSelectedChatId(undefined);
    setSelectedChat(undefined);
    setSelectedWorkspaceId(workspaceId);
  }

  async function createChat(workspaceId?: string) {
    setBusy(true);
    setError("");
    try {
      const workspace = workspaces.find((item) => item.workspace_id === workspaceId);
      const chat = await api.createChat({
        title: workspace ? `${workspace.name} · 代码对话` : "新对话",
        workspace_id: workspaceId,
      });
      setChats((current) => [chat, ...current]);
      setSelectedId(undefined);
      setSelected(undefined);
      setSelectedWorkspaceId(undefined);
      setSelectedChatId(chat.chat_id);
      setSelectedChat(chat);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建对话失败");
    } finally {
      setBusy(false);
    }
  }

  async function sendChat(
    content: string,
    mode: RequestedMode,
    enableThinking: boolean,
  ) {
    if (!selectedChat) return;
    const before = selectedChat;
    const turnId = Date.now();
    const optimistic: ChatMessage = {
      message_id: `pending-${turnId}`,
      chat_id: selectedChat.chat_id,
      role: "user",
      content,
      mode: "chat",
      metadata: {},
      created_at: new Date().toISOString(),
    };
    setSelectedChat({ ...selectedChat, messages: [...selectedChat.messages, optimistic] });
    sendingChatRef.current = true;
    setSendingChat(true);
    setError("");
    try {
      let streamedContent = "";
      let reasoningContent = "";
      let reasoningDone = false;
      const updateStreamingMessage = () => {
        setSelectedChat((current) => {
          if (!current || current.chat_id !== selectedChat.chat_id) return current;
          const streamingId = `streaming-${turnId}`;
          const existing = current.messages.findIndex(
            (message) => message.message_id === streamingId,
          );
          const streamingMessage: ChatMessage = {
            message_id: streamingId,
            chat_id: selectedChat.chat_id,
            role: "assistant",
            content: streamedContent,
            mode: "chat",
            metadata: {
              streaming: true,
              thinking_enabled: enableThinking,
              reasoning: reasoningContent,
              reasoning_done: reasoningDone,
            },
            created_at: new Date().toISOString(),
          };
          if (existing < 0) {
            return { ...current, messages: [...current.messages, streamingMessage] };
          }
          const messages = [...current.messages];
          messages[existing] = streamingMessage;
          return { ...current, messages };
        });
      };
      const response = await api.streamChatMessage(
        selectedChat.chat_id,
        content,
        mode,
        true,
        enableThinking,
        (delta) => {
          streamedContent += delta;
          updateStreamingMessage();
        },
        (delta) => {
          reasoningContent += delta;
          updateStreamingMessage();
        },
        () => {
          reasoningDone = true;
        },
      );
      setSelectedChat(response.chat);
      const summary: ChatSummary = {
        chat_id: response.chat.chat_id,
        tenant_id: response.chat.tenant_id,
        user_id: response.chat.user_id,
        title: response.chat.title,
        created_at: response.chat.created_at,
        updated_at: response.chat.updated_at,
      };
      setChats((current) => [summary, ...current.filter((item) => item.chat_id !== summary.chat_id)]);
      if (response.task) {
        setTasks((current) => [
          response.task!,
          ...current.filter((item) => item.task_id !== response.task!.task_id),
        ]);
      }
      if (response.code_run) {
        setCodeRuns((current) => [
          response.code_run!,
          ...current.filter((item) => item.code_run_id !== response.code_run!.code_run_id),
        ]);
      }
      if (response.desktop_run) {
        setDesktopRuns((current) => [
          response.desktop_run!,
          ...current.filter(
            (item) => item.desktop_run_id !== response.desktop_run!.desktop_run_id,
          ),
        ]);
      }
    } catch (reason) {
      setSelectedChat(before);
      setError(reason instanceof Error ? reason.message : "发送消息失败");
    } finally {
      sendingChatRef.current = false;
      setSendingChat(false);
    }
  }

  async function command(commandName: string, payload?: string) {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      if (commandName === "start") await api.startTask(selected.task_id);
      if (commandName === "pause") await api.pauseTask(selected.task_id);
      if (commandName === "terminate") await api.terminateTask(selected.task_id);
      if (commandName === "respond" && payload)
        await api.respond(selected.task_id, payload, selected.session.resume_token);
      if ((commandName === "approve" || commandName === "reject") && payload)
        await api.decideApproval(
          selected.task_id,
          payload,
          commandName,
          selected.session.resume_token,
        );
      await loadDetail(selected.task_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function terminateDesktopRun(desktopRunId: string) {
    try {
      const run = await api.terminateDesktopRun(desktopRunId);
      setDesktopRuns((current) =>
        current.map((item) => (item.desktop_run_id === run.desktop_run_id ? run : item)),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "停止桌面任务失败");
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Logo />
        <div className="sidebar-actions">
          <button className="new-task new-chat" disabled={busy} onClick={() => void createChat()}>
            <span>＋</span> 新建对话
          </button>
          <button className="new-task secondary-action" onClick={() => setShowCreate(true)}>
            <span>◎</span> 新建自动化
          </button>
          <button className="new-task secondary-action" onClick={() => setShowWorkspaceCreate(true)}>
            <span>⌘</span> 添加本机文件夹
          </button>
        </div>
        <div className="sidebar-scroll">
          <div className="nav-label">代码工作区</div>
          <nav className="task-list">
            {workspaces.map((workspace) => (
              <button
                key={workspace.workspace_id}
                className={`task-nav ${selectedWorkspaceId === workspace.workspace_id ? "active" : ""}`}
                onClick={() => selectWorkspace(workspace.workspace_id)}
              >
                <span className="chat-state-icon">⌘</span>
                <span className="task-nav-copy">
                  <strong>{workspace.name}</strong>
                  <small>{workspace.repository_kind === "git" ? workspace.branch ?? "Git" : "文件夹"}</small>
                </span>
              </button>
            ))}
            {!loading && workspaces.length === 0 && <div className="no-tasks">未启用或尚未添加</div>}
          </nav>

          <div className="nav-label">对话</div>
          <nav className="task-list">
            {chats.map((chat) => (
              <button
                key={chat.chat_id}
                className={`task-nav ${selectedChatId === chat.chat_id ? "active" : ""}`}
                onClick={() => selectChat(chat.chat_id)}
              >
                <span className="chat-state-icon">◇</span>
                <span className="task-nav-copy">
                  <strong>{chat.title}</strong>
                  <small>{formatTime(chat.updated_at)}</small>
                </span>
              </button>
            ))}
            {!loading && chats.length === 0 && <div className="no-tasks">还没有对话</div>}
          </nav>

          <div className="nav-label">自动化任务</div>
          <nav className="task-list">
            {tasks.map((task) => (
              <button
                key={task.task_id}
                className={`task-nav ${selectedId === task.task_id ? "active" : ""}`}
                onClick={() => selectTask(task.task_id)}
              >
                <span className={`task-state-dot dot-${task.status.toLowerCase()}`} />
                <span className="task-nav-copy">
                  <strong>{task.task_name}</strong>
                  <small>{formatTime(task.created_at)}</small>
                </span>
              </button>
            ))}
            {!loading && tasks.length === 0 && <div className="no-tasks">还没有任务</div>}
          </nav>
        </div>
        <div className="sidebar-footer">
          <button
            className="theme-toggle"
            type="button"
            aria-label={theme === "dark" ? "切换到浅色界面" : "切换到深色界面"}
            onClick={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
          >
            <span aria-hidden="true">{theme === "dark" ? "☀" : "☾"}</span>
            {theme === "dark" ? "浅色界面" : "深色界面"}
          </button>
          <div className="sidebar-status">
            <div>
              <span className="pulse" />
              服务在线
            </div>
            <small>
              {stats.running} 个执行中 · {stats.waiting} 个等待中
            </small>
          </div>
        </div>
      </aside>

      <section className="content">
        {error && (
          <div className="global-error">
            <span>{error}</span>
            <button onClick={() => setError("")}>×</button>
          </div>
        )}
        {selectedWorkspace ? (
          <CodeWorkspace
            workspace={selectedWorkspace}
            onCreateChat={createChat}
            onDelete={deleteWorkspace}
          />
        ) : selectedChat ? (
          <ChatPanel
            chat={selectedChat}
            tasks={tasks}
            codeRuns={codeRuns}
            desktopRuns={desktopRuns}
            sending={sendingChat}
            onSend={sendChat}
            onOpenTask={selectTask}
            onOpenWorkspace={selectWorkspace}
            onTerminateDesktopRun={terminateDesktopRun}
            onDesktopRunChanged={(run) =>
              setDesktopRuns((current) =>
                current.map((item) =>
                  item.desktop_run_id === run.desktop_run_id ? run : item,
                ),
              )
            }
          />
        ) : selected ? (
          <TaskDetail
            task={selected}
            events={events}
            actions={actions}
            approvals={approvals}
            busy={busy}
            onCommand={command}
            onRefresh={() => void loadDetail(selected.task_id)}
          />
        ) : (
          <EmptyState onChat={() => void createChat()} onCreate={() => setShowCreate(true)} />
        )}
      </section>

      {showCreate && (
        <CreateTask
          onClose={() => setShowCreate(false)}
          onCreated={(task) => {
            setTasks((current) => [task, ...current]);
            setSelectedChatId(undefined);
            setSelectedChat(undefined);
            setSelectedId(task.task_id);
            setSelected(task);
            setShowCreate(false);
          }}
        />
      )}
      {showWorkspaceCreate && (
        <WorkspaceModal
          onClose={() => setShowWorkspaceCreate(false)}
          onCreated={(workspace) => {
            setWorkspaces((current) => [workspace, ...current]);
            setShowWorkspaceCreate(false);
            selectWorkspace(workspace.workspace_id);
          }}
        />
      )}
    </div>
  );
}

