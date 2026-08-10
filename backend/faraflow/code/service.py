import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from faraflow.domain.enums import CodeRunStatus
from faraflow.domain.schemas import (
    CodeDiffView,
    CodeRunCreate,
    CodeRunView,
    ToolCallView,
)
from faraflow.infra.repository import ConflictError, Repository
from faraflow.workspace.run_store import CodeRunStore, file_hash
from faraflow.workspace.service import WorkspaceService

from .runtime import CodeRuntime


class CodeRunService:
    def __init__(
        self,
        repository: Repository,
        workspaces: WorkspaceService,
        run_store: CodeRunStore,
        runtime: CodeRuntime,
    ) -> None:
        self.repository = repository
        self.workspaces = workspaces
        self.run_store = run_store
        self.runtime = runtime

    async def create(self, request: CodeRunCreate) -> CodeRunView:
        self.workspaces.assert_enabled()
        record = await self.repository.create_code_run(
            workspace_id=request.workspace_id,
            instruction=request.instruction,
            chat_id=request.chat_id,
        )
        await self.runtime.emit(
            record.session_id,
            "code.run.created",
            "代码任务已创建",
            {"code_run_id": record.code_run_id, "workspace_id": record.workspace_id},
        )
        if request.auto_start:
            await self.runtime.start(record.code_run_id)
        return await self.get(record.code_run_id)

    async def get(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        calls = await self.repository.list_tool_calls(code_run_id)
        return self._view(record, calls)

    async def list(self, workspace_id: Optional[str] = None) -> List[CodeRunView]:
        records = await self.repository.list_code_runs(workspace_id)
        return [await self.get(record.code_run_id) for record in records]

    async def diff(self, code_run_id: str) -> CodeDiffView:
        record = await self.repository.get_code_run(code_run_id)
        path = self.run_store.run_dir(code_run_id) / "diff.patch"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return CodeDiffView(
            code_run_id=code_run_id,
            status=CodeRunStatus(record.status),
            changed_paths=list(record.changed_paths or []),
            diff=content,
        )

    async def apply(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) != CodeRunStatus.REVIEW_REQUIRED:
            raise ConflictError("code run is not waiting for review")
        workspace = await self.repository.get_workspace(record.workspace_id)
        original_root = Path(workspace.root_path)
        isolated_root = Path(record.isolated_path or "")
        if not await asyncio.to_thread(isolated_root.is_dir):
            raise ConflictError("code run isolation directory is unavailable")

        targets: Dict[str, Path] = {}
        for relative in record.changed_paths:
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            expected = (record.baseline_manifest or {}).get(relative)
            if file_hash(target) != expected:
                raise ConflictError(f"workspace file changed since review started: {relative}")
            targets[relative] = target

        applied: Dict[str, Dict[str, Any]] = {}
        backed_up: List[str] = []
        try:
            for relative, target in targets.items():
                self.run_store.preserve_apply_backup(code_run_id, relative, target)
                backed_up.append(relative)
            for relative, target in targets.items():
                source = self.workspaces.safe_path(isolated_root, relative, allow_missing=True)
                before_hash = file_hash(target)
                if source.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                elif target.exists():
                    target.unlink()
                applied[relative] = {
                    "before_hash": before_hash,
                    "after_hash": file_hash(target),
                    "existed_before": before_hash is not None,
                }
        except Exception:
            self._restore_backups(code_run_id, original_root, backed_up)
            raise

        record = await self.repository.update_code_run(
            code_run_id,
            status=CodeRunStatus.APPLIED,
            applied_manifest=applied,
        )
        self.run_store.save_manifest(code_run_id, "applied-manifest", applied)
        await self.runtime.emit(
            record.session_id,
            "code.run.applied",
            "代码修改已应用到原工作区",
            {"code_run_id": code_run_id, "changed_paths": record.changed_paths},
        )
        await self._cleanup(record, workspace)
        return await self.get(code_run_id)

    async def revert(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) != CodeRunStatus.APPLIED or not record.applied_manifest:
            raise ConflictError("code run has no applied changes to revert")
        workspace = await self.repository.get_workspace(record.workspace_id)
        original_root = Path(workspace.root_path)
        for relative, metadata in record.applied_manifest.items():
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            if file_hash(target) != metadata.get("after_hash"):
                raise ConflictError(f"workspace file changed after apply: {relative}")
        self._restore_backups(code_run_id, original_root, record.applied_manifest.keys())
        record = await self.repository.update_code_run(
            code_run_id, status=CodeRunStatus.REVERTED
        )
        await self.runtime.emit(
            record.session_id,
            "code.run.reverted",
            "代码修改已撤销",
            {"code_run_id": code_run_id, "changed_paths": record.changed_paths},
        )
        return await self.get(code_run_id)

    async def discard(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        state = CodeRunStatus(record.status)
        if state in {CodeRunStatus.APPLIED, CodeRunStatus.REVERTED, CodeRunStatus.DISCARDED}:
            raise ConflictError(f"cannot discard code run in state {state.value}")
        await self.runtime.cancel(code_run_id)
        workspace = await self.repository.get_workspace(
            record.workspace_id, active_only=False
        )
        await self._cleanup(record, workspace)
        record = await self.repository.update_code_run(
            code_run_id, status=CodeRunStatus.DISCARDED
        )
        await self.runtime.emit(
            record.session_id,
            "code.run.discarded",
            "代码运行已丢弃",
            {"code_run_id": code_run_id},
        )
        return await self.get(code_run_id)

    async def _cleanup(self, record: Any, workspace: Any) -> None:
        if record.isolated_path:
            await asyncio.to_thread(
                self.workspaces.cleanup_isolation, record, workspace
            )

    def _restore_backups(
        self, code_run_id: str, original_root: Path, relative_paths: Any
    ) -> None:
        for relative in relative_paths:
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            backup = self.run_store.backup_bytes(code_run_id, relative)
            if backup is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup)

    @staticmethod
    def _view(record: Any, calls: List[Any]) -> CodeRunView:
        return CodeRunView(
            code_run_id=record.code_run_id,
            workspace_id=record.workspace_id,
            chat_id=record.chat_id,
            session_id=record.session_id,
            instruction=record.instruction,
            status=CodeRunStatus(record.status),
            isolation_kind=record.isolation_kind,
            base_revision=record.base_revision,
            final_summary=record.final_summary,
            changed_paths=list(record.changed_paths or []),
            diff_ref=record.diff_ref,
            error=record.error,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            tool_calls=[
                ToolCallView(
                    tool_call_id=item.tool_call_id,
                    step_no=item.step_no,
                    tool_name=item.tool_name,
                    status=item.status,
                    affected_paths=list(item.affected_paths or []),
                    diff_summary=list(item.diff_summary or []),
                    before_hashes=dict(item.before_hashes or {}),
                    after_hashes=dict(item.after_hashes or {}),
                    unified_diff=item.unified_diff or "",
                    result_excerpt=item.result_excerpt,
                    created_at=item.created_at,
                )
                for item in calls
            ],
        )
