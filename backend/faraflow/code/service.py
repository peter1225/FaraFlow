import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

from faraflow.domain.enums import (
    CodeApplyStatus,
    CodeRunStatus,
    CodeToolPhase,
    CodeVerificationStatus,
)
from faraflow.domain.schemas import (
    CodeAgentCreate,
    CodeDiffView,
    CodeRecoveryDecision,
    CodeRecoveryToolView,
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

    async def create_agent(self, parent_code_run_id: str, request: CodeAgentCreate) -> CodeRunView:
        parent = await self.repository.get_code_run(parent_code_run_id)
        if not parent.isolated_path or not await run_sync(Path(parent.isolated_path).is_dir):
            raise ConflictError("start the parent code run before creating agents")
        record = await self.repository.create_code_run(
            workspace_id=parent.workspace_id,
            instruction=request.instruction,
            engine=self.runtime.settings.code_engine,
            model=self.runtime.settings.code_model,
            parent_code_run_id=parent_code_run_id,
            agent_mode=request.mode,
        )
        await self.runtime.emit(
            record.session_id,
            "code.agent.created",
            "只读分析 Agent 已创建" if request.mode == "read_only" else "独立写入 Agent 已创建",
            {
                "code_run_id": record.code_run_id,
                "parent_code_run_id": parent_code_run_id,
                "agent_mode": request.mode,
            },
        )
        if request.auto_start:
            await self.runtime.start(record.code_run_id)
        return await self.get(record.code_run_id)

    async def list_agents(self, parent_code_run_id: str) -> List[CodeRunView]:
        records = await self.repository.list_code_agent_runs(parent_code_run_id)
        return [await self.get(record.code_run_id) for record in records]

    async def continue_run(self, code_run_id: str, request: CodeTurnCreate) -> CodeRunView:
        lock = self._run_locks.setdefault(code_run_id, asyncio.Lock())
        async with lock:
            return await self._continue_locked(code_run_id, request)

    async def _continue_locked(self, code_run_id: str, request: CodeTurnCreate) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if (
            record.engine != self.runtime.settings.code_engine
            or record.model != self.runtime.settings.code_model
        ):
            raise ConflictError(
                "code run was created with a different engine/model; start a new task"
            )
        was_running = CodeRunStatus(record.status) == CodeRunStatus.RUNNING
        if was_running and request.busy_policy == "interrupt":
            await self.runtime.pause(code_run_id)
            record = await self.repository.get_code_run(code_run_id)
            was_running = False
        turn = await self.repository.create_code_turn(code_run_id, request.instruction)
        await self.runtime.emit(
            record.session_id,
            "code.turn.queued",
            f"第 {turn.ordinal} 轮代码协作已排队",
            {
                "code_run_id": code_run_id,
                "turn_id": turn.turn_id,
                "ordinal": turn.ordinal,
                "busy_policy": request.busy_policy,
            },
        )
        if was_running and request.busy_policy == "inject":
            if await self.runtime.inject(code_run_id, turn.turn_id, request.instruction):
                return await self.get(code_run_id)
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

    async def _apply_current(self, code_run_id: str, request: CodeRunApply) -> CodeRunView:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) not in {
            CodeRunStatus.REVIEW_REQUIRED,
            CodeRunStatus.PAUSED,
            CodeRunStatus.FAILED,
        }:
            raise ConflictError("code run is not waiting for review")
        if record.review_revision != request.review_revision:
            raise ConflictError("review revision is stale; reload the latest diff")
        review = await self.repository.get_code_review(code_run_id, request.review_revision)
        if not review.changed_paths or not record.changed_paths:
            raise ConflictError("code run has no current changes to apply")
        async with self._mutation_target(record) as workspace:
            return await self._apply_to_target(record, review, workspace)

    @asynccontextmanager
    async def _mutation_target(self, record: Any) -> AsyncIterator[Any]:
        workspace = await self.repository.get_workspace(record.workspace_id)
        parent_id = record.parent_code_run_id
        if not parent_id:
            yield workspace
            return
        lock = self._run_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            token = await self.repository.acquire_code_run_lease(
                parent_id, self._mutation_owner, self.runtime.settings.code_lease_seconds
            )
            if token is None:
                raise ConflictError("parent code run is currently in use")
            try:
                parent = await self.repository.get_code_run(parent_id)
                if CodeRunStatus(parent.status) not in {
                    CodeRunStatus.PAUSED,
                    CodeRunStatus.REVIEW_REQUIRED,
                    CodeRunStatus.FAILED,
                    CodeRunStatus.INTERRUPTED,
                }:
                    raise ConflictError("parent code run is not paused for an agent mutation")
                if await self.repository.next_queued_code_turn(parent_id) is not None:
                    raise ConflictError("parent code run has queued work")
                if not parent.isolated_path or not await run_sync(
                    Path(parent.isolated_path).is_dir
                ):
                    raise ConflictError("parent code run isolation is unavailable")
                yield SimpleNamespace(
                    workspace_id=workspace.workspace_id,
                    root_path=parent.isolated_path,
                    git_root=parent.isolated_path,
                )
            finally:
                await self.repository.release_code_run_lease(parent_id, self._mutation_owner, token)

    async def _apply_to_target(self, record: Any, review: Any, workspace: Any) -> CodeRunView:
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
        if latest.review_revision != review.revision or CodeRunStatus(latest.status) not in {
            CodeRunStatus.REVIEW_REQUIRED,
            CodeRunStatus.PAUSED,
            CodeRunStatus.FAILED,
        }:
            raise ConflictError("review revision changed before apply")
        if await self.repository.next_queued_code_turn(code_run_id) is not None:
            raise ConflictError("code run has a queued turn; wait for the latest review")
        original_root = Path(workspace.root_path)
        parent = (
            await self.repository.get_code_run(record.parent_code_run_id)
            if record.parent_code_run_id
            else None
        )

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

        if parent is not None:
            # A child's first write must preserve the parent's original bytes,
            # including paths never touched by the parent's own file tools.
            for relative, target in targets.items():
                await run_sync(
                    self.run_store.ensure_baseline,
                    parent.code_run_id,
                    relative,
                    target,
                    (parent.baseline_manifest or {}).get(relative),
                )

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
                    await run_sync(DurableFileWriter.delete, target, journal.journal_id)
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
            self._remove_apply_temps(original_root, journal.journal_id, list(targets))
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
            "Agent 修改已合并到主任务隔离区"
            if record.parent_code_run_id
            else "代码修改已应用到原工作区",
            {
                "code_run_id": code_run_id,
                "changed_paths": record.changed_paths,
                "parent_code_run_id": record.parent_code_run_id,
            },
        )
        if record.parent_code_run_id:
            parent = await self.repository.get_code_run(record.parent_code_run_id)
            await self.runtime.refresh_review(
                parent,
                original_root,
                f"已合并写入 Agent {record.code_run_id} 的修改",
            )
        await self._cleanup(record, workspace)
        return await self.get(code_run_id)

    async def recover_incomplete_applies(self) -> int:
        journals = await self.repository.list_incomplete_apply_journals()
        recovered = 0
        for journal in journals:
            run = await self.repository.get_code_run(journal.code_run_id)
            workspace: Any = await self.repository.get_workspace(
                journal.workspace_id, active_only=False
            )
            if run.parent_code_run_id:
                parent = await self.repository.get_code_run(run.parent_code_run_id)
                if not parent.isolated_path or not await run_sync(
                    Path(parent.isolated_path).is_dir
                ):
                    continue
                workspace = SimpleNamespace(
                    workspace_id=workspace.workspace_id,
                    root_path=parent.isolated_path,
                    git_root=parent.isolated_path,
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
            item.tool_call_id for item in incomplete if item.phase == CodeToolPhase.UNKNOWN.value
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
            tools=[
                CodeRecoveryToolView(
                    tool_call_id=item.tool_call_id,
                    tool_name=item.tool_name,
                    phase=item.phase,
                    before_hashes=dict(item.before_hashes or {}),
                    expected_after_hashes=dict(item.expected_after_hashes or {}),
                    current_hashes=self._current_tool_hashes(run, item),
                )
                for item in incomplete
            ],
        )

    def _current_tool_hashes(self, run: Any, tool_call: Any) -> Dict[str, Optional[str]]:
        if not run.isolated_path:
            return {}
        root = Path(run.isolated_path)
        current: Dict[str, Optional[str]] = {}
        for relative in set(tool_call.before_hashes or {}) | set(
            tool_call.expected_after_hashes or {}
        ):
            try:
                path = self.workspaces.safe_path(root, relative, allow_missing=True)
                current[relative] = file_hash(path)
            except (OSError, PermissionError, ValueError):
                current[relative] = None
        return current

    async def decide_recovery(
        self,
        code_run_id: str,
        tool_call_id: str,
        request: CodeRecoveryDecision,
    ) -> CodeRecoveryView:
        tool_call = await self.repository.get_tool_call(tool_call_id)
        if tool_call.code_run_id != code_run_id:
            raise ConflictError("tool call does not belong to this code run")
        if request.action == "discard_run":
            await self.discard(code_run_id)
            return await self.recovery(code_run_id)
        run = await self.repository.get_code_run(code_run_id)
        if tool_call.phase not in {
            CodeToolPhase.RUNNING.value,
            CodeToolPhase.UNKNOWN.value,
        }:
            raise ConflictError("tool call no longer requires recovery")
        current = self._current_tool_hashes(run, tool_call)
        if request.action == "mark_retryable":
            before = dict(tool_call.before_hashes or {})
            if not before or any(current.get(path) != digest for path, digest in before.items()):
                raise ConflictError("current files do not match the pre-tool state")
            phase = CodeToolPhase.FAILED
            status = "operator_retryable"
            message = "已确认文件仍处于工具执行前状态，可安全重试"
        else:
            phase = CodeToolPhase.SUCCEEDED
            status = "operator_accepted"
            message = "已接受当前文件状态作为工具执行结果"
        await self.repository.resolve_incomplete_tool_call(
            tool_call_id,
            phase=phase,
            status=status,
            result_excerpt=message,
            after_hashes=current,
        )
        await self.runtime.emit(
            run.session_id,
            "code.recovery.decided",
            message,
            {
                "code_run_id": code_run_id,
                "tool_call_id": tool_call_id,
                "action": request.action,
            },
        )
        return await self.recovery(code_run_id)

    async def resume(self, code_run_id: str, request: CodeRunResume) -> CodeRunView:
        run = await self.repository.get_code_run(code_run_id)
        isolation_available = False
        if run.isolated_path:
            isolation_available = await run_sync(Path(run.isolated_path).is_dir)
        if isolation_available:
            assert run.isolated_path is not None
            await self.runtime.reconcile_incomplete_tools(code_run_id, Path(run.isolated_path))
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

    async def verify(self, code_run_id: str, profile_id: str) -> CodeVerificationView:
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
                path = self.workspaces.safe_path(isolated_root, relative, allow_missing=True)
                if file_hash(path) != expected:
                    raise ConflictError(f"isolated workspace changed after review: {relative}")
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
        return [self._verification_view(item, run.review_revision) for item in records]

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
        async with self._mutation_target(record) as workspace:
            return await self._revert_target(record, workspace)

    async def _revert_target(self, record: Any, workspace: Any) -> CodeRunView:
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
            expected_hashes={
                relative: metadata.get("before_hash")
                for relative, metadata in latest.applied_manifest.items()
            },
        )
        record = await self.repository.update_code_run(code_run_id, status=CodeRunStatus.REVERTED)
        await self.runtime.emit(
            record.session_id,
            "code.run.reverted",
            "代码修改已撤销",
            {"code_run_id": code_run_id, "changed_paths": record.changed_paths},
        )
        if record.parent_code_run_id:
            parent = await self.repository.get_code_run(record.parent_code_run_id)
            await self.runtime.refresh_review(
                parent, original_root, f"已撤销 Agent {record.code_run_id} 的合并"
            )
        await self.runtime.release_worker(code_run_id)
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
        await self.runtime.release_worker(record.code_run_id)
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
        paths = list(relative_paths)
        # Validate every backup before restoring any file. Missing evidence must
        # never be treated as evidence that the original file did not exist.
        for relative in paths:
            backup = self.run_store.backup_path(code_run_id, relative)
            expected = (expected_hashes or {}).get(relative)
            if backup.exists():
                if (
                    expected_hashes is not None
                    and relative in expected_hashes
                    and file_hash(backup) != expected
                ):
                    raise ConflictError(f"apply backup integrity check failed: {relative}")
                continue
            if expected is not None:
                raise ConflictError(f"apply backup is missing: {relative}")
            marker = backup.with_name(backup.name + ".missing.json")
            try:
                missing = json.loads(marker.read_text(encoding="utf-8")) == {"missing": True}
            except (OSError, ValueError):
                missing = False
            if not missing:
                raise ConflictError(f"apply backup absence marker is missing: {relative}")

        for relative in paths:
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
            parent_code_run_id=record.parent_code_run_id,
            agent_mode=record.agent_mode,
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
