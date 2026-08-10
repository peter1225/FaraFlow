import { useCallback, useEffect, useMemo, useState } from "react";

import { api, eventWebSocketUrl } from "./api";
import type {
  CodeDiff,
  CodeRun,
  SessionEvent,
  Workspace,
  WorkspaceFile,
  WorkspaceTreeEntry,
} from "./types";

const STATUS_TEXT: Record<CodeRun["status"], string> = {
  CREATED: "待执行",
  RUNNING: "执行中",
  REVIEW_REQUIRED: "等待审核",
  APPLIED: "已应用",
  DISCARDED: "已丢弃",
  REVERTED: "已撤销",
  FAILED: "失败",
  INTERRUPTED: "已中断",
};

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

export function CodeWorkspace({
  workspace,
  onCreateChat,
  onDelete,
}: {
  workspace: Workspace;
  onCreateChat: (workspaceId: string) => Promise<void>;
  onDelete: (workspaceId: string) => Promise<void>;
}) {
  const [path, setPath] = useState("");
  const [tree, setTree] = useState<WorkspaceTreeEntry[]>([]);
  const [file, setFile] = useState<WorkspaceFile>();
  const [runs, setRuns] = useState<CodeRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [diff, setDiff] = useState<CodeDiff>();
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selectedRun = useMemo(
    () => runs.find((run) => run.code_run_id === selectedRunId),
    [runs, selectedRunId],
  );

  const loadRuns = useCallback(async () => {
    const rows = await api.listCodeRuns(workspace.workspace_id);
    setRuns(rows);
    setSelectedRunId((current) => current ?? rows[0]?.code_run_id);
  }, [workspace.workspace_id]);

  const loadTree = useCallback(async () => {
    setTree(await api.workspaceTree(workspace.workspace_id, path));
  }, [path, workspace.workspace_id]);

  useEffect(() => {
    setPath("");
    setFile(undefined);
    setSelectedRunId(undefined);
    void loadRuns().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "加载代码工作区失败"),
    );
  }, [loadRuns, workspace.workspace_id]);

  useEffect(() => {
    void loadTree().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "加载目录失败"),
    );
  }, [loadTree]);

  const refreshRun = useCallback(async () => {
    if (!selectedRunId) {
      setDiff(undefined);
      setEvents([]);
      return;
    }
    const [run, patch, eventRows] = await Promise.all([
      api.getCodeRun(selectedRunId),
      api.codeRunDiff(selectedRunId),
      api.codeRunEvents(selectedRunId),
    ]);
    setRuns((current) => [run, ...current.filter((item) => item.code_run_id !== run.code_run_id)]);
    setDiff(patch);
    setEvents(eventRows);
  }, [selectedRunId]);

  useEffect(() => {
    void refreshRun().catch((reason: unknown) =>
      setError(reason instanceof Error ? reason.message : "加载代码运行失败"),
    );
    if (!selectedRun || !["CREATED", "RUNNING"].includes(selectedRun.status)) return;
    const socket = new WebSocket(eventWebSocketUrl(selectedRun.session_id));
    socket.onmessage = () => void refreshRun();
    const timer = window.setInterval(() => void refreshRun(), 3000);
    return () => {
      socket.close();
      window.clearInterval(timer);
    };
  }, [refreshRun, selectedRun?.session_id, selectedRun?.status]);

  async function openEntry(entry: WorkspaceTreeEntry) {
    setError("");
    if (entry.kind === "directory") {
      setPath(entry.path);
      setFile(undefined);
      return;
    }
    try {
      setFile(await api.workspaceFile(workspace.workspace_id, entry.path));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取文件失败");
    }
  }

  async function runAction(action: "apply" | "discard" | "revert") {
    if (!selectedRun) return;
    setBusy(true);
    setError("");
    try {
      if (action === "apply") await api.applyCodeRun(selectedRun.code_run_id);
      if (action === "discard") await api.discardCodeRun(selectedRun.code_run_id);
      if (action === "revert") await api.revertCodeRun(selectedRun.code_run_id);
      await Promise.all([loadRuns(), refreshRun(), loadTree()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="code-workspace-page">
      <header className="workspace-header">
        <div>
          <div className="breadcrumb">WORKSPACE <span>/</span> {workspace.repository_kind}</div>
          <h1>{workspace.name}</h1>
          <p title={workspace.root_path}>{workspace.root_path}</p>
        </div>
        <div className="workspace-header-actions">
          <span className={`repo-badge ${workspace.is_dirty ? "dirty" : ""}`}>
            {workspace.repository_kind === "git"
              ? `${workspace.branch ?? "detached"}${workspace.is_dirty ? " · 有未提交修改" : " · 干净"}`
              : "普通文件夹"}
          </span>
          <button className="button primary" onClick={() => void onCreateChat(workspace.workspace_id)}>
            在此工作区新建对话
          </button>
          <button className="button secondary danger-text" onClick={() => void onDelete(workspace.workspace_id)}>
            取消注册
          </button>
        </div>
      </header>

      {error && <div className="workspace-error">{error}</div>}
      <section className="code-grid">
        <aside className="file-explorer panel-card">
          <div className="panel-title"><span>文件</span><small>{path || "/"}</small></div>
          {path && <button className="file-row directory" onClick={() => setPath(parentPath(path))}>↰ ..</button>}
          <div className="file-list">
            {tree.map((entry) => (
              <button className={`file-row ${entry.kind}`} key={entry.path} onClick={() => void openEntry(entry)}>
                <span>{entry.kind === "directory" ? "▸" : "·"}</span>{entry.name}
              </button>
            ))}
            {tree.length === 0 && <div className="panel-empty">目录为空或内容已被安全策略隐藏</div>}
          </div>
        </aside>

        <section className="code-timeline panel-card">
          <div className="panel-title"><span>代码运行</span><small>{runs.length} RUNS</small></div>
          <div className="run-tabs">
            {runs.map((run) => (
              <button
                className={run.code_run_id === selectedRunId ? "active" : ""}
                key={run.code_run_id}
                onClick={() => setSelectedRunId(run.code_run_id)}
              >
                <span className={`run-dot state-${run.status.toLowerCase()}`} />
                <span><strong>{run.instruction}</strong><small>{STATUS_TEXT[run.status]}</small></span>
              </button>
            ))}
            {runs.length === 0 && <div className="panel-empty">在绑定此工作区的对话中选择“代码”模式开始。</div>}
          </div>
          {selectedRun && (
            <div className="tool-timeline">
              <div className="run-summary">
                <span className={`code-status state-${selectedRun.status.toLowerCase()}`}>{STATUS_TEXT[selectedRun.status]}</span>
                <p>{selectedRun.final_summary ?? selectedRun.instruction}</p>
              </div>
              {selectedRun.tool_calls.map((call) => (
                <article className="tool-step" key={call.tool_call_id}>
                  <span>{call.step_no}</span>
                  <div>
                    <strong>{call.tool_name}</strong>
                    <small>{call.status}{call.affected_paths.length ? ` · ${call.affected_paths.join(", ")}` : ""}</small>
                    {call.result_excerpt && <p>{call.result_excerpt}</p>}
                  </div>
                </article>
              ))}
              {events.length > 0 && <div className="event-count">已记录 {events.length} 条运行事件</div>}
            </div>
          )}
        </section>

        <section className="code-review panel-card">
          <div className="panel-title">
            <span>{diff?.diff ? "统一 Diff" : file ? file.path : "内容与审核"}</span>
            <small>{diff?.changed_paths.length ?? 0} FILES CHANGED</small>
          </div>
          {selectedRun?.status === "REVIEW_REQUIRED" && (
            <div className="review-actions">
              <button className="button primary" disabled={busy} onClick={() => void runAction("apply")}>应用修改</button>
              <button className="button secondary" disabled={busy} onClick={() => void runAction("discard")}>丢弃修改</button>
            </div>
          )}
          {selectedRun?.status === "APPLIED" && (
            <div className="review-actions">
              <button className="button secondary" disabled={busy} onClick={() => void runAction("revert")}>撤销本次应用</button>
            </div>
          )}
          {selectedRun &&
            ["CREATED", "RUNNING", "FAILED", "INTERRUPTED"].includes(selectedRun.status) && (
              <div className="review-actions">
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={() => void runAction("discard")}
                >
                  {selectedRun.status === "RUNNING" ? "停止并丢弃" : "丢弃运行"}
                </button>
              </div>
            )}
          <pre className={`code-view ${diff?.diff ? "diff-view" : ""}`}>
            {diff?.diff || file?.content || "选择文件查看内容，或选择一次代码运行查看修改。"}
          </pre>
        </section>
      </section>
    </main>
  );
}
