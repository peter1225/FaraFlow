import { FormEvent, useState } from "react";

import { api } from "./api";
import type { Workspace } from "./types";

export function WorkspaceModal({
  onCreated,
  onClose,
}: {
  onCreated: (workspace: Workspace) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [rootPath, setRootPath] = useState("");
  const [picking, setPicking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function pickDirectory() {
    setPicking(true);
    setError("");
    try {
      const selection = await api.pickWorkspaceDirectory();
      if (!selection.path) return;
      setRootPath(selection.path);
      if (!name.trim() && selection.name) setName(selection.name);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法打开文件夹选择器");
    } finally {
      setPicking(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      onCreated(await api.createWorkspace({ name: name.trim(), root_path: rootPath.trim() }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "添加工作区失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section className="modal workspace-modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-heading">
          <div>
            <div className="eyebrow">LOCAL WORKSPACE</div>
            <h2>添加本机文件夹</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭">×</button>
        </div>
        <form onSubmit={submit}>
          <label>
            工作区名称
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：FaraFlow"
              required
            />
          </label>
          <div className="workspace-folder-field">
            <span>本机文件夹</span>
            <div className="folder-picker-row">
              <input
                aria-label="本机绝对路径"
                value={rootPath}
                onChange={(event) => setRootPath(event.target.value)}
                placeholder="选择文件夹，或输入本机绝对路径"
                required
              />
              <button
                type="button"
                className="button secondary folder-picker-button"
                disabled={picking || saving}
                onClick={() => void pickDirectory()}
              >
                <span aria-hidden="true">▣</span>
                {picking ? "等待选择…" : "选择文件夹"}
              </button>
            </div>
          </div>
          <p className="modal-help">
            修改会先进入隔离副本。原目录只有在你查看 Diff 并点击“应用修改”后才会变化。
          </p>
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="button secondary" onClick={onClose}>取消</button>
            <button className="button primary" disabled={saving || picking}>
              {saving ? "正在验证…" : "添加工作区"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
