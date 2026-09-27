import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from faraflow.domain.enums import CodeRunStatus
from faraflow.domain.schemas import (
    CodeDiffView,
    CodeRunApply,
    CodeRunCreate,
    CodeRunView,
    CodeTurnCreate,
    CodeTurnView,
    ToolCallView,
)
from faraflow.infra.async_utils import run_sync
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
        self._workspace_locks: Dict[str, asyncio.Lock] = {}
        self._run_locks: Dict[str, asyncio.Lock] = {}

    async def create(self, request: CodeRunCreate) -> CodeRunView:
        self.workspaces.assert_enabled()
        record = await self.repository.create_code_run(
            workspace_id=request.workspace_id,
            instruction=request.instruction,
            chat_id=request.chat_id,
            engine=self.runtime.settings.code_engine,
            model=self.runtime.settings.code_model,
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
        turns = await self.repository.list_code_turns(code_run_id)
        return self._view(record, calls, turns)

    async def list(self, workspace_id: Optional[str] = None) -> List[CodeRunView]:
        records = await self.repository.list_code_runs(workspace_id)
        return [await self.get(record.code_run_id) for record in records]

    async def continue_run(
        self, code_run_id: str, request: CodeTurnCreate
    ) -> CodeRunView:
        lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with lock:
            return await self._continue_locked(code_run_id, request)

    async def _continue_locked(
        self, code_run_id: str, request: CodeTurnCreate
    ) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if (
            record.engine != self.runtime.settings.code_engine
            or record.model != self.runtime.settings.code_model
        ):
            raise ConflictError(
                "code run was created with a different engine/model; start a new task"
            )
        turn = await self.repository.create_code_turn(code_run_id, request.instruction)
        await self.runtime.emit(
            record.session_id,
            "code.turn.queued",
            f"第 {turn.ordinal} 轮代码协作已排队",
            {
                "code_run_id": code_run_id,
                "turn_id": turn.turn_id,
                "ordinal": turn.ordinal,
            },
        )
        if request.auto_start:
            await self.runtime.start(code_run_id)
        return await self.get(code_run_id)

    async def continue_chat_run(
        self, chat_id: str, instruction: str, *, auto_start: bool = True
    ) -> Optional[CodeRunView]:
        record = await self.repository.get_latest_open_code_run(chat_id)
        if record is None:
            return None
        return await self.continue_run(
            record.code_run_id,
            CodeTurnCreate(instruction=instruction, auto_start=auto_start),
        )

    async def pause(self, code_run_id: str) -> CodeRunView:
        lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with lock:
            record = await self.repository.get_code_run(code_run_id)
            if CodeRunStatus(record.status) not in {
                CodeRunStatus.CREATED,
                CodeRunStatus.RUNNING,
            }:
                raise ConflictError("code run is not currently running")
            await self.runtime.pause(code_run_id)
            return await self.get(code_run_id)

    async def diff(self, code_run_id: str) -> CodeDiffView:
        record = await self.repository.get_code_run(code_run_id)
        path = (
            self.run_store.review_diff_path(code_run_id, record.review_revision)
            if record.review_revision
            else self.run_store.run_dir(code_run_id) / "diff.patch"
        )
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        return CodeDiffView(
            code_run_id=code_run_id,
            status=CodeRunStatus(record.status),
            review_revision=record.review_revision,
            changed_paths=list(record.changed_paths or []),
            diff=content,
        )

    async def apply(self, code_run_id: str, request: CodeRunApply) -> CodeRunView:
        run_lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with run_lock:
            return await self._apply_current(code_run_id, request)

    async def _apply_current(
        self, code_run_id: str, request: CodeRunApply
    ) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) not in {
            CodeRunStatus.REVIEW_REQUIRED,
            CodeRunStatus.PAUSED,
            CodeRunStatus.FAILED,
        }:
            raise ConflictError("code run is not waiting for review")
        if record.review_revision != request.review_revision:
            raise ConflictError("review revision is stale; reload the latest diff")
        review = await self.repository.get_code_review(
            code_run_id, request.review_revision
        )
        workspace = await self.repository.get_workspace(record.workspace_id)
        lock = self._workspace_locks.setdefault(record.workspace_id, asyncio.Lock())
        async with lock:
            return await self._apply_locked(record, review, workspace)

    async def _apply_locked(self, record: Any, review: Any, workspace: Any) -> CodeRunView:
        code_run_id = record.code_run_id
        latest = await self.repository.get_code_run(code_run_id)
        if (
            latest.review_revision != review.revision
            or CodeRunStatus(latest.status)
            not in {
                CodeRunStatus.REVIEW_REQUIRED,
                CodeRunStatus.PAUSED,
                CodeRunStatus.FAILED,
            }
        ):
            raise ConflictError("review revision changed before apply")
        if await self.repository.next_queued_code_turn(code_run_id) is not None:
            raise ConflictError("code run has a queued turn; wait for the latest review")
        original_root = Path(workspace.root_path)

        targets: Dict[str, Path] = {}
        for relative in review.changed_paths:
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            expected = (record.baseline_manifest or {}).get(relative)
            if file_hash(target) != expected:
                raise ConflictError(f"workspace file changed since review started: {relative}")
            source = self.run_store.review_path(code_run_id, review.revision, relative)
            if file_hash(source) != (review.manifest or {}).get(relative):
                raise ConflictError(f"review snapshot changed after review: {relative}")
            targets[relative] = target

        applied: Dict[str, Dict[str, Any]] = {}
        backed_up: List[str] = []
        try:
            for relative, target in targets.items():
                self.run_store.preserve_apply_backup(code_run_id, relative, target)
                backed_up.append(relative)
            for relative, target in targets.items():
                source = self.run_store.review_path(code_run_id, review.revision, relative)
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
        run_lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with run_lock:
            return await self._revert_current(code_run_id)

    async def _revert_current(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) != CodeRunStatus.APPLIED or not record.applied_manifest:
            raise ConflictError("code run has no applied changes to revert")
        workspace = await self.repository.get_workspace(record.workspace_id)
        lock = self._workspace_locks.setdefault(record.workspace_id, asyncio.Lock())
        async with lock:
            return await self._revert_locked(record, workspace)

    async def _revert_locked(self, record: Any, workspace: Any) -> CodeRunView:
        code_run_id = record.code_run_id
        latest = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(latest.status) != CodeRunStatus.APPLIED:
            raise ConflictError("code run is no longer applied")
        original_root = Path(workspace.root_path)
        for relative, metadata in latest.applied_manifest.items():
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            if file_hash(target) != metadata.get("after_hash"):
                raise ConflictError(f"workspace file changed after apply: {relative}")
        self._restore_backups(code_run_id, original_root, latest.applied_manifest.keys())
        record = await self.repository.update_code_run(code_run_id, status=CodeRunStatus.REVERTED)
        await self.runtime.emit(
            record.session_id,
            "code.run.reverted",
            "代码修改已撤销",
            {"code_run_id": code_run_id, "changed_paths": record.changed_paths},
        )
        return await self.get(code_run_id)

    async def discard(self, code_run_id: str) -> CodeRunView:
        lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with lock:
            return await self._discard_locked(code_run_id)

    async def _discard_locked(self, code_run_id: str) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        state = CodeRunStatus(record.status)
        if state in {CodeRunStatus.APPLIED, CodeRunStatus.REVERTED, CodeRunStatus.DISCARDED}:
            raise ConflictError(f"cannot discard code run in state {state.value}")
        await self.runtime.cancel(code_run_id)
        # Isolation may have been persisted while cancellation drained an in-flight operation.
        record = await self.repository.get_code_run(code_run_id)
        workspace = await self.repository.get_workspace(record.workspace_id, active_only=False)
        await self._cleanup(record, workspace)
        record = await self.repository.update_code_run(code_run_id, status=CodeRunStatus.DISCARDED)
        await self.runtime.emit(
            record.session_id,
            "code.run.discarded",
            "代码运行已丢弃",
            {"code_run_id": code_run_id},
        )
        return await self.get(code_run_id)

    async def _cleanup(self, record: Any, workspace: Any) -> None:
        if record.isolated_path:
            await run_sync(self.workspaces.cleanup_isolation, record, workspace)

    def _restore_backups(self, code_run_id: str, original_root: Path, relative_paths: Any) -> None:
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
    def _view(record: Any, calls: List[Any], turns: List[Any]) -> CodeRunView:
        return CodeRunView(
            code_run_id=record.code_run_id,
            workspace_id=record.workspace_id,
            chat_id=record.chat_id,
            session_id=record.session_id,
            instruction=record.instruction,
            status=CodeRunStatus(record.status),
            engine=record.engine,
            model=record.model,
            isolation_kind=record.isolation_kind,
            base_revision=record.base_revision,
            final_summary=record.final_summary,
            changed_paths=list(record.changed_paths or []),
            diff_ref=record.diff_ref,
            review_revision=record.review_revision,
            error=record.error,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            tool_calls=[
                ToolCallView(
                    tool_call_id=item.tool_call_id,
                    step_no=item.step_no,
                    tool_name=item.tool_name,
                    turn_id=item.turn_id,
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
            turns=[
                CodeTurnView(
                    turn_id=item.turn_id,
                    ordinal=item.ordinal,
                    instruction=item.instruction,
                    status=item.status,
                    summary=item.summary,
                    error=item.error,
                    created_at=item.created_at,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                )
                for item in turns
            ],
        )
