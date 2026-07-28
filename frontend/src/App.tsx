import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { api, artifactUrl, eventWebSocketUrl } from "./api";
import type { Approval, BrowserAction, SessionEvent, SessionState, Task } from "./types";

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
        <div className="brand-caption">AUTOMATION CONTROL</div>
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

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <main className="empty-state">
      <div className="empty-visual" aria-hidden="true">
        <div className="orbit orbit-a" />
        <div className="orbit orbit-b" />
        <div className="core">F</div>
      </div>
      <div className="eyebrow">READY FOR FIRST RUN</div>
      <h1>让浏览器流程变得可控、可恢复、可审计</h1>
      <p>创建一个自然语言任务，FaraFlow 会在隔离浏览器中执行，并在关键操作前停下来等待你。</p>
      <button className="button primary large" onClick={onCreate}>
        创建第一个任务 <span>↗</span>
      </button>
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
  const [selectedId, setSelectedId] = useState<string>();
  const [selected, setSelected] = useState<Task>();
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [actions, setActions] = useState<BrowserAction[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadTasks = useCallback(async () => {
    try {
      const rows = await api.listTasks();
      setTasks(rows);
      if (!selectedId && rows.length > 0) setSelectedId(rows[0].task_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载任务");
    } finally {
      setLoading(false);
    }
  }, [selectedId]);

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
    void loadTasks();
  }, [loadTasks]);

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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Logo />
        <button className="new-task" onClick={() => setShowCreate(true)}>
          <span>＋</span> 新建自动化
        </button>
        <div className="nav-label">任务会话</div>
        <nav className="task-list">
          {tasks.map((task) => (
            <button
              key={task.task_id}
              className={`task-nav ${selectedId === task.task_id ? "active" : ""}`}
              onClick={() => setSelectedId(task.task_id)}
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
        <div className="sidebar-status">
          <div>
            <span className="pulse" />
            CONTROL PLANE ONLINE
          </div>
          <small>
            {stats.running} running · {stats.waiting} waiting
          </small>
        </div>
      </aside>

      <section className="content">
        {error && (
          <div className="global-error">
            <span>{error}</span>
            <button onClick={() => setError("")}>×</button>
          </div>
        )}
        {selected ? (
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
          <EmptyState onCreate={() => setShowCreate(true)} />
        )}
      </section>

      {showCreate && (
        <CreateTask
          onClose={() => setShowCreate(false)}
          onCreated={(task) => {
            setTasks((current) => [task, ...current]);
            setSelectedId(task.task_id);
            setSelected(task);
            setShowCreate(false);
          }}
        />
      )}
    </div>
  );
}

