import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from faraflow.domain.enums import (
    CodeApplyStatus,
    CodeRunStatus,
    CodeToolPhase,
    CodeVerificationStatus,
)
from faraflow.domain.schemas import (
    CodeDiffView,
    CodeRecoveryView,
    CodeRunApply,
    CodeRunCreate,
    CodeRunResume,
    CodeRunView,
    CodeTurnCreate,
    CodeTurnView,
    CodeVerificationProfileView,
    CodeVerificationView,
    ToolCallView,
)
from faraflow.infra.async_utils import run_sync
from faraflow.infra.repository import ConflictError, Repository
from faraflow.workspace.durable import DurableFileWriter
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
        self._mutation_owner = f"code-service-{uuid.uuid4().hex}"
        self._verification_owner = f"verification-service-{uuid.uuid4().hex}"

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
            token = await self.repository.acquire_workspace_mutation_lease(
                record.workspace_id,
                self._mutation_owner,
                self.runtime.settings.code_lease_seconds,
            )
            if token is None:
                raise ConflictError("workspace is being mutated by another process")
            try:
                return await self._apply_locked(record, review, workspace, token)
            finally:
                await self.repository.release_workspace_mutation_lease(
                    record.workspace_id, self._mutation_owner, token
                )

    async def _apply_locked(
        self, record: Any, review: Any, workspace: Any, fencing_token: int
    ) -> CodeRunView:
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
        completed: List[str] = []
        backup_hashes: Dict[str, Optional[str]] = {}
        journal = await self.repository.create_apply_journal(
            code_run_id=code_run_id,
            workspace_id=record.workspace_id,
            review_revision=review.revision,
            paths=list(review.changed_paths),
        )
        try:
            for relative, target in targets.items():
                backup_hashes[relative] = self.run_store.preserve_apply_backup(
                    code_run_id, relative, target
                )
            await self.repository.update_apply_journal(
                journal.journal_id, backup_hashes=backup_hashes
            )
            await self.repository.update_apply_journal(
                journal.journal_id, status=CodeApplyStatus.APPLYING
            )
            for relative, target in targets.items():
                renewed = await self.repository.renew_workspace_mutation_lease(
                    record.workspace_id,
                    self._mutation_owner,
                    fencing_token,
                    self.runtime.settings.code_lease_seconds,
                )
                if not renewed:
                    raise ConflictError("workspace mutation lease was lost")
                await self.repository.assert_workspace_mutation_fence(
                    record.workspace_id, self._mutation_owner, fencing_token
                )
                source = self.run_store.review_path(code_run_id, review.revision, relative)
                expected_original = (record.baseline_manifest or {}).get(relative)
                if file_hash(target) != expected_original:
                    raise ConflictError(f"workspace changed while applying: {relative}")
                before_hash = file_hash(target)
                if source.exists():
                    await run_sync(
                        DurableFileWriter.replace_from,
                        source,
                        target,
                        journal.journal_id,
                    )
                elif target.exists():
                    await run_sync(
                        DurableFileWriter.delete, target, journal.journal_id
                    )
                applied[relative] = {
                    "before_hash": before_hash,
                    "after_hash": file_hash(target),
                    "existed_before": before_hash is not None,
                }
                completed.append(relative)
                await self.repository.update_apply_journal(
                    journal.journal_id, completed_paths=list(completed)
                )
        except Exception as exc:
            await self.repository.update_apply_journal(
                journal.journal_id,
                status=CodeApplyStatus.ROLLING_BACK,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            self._restore_backups(
                code_run_id,
                original_root,
                completed,
                operation_id=journal.journal_id,
                expected_hashes=backup_hashes,
            )
            self._remove_apply_temps(
                original_root, journal.journal_id, list(targets)
            )
            await self.repository.update_apply_journal(
                journal.journal_id, status=CodeApplyStatus.ROLLED_BACK
            )
            raise

        record = await self.repository.update_code_run(
            code_run_id,
            status=CodeRunStatus.APPLIED,
            applied_manifest=applied,
        )
        self.run_store.save_manifest(code_run_id, "applied-manifest", applied)
        await self.repository.update_apply_journal(
            journal.journal_id, status=CodeApplyStatus.APPLIED
        )
        await self.runtime.emit(
            record.session_id,
            "code.run.applied",
            "代码修改已应用到原工作区",
            {"code_run_id": code_run_id, "changed_paths": record.changed_paths},
        )
        await self._cleanup(record, workspace)
        return await self.get(code_run_id)

    async def recover_incomplete_applies(self) -> int:
        journals = await self.repository.list_incomplete_apply_journals()
        recovered = 0
        for journal in journals:
            run = await self.repository.get_code_run(journal.code_run_id)
            workspace = await self.repository.get_workspace(
                journal.workspace_id, active_only=False
            )
            lock = self._workspace_locks.setdefault(journal.workspace_id, asyncio.Lock())
            async with lock:
                token = await self.repository.acquire_workspace_mutation_lease(
                    journal.workspace_id,
                    self._mutation_owner,
                    self.runtime.settings.code_lease_seconds,
                )
                if token is None:
                    continue
                try:
                    await self.repository.assert_workspace_mutation_fence(
                        journal.workspace_id, self._mutation_owner, token
                    )
                    await self.repository.update_apply_journal(
                        journal.journal_id, status=CodeApplyStatus.ROLLING_BACK
                    )
                    original_root = Path(workspace.root_path)
                    rollback_paths = (
                        journal.paths
                        if journal.status
                        in {
                            CodeApplyStatus.APPLYING.value,
                            CodeApplyStatus.ROLLING_BACK.value,
                        }
                        else []
                    )
                    self._restore_backups(
                        journal.code_run_id,
                        original_root,
                        rollback_paths,
                        operation_id=journal.journal_id,
                        expected_hashes=journal.backup_hashes or {},
                    )
                    await run_sync(
                        self._remove_apply_temps,
                        original_root,
                        journal.journal_id,
                        journal.paths,
                    )
                    await self.repository.update_apply_journal(
                        journal.journal_id, status=CodeApplyStatus.ROLLED_BACK
                    )
                    await self.repository.update_code_run(
                        run.code_run_id, status=CodeRunStatus.REVIEW_REQUIRED
                    )
                    await self.runtime.emit(
                        run.session_id,
                        "code.apply.recovered",
                        "检测到未完成的应用操作，已从备份回滚",
                        {
                            "code_run_id": run.code_run_id,
                            "review_revision": journal.review_revision,
                        },
                    )
                    recovered += 1
                except Exception as exc:
                    error = {"type": type(exc).__name__, "message": str(exc)}
                    await self.repository.update_apply_journal(
                        journal.journal_id,
                        status=CodeApplyStatus.FAILED,
                        error=error,
                    )
                    await self.repository.update_code_run(
                        run.code_run_id,
                        status=CodeRunStatus.FAILED,
                        error={"reason": "apply_recovery_failed", **error},
                    )
                    await self.runtime.emit(
                        run.session_id,
                        "code.apply.recovery_failed",
                        "未完成的应用操作无法自动回滚，需要人工检查",
                        {
                            "code_run_id": run.code_run_id,
                            "journal_id": journal.journal_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                finally:
                    await self.repository.release_workspace_mutation_lease(
                        journal.workspace_id, self._mutation_owner, token
                    )
        return recovered

    @staticmethod
    def _remove_apply_temps(
        original_root: Path, journal_id: str, relative_paths: List[str]
    ) -> None:
        for relative in relative_paths:
            target = (original_root / Path(relative)).resolve()
            if target != original_root and original_root not in target.parents:
                continue
            DurableFileWriter.cleanup(target, journal_id)

    async def recovery(self, code_run_id: str) -> CodeRecoveryView:
        run = await self.repository.get_code_run(code_run_id)
        incomplete = await self.repository.list_incomplete_tool_calls(code_run_id)
        queued = [
            turn
            for turn in await self.repository.list_code_turns(code_run_id)
            if turn.status == "QUEUED"
        ]
        unknown = [
            item.tool_call_id
            for item in incomplete
            if item.phase == CodeToolPhase.UNKNOWN.value
        ]
        recoverable = not unknown and CodeRunStatus(run.status) in {
            CodeRunStatus.INTERRUPTED,
            CodeRunStatus.PAUSED,
            CodeRunStatus.FAILED,
            CodeRunStatus.CREATED,
        }
        reason = (
            "incomplete tool calls require inspection"
            if unknown
            else "safe to resume from the latest durable tool boundary"
            if recoverable
            else f"code run state {run.status} is not resumable"
        )
        return CodeRecoveryView(
            code_run_id=code_run_id,
            status=CodeRunStatus(run.status),
            recoverable=recoverable,
            reason=reason,
            incomplete_tool_calls=[item.tool_call_id for item in incomplete],
            queued_turns=len(queued),
            review_revision=run.review_revision,
        )

    async def resume(
        self, code_run_id: str, request: CodeRunResume
    ) -> CodeRunView:
        run = await self.repository.get_code_run(code_run_id)
        isolation_available = False
        if run.isolated_path:
            isolation_available = await run_sync(Path(run.isolated_path).is_dir)
        if isolation_available:
            assert run.isolated_path is not None
            await self.runtime.reconcile_incomplete_tools(
                code_run_id, Path(run.isolated_path)
            )
        recovery = await self.recovery(code_run_id)
        if not recovery.recoverable:
            raise ConflictError(recovery.reason)
        if recovery.queued_turns:
            await self.runtime.start(code_run_id)
            return await self.get(code_run_id)
        turns = await self.repository.list_code_turns(code_run_id)
        previous = turns[-1].instruction if turns else run.instruction
        instruction = request.instruction or (
            "Resume the interrupted code task. Inspect the current workspace and continue "
            f"from the last durable tool boundary. Previous request: {previous}"
        )
        return await self.continue_run(
            code_run_id, CodeTurnCreate(instruction=instruction, auto_start=True)
        )

    def verification_profiles(self) -> List[CodeVerificationProfileView]:
        return [
            CodeVerificationProfileView(
                profile_id=profile_id,
                argv=argv,
                timeout_seconds=self.runtime.settings.code_verification_timeout_seconds,
            )
            for profile_id, argv in sorted(self.runtime.verification.profiles().items())
        ]

    async def verify(
        self, code_run_id: str, profile_id: str
    ) -> CodeVerificationView:
        lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with lock:
            run = await self.repository.get_code_run(code_run_id)
            if CodeRunStatus(run.status) not in {
                CodeRunStatus.REVIEW_REQUIRED,
                CodeRunStatus.PAUSED,
                CodeRunStatus.FAILED,
            }:
                raise ConflictError("code run is not ready for verification")
            if not run.isolated_path or not await run_sync(Path(run.isolated_path).is_dir):
                raise ConflictError("code run isolation is unavailable")
            isolated_root = Path(run.isolated_path)
            for relative, expected in dict(run.review_manifest or {}).items():
                path = self.workspaces.safe_path(
                    isolated_root, relative, allow_missing=True
                )
                if file_hash(path) != expected:
                    raise ConflictError(
                        f"isolated workspace changed after review: {relative}"
                    )
            token = await self.repository.acquire_code_run_lease(
                code_run_id,
                self._verification_owner,
                self.runtime.settings.code_lease_seconds,
            )
            if token is None:
                raise ConflictError("code run is being executed by another process")
            try:
                verification = await self.runtime.verification.run(
                    code_run_id=code_run_id,
                    turn_id=None,
                    isolated_root=isolated_root,
                    review_revision=run.review_revision,
                    profile_id=profile_id,
                )
            finally:
                await self.repository.release_code_run_lease(
                    code_run_id, self._verification_owner, token
                )
            await self.runtime.emit(
                run.session_id,
                "code.verification.completed",
                f"验证 {profile_id}：{verification.status}",
                {
                    "code_run_id": code_run_id,
                    "verification_id": verification.verification_id,
                    "profile_id": profile_id,
                    "status": verification.status,
                    "exit_code": verification.exit_code,
                    "review_revision": run.review_revision,
                },
            )
            return self._verification_view(verification, run.review_revision)

    async def verifications(self, code_run_id: str) -> List[CodeVerificationView]:
        run = await self.repository.get_code_run(code_run_id)
        records = await self.repository.list_code_verifications(code_run_id)
        return [
            self._verification_view(item, run.review_revision) for item in records
        ]

    @staticmethod
    def _verification_view(record: Any, current_revision: int) -> CodeVerificationView:
        return CodeVerificationView(
            verification_id=record.verification_id,
            code_run_id=record.code_run_id,
            turn_id=record.turn_id,
            review_revision=record.review_revision,
            profile_id=record.profile_id,
            status=CodeVerificationStatus(record.status),
            exit_code=record.exit_code,
            stdout_excerpt=record.stdout_excerpt or "",
            stderr_excerpt=record.stderr_excerpt or "",
            duration_ms=record.duration_ms,
            source_manifest_digest=record.source_manifest_digest,
            stale=record.review_revision != current_revision,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
        )

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
            token = await self.repository.acquire_workspace_mutation_lease(
                record.workspace_id,
                self._mutation_owner,
                self.runtime.settings.code_lease_seconds,
            )
            if token is None:
                raise ConflictError("workspace is being mutated by another process")
            try:
                await self.repository.assert_workspace_mutation_fence(
                    record.workspace_id, self._mutation_owner, token
                )
                return await self._revert_locked(record, workspace)
            finally:
                await self.repository.release_workspace_mutation_lease(
                    record.workspace_id, self._mutation_owner, token
                )

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
        self._restore_backups(
            code_run_id,
            original_root,
            latest.applied_manifest.keys(),
            operation_id=f"revert-{code_run_id}",
        )
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

    def _restore_backups(
        self,
        code_run_id: str,
        original_root: Path,
        relative_paths: Any,
        *,
        operation_id: str,
        expected_hashes: Optional[Mapping[str, Optional[str]]] = None,
    ) -> None:
        for relative in relative_paths:
            target = self.workspaces.safe_path(original_root, relative, allow_missing=True)
            backup = self.run_store.backup_path(code_run_id, relative)
            expected = (expected_hashes or {}).get(relative)
            if backup.exists():
                if (
                    expected_hashes is not None
                    and relative in expected_hashes
                    and file_hash(backup) != expected
                ):
                    raise ConflictError(f"apply backup integrity check failed: {relative}")
                DurableFileWriter.replace_from(backup, target, operation_id)
            else:
                if (
                    expected_hashes is not None
                    and relative in expected_hashes
                    and expected is not None
                ):
                    raise ConflictError(f"apply backup is missing: {relative}")
                DurableFileWriter.delete(target, operation_id)

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
                    external_call_id=item.external_call_id,
                    phase=item.phase,
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
