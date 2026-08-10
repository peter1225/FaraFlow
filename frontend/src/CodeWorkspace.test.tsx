import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CodeWorkspace } from "./CodeWorkspace";
import type { CodeRun, Workspace } from "./types";
import { WorkspaceModal } from "./WorkspaceModal";

const apiMock = vi.hoisted(() => ({
  applyCodeRun: vi.fn(),
  codeRunDiff: vi.fn(),
  codeRunEvents: vi.fn(),
  createWorkspace: vi.fn(),
  discardCodeRun: vi.fn(),
  getCodeRun: vi.fn(),
  listCodeRuns: vi.fn(),
  pickWorkspaceDirectory: vi.fn(),
  revertCodeRun: vi.fn(),
  workspaceFile: vi.fn(),
  workspaceTree: vi.fn(),
}));

vi.mock("./api", () => ({
  api: apiMock,
  eventWebSocketUrl: (sessionId: string) => `ws://localhost/${sessionId}`,
}));

const workspace: Workspace = {
  workspace_id: "ws_test",
  name: "Demo",
  root_path: "C:\\code\\demo",
  repository_kind: "git",
  git_root: "C:\\code\\demo",
  branch: "develop",
  is_dirty: false,
  created_at: "2026-08-08T00:00:00Z",
  updated_at: "2026-08-08T00:00:00Z",
};

const reviewRun: CodeRun = {
  code_run_id: "code_test",
  workspace_id: workspace.workspace_id,
  session_id: "sess_test",
  instruction: "Update README",
  status: "REVIEW_REQUIRED",
  final_summary: "README updated",
  changed_paths: ["README.md"],
  created_at: "2026-08-08T00:00:00Z",
  tool_calls: [
    {
      tool_call_id: "tool_test",
      step_no: 1,
      tool_name: "patch_file",
      status: "ok",
      affected_paths: ["README.md"],
      diff_summary: ["modified:README.md"],
      before_hashes: { "README.md": "before" },
      after_hashes: { "README.md": "after" },
      unified_diff: "--- a/README.md\n+++ b/README.md\n",
      result_excerpt: "modified:README.md",
      created_at: "2026-08-08T00:00:01Z",
    },
  ],
};

describe("local code workspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.workspaceTree.mockResolvedValue([
      { path: "README.md", name: "README.md", kind: "file", size: 12 },
    ]);
    apiMock.workspaceFile.mockResolvedValue({
      path: "README.md",
      content: "# Demo\n",
      start_line: 1,
      end_line: 1,
      total_lines: 1,
    });
    apiMock.codeRunEvents.mockResolvedValue([]);
  });

  afterEach(cleanup);

  it("loads a safe workspace file into the read-only viewer", async () => {
    apiMock.listCodeRuns.mockResolvedValue([]);
    render(
      <CodeWorkspace
        workspace={workspace}
        onCreateChat={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /README\.md/ }));
    await waitFor(() => expect(apiMock.workspaceFile).toHaveBeenCalledWith("ws_test", "README.md"));
    expect(await screen.findByText("# Demo", { exact: false })).toBeInTheDocument();
  });

  it("shows review actions and applies only after user confirmation", async () => {
    apiMock.listCodeRuns.mockResolvedValue([reviewRun]);
    apiMock.getCodeRun.mockResolvedValue(reviewRun);
    apiMock.codeRunDiff.mockResolvedValue({
      code_run_id: reviewRun.code_run_id,
      status: "REVIEW_REQUIRED",
      changed_paths: ["README.md"],
      diff: "-# Old\n+# Demo\n",
    });
    apiMock.applyCodeRun.mockResolvedValue({ ...reviewRun, status: "APPLIED" });
    render(
      <CodeWorkspace
        workspace={workspace}
        onCreateChat={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "应用修改" }));
    await waitFor(() => expect(apiMock.applyCodeRun).toHaveBeenCalledWith("code_test"));
    expect(screen.getByRole("button", { name: "丢弃修改" })).toBeInTheDocument();
  });

  it("registers an absolute local folder from the modal", async () => {
    apiMock.createWorkspace.mockResolvedValue(workspace);
    const onCreated = vi.fn();
    render(<WorkspaceModal onCreated={onCreated} onClose={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("工作区名称"), { target: { value: "Demo" } });
    fireEvent.change(screen.getByLabelText("本机绝对路径"), {
      target: { value: "C:\\code\\demo" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加工作区" }));

    await waitFor(() =>
      expect(apiMock.createWorkspace).toHaveBeenCalledWith({
        name: "Demo",
        root_path: "C:\\code\\demo",
      }),
    );
    expect(onCreated).toHaveBeenCalledWith(workspace);
  });

  it("fills the workspace fields from the native folder picker", async () => {
    apiMock.pickWorkspaceDirectory.mockResolvedValue({
      path: "C:\\code\\demo",
      name: "demo",
    });
    render(<WorkspaceModal onCreated={vi.fn()} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "选择文件夹" }));

    await waitFor(() => expect(apiMock.pickWorkspaceDirectory).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("工作区名称")).toHaveValue("demo");
    expect(screen.getByLabelText("本机绝对路径")).toHaveValue("C:\\code\\demo");
  });
});
