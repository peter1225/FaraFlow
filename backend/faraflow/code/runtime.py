import asyncio
import json
import subprocess
import time
import uuid
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from faraflow.config import Settings
from faraflow.domain.enums import (
    CodeRunStatus,
    CodeToolPhase,
    CodeTurnStatus,
    CodeVerificationStatus,
)
from faraflow.domain.schemas import SessionEvent
from faraflow.infra.async_utils import run_sync
from faraflow.infra.events import EventBus
from faraflow.infra.repository import ConflictError, Repository
from faraflow.workspace.run_store import CodeRunStore, file_hash
from faraflow.workspace.service import WorkspaceService

from .adapter import CodeAdapter
from .engine import CodeEngine, EngineRequest, NativeCodeEngine
from .pico_bridge import PicoCodeEngine
from .registry import ToolRegistry, ToolSpec
from .tools import CodeToolExecutor, CodeToolResult
from .verification import VerificationRunner


class CodeRuntime:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        workspaces: WorkspaceService,
        run_store: CodeRunStore,
        adapter: CodeAdapter,
        event_bus: EventBus,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.workspaces = workspaces
        self.run_store = run_store
        self.adapter = adapter
        self.event_bus = event_bus
        self._jobs: Dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(settings.code_max_concurrent_runs)
        self._restart_requested: Set[str] = set()
        self._cancel_preserve: Dict[str, bool] = {}
        self._lease_tokens: Dict[str, int] = {}
        self._injections: Dict[str, List[Tuple[str, str]]] = {}
        self._closing = False
        self._owner_id = f"code-runtime-{uuid.uuid4().hex}"
        self.tool_registry = ToolRegistry.default()
        self.verification = VerificationRunner(settings, repository, workspaces)
        self.engine: CodeEngine = (
            PicoCodeEngine(settings)
            if settings.code_engine == "pico"
            else NativeCodeEngine(settings, adapter)
        )

    async def emit(
        self,
        session_id: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> SessionEvent:
        record = await self.repository.append_event(session_id, event_type, message, payload)
        event = SessionEvent(
            event_id=record.event_id,
            session_id=record.session_id,
            sequence=getattr(record, "sequence", 0),
            event_type=record.event_type,
            message=record.message,
            payload=record.payload,
            created_at=record.created_at,
        )
        await self.event_bus.publish(event)
        marker = getattr(self.repository, "mark_event_published", None)
        if marker is not None:
            await marker(record.event_id)
        return event

    async def start(self, code_run_id: str) -> None:
        record = await self.repository.get_code_run(code_run_id)
        if record.engine != self.settings.code_engine or record.model != self.settings.code_model:
            raise ConflictError("code run engine/model differs from the active configuration")
        allowed = {
            CodeRunStatus.CREATED,
            CodeRunStatus.RUNNING,
            CodeRunStatus.PAUSED,
            CodeRunStatus.REVIEW_REQUIRED,
            CodeRunStatus.FAILED,
            CodeRunStatus.INTERRUPTED,
        }
        if CodeRunStatus(record.status) not in allowed:
            raise ConflictError(f"cannot start code run in state {record.status}")
        if await self.repository.next_queued_code_turn(code_run_id) is None:
            raise ConflictError("code run has no queued turn")
        async with self._lock:
            existing = self._jobs.get(code_run_id)
            if existing and not existing.done():
                self._restart_requested.add(code_run_id)
                return
            token = await self.repository.acquire_code_run_lease(
                code_run_id, self._owner_id, self.settings.code_lease_seconds
            )
            if token is None:
                raise ConflictError("code run is leased by another runtime")
            self._lease_tokens[code_run_id] = token
            job = asyncio.create_task(self._run(code_run_id), name=f"faraflow-code:{code_run_id}")
            self._jobs[code_run_id] = job
            job.add_done_callback(partial(self._forget_job, code_run_id))

    def _forget_job(
        self,
        code_run_id: str,
        finished: asyncio.Task,  # type: ignore[type-arg]
    ) -> None:
        if self._jobs.get(code_run_id) is finished:
            self._jobs.pop(code_run_id, None)
        if code_run_id in self._restart_requested:
            self._restart_requested.discard(code_run_id)
            asyncio.create_task(
                self._restart_if_queued(code_run_id),
                name=f"faraflow-code-restart:{code_run_id}",
            )

    async def _restart_if_queued(self, code_run_id: str) -> None:
        if await self.repository.next_queued_code_turn(code_run_id) is not None:
            try:
                await self.start(code_run_id)
            except ConflictError:
                return

    async def pause(self, code_run_id: str) -> None:
        await self._cancel(code_run_id, preserve=True)

    async def cancel(self, code_run_id: str) -> None:
        await self._cancel(code_run_id, preserve=False)

    async def inject(self, code_run_id: str, turn_id: str, instruction: str) -> bool:
        """Queue an instruction for the next completed tool boundary.

        The database turn remains QUEUED until the boundary is reached. If the
        process exits first, normal recovery treats it as an appended turn.
        """
        async with self._lock:
            job = self._jobs.get(code_run_id)
            if job is None or job.done():
                return False
            self._injections.setdefault(code_run_id, []).append((turn_id, instruction))
            return True

    async def _drain_injections(
        self, code_run_id: str, session_id: str, active_turn_id: str
    ) -> str:
        async with self._lock:
            pending = self._injections.pop(code_run_id, [])
        if not pending:
            return ""
        instructions: List[str] = []
        for turn_id, instruction in pending:
            if turn_id == active_turn_id:
                continue
            await self.repository.update_code_turn(
                turn_id,
                CodeTurnStatus.INJECTED,
                summary="已在工具边界注入，等待活动轮次完成",
            )
            await self.emit(
                session_id,
                "code.turn.injected",
                "新的要求已注入当前协作轮次",
                {"code_run_id": code_run_id, "turn_id": turn_id},
            )
            instructions.append(instruction)
        return "\n".join(instructions)

    async def _cancel(self, code_run_id: str, *, preserve: bool) -> None:
        self._cancel_preserve[code_run_id] = preserve
        self._restart_requested.discard(code_run_id)
        self._injections.pop(code_run_id, None)
        async with self._lock:
            job = self._jobs.get(code_run_id)
        if job and not job.done():
            job.cancel()
            results = await asyncio.gather(job, return_exceptions=True)
            if results and isinstance(results[0], Exception):
                raise results[0]
            await self.release_worker(code_run_id)
            return
        await self.repository.cancel_open_code_turns(code_run_id)
        record = await self.repository.get_code_run(code_run_id)
        if preserve:
            isolated_path = record.isolated_path
            isolation_available = False
            if isolated_path:
                isolation_available = await run_sync(Path(isolated_path).is_dir)
            if isolation_available:
                assert isolated_path is not None
                await self._finalize_review(
                    record,
                    Path(isolated_path),
                    CodeRunStatus.PAUSED,
                    record.final_summary or "代码任务已停止，修改已保留",
                )
            else:
                await self.repository.update_code_run(
                    code_run_id,
                    status=CodeRunStatus.PAUSED,
                    final_summary="代码任务已停止，尚未产生修改",
                )
        self._cancel_preserve.pop(code_run_id, None)
        await self.release_worker(code_run_id)

    async def release_worker(self, code_run_id: str) -> None:
        await self.engine.release(code_run_id)

    async def _prepare_isolation(self, code_run_id: str, record: Any, workspace: Any) -> Any:
        if record.isolated_path and await run_sync(Path(record.isolated_path).is_dir):
            return record

        source_workspace = workspace
        if record.parent_code_run_id:
            parent = await self.repository.get_code_run(record.parent_code_run_id)
            if not parent.isolated_path or not await run_sync(Path(parent.isolated_path).is_dir):
                raise ConflictError("parent code run isolation is unavailable")
            source_workspace = SimpleNamespace(
                root_path=parent.isolated_path,
                git_root=parent.isolated_path,
            )

        async def prepare() -> Any:
            isolation = await run_sync(
                self.workspaces.prepare_isolation, code_run_id, source_workspace
            )
            return await self.repository.update_code_run(code_run_id, **isolation)

        preparation = asyncio.create_task(prepare())
        try:
            return await asyncio.shield(preparation)
        except asyncio.CancelledError:
            await preparation
            raise

    async def _run(self, code_run_id: str) -> None:
        async with self._capacity:
            record = await self.repository.get_code_run(code_run_id)
            workspace = await self.repository.get_workspace(record.workspace_id)
            isolated_root: Optional[Path] = None
            current_turn: Optional[Any] = None
            final_summary = record.final_summary or ""
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(code_run_id),
                name=f"faraflow-code-lease:{code_run_id}",
            )
            try:
                record = await self._prepare_isolation(code_run_id, record, workspace)
                isolated_root = Path(record.isolated_path or "")
                assert isolated_root is not None
                unknown = await self.reconcile_incomplete_tools(code_run_id, isolated_root)
                if unknown:
                    raise ConflictError(
                        "incomplete tool calls require inspection: " + ", ".join(unknown)
                    )
                await self.emit(
                    record.session_id,
                    "code.run.started",
                    "代码任务开始执行",
                    {
                        "code_run_id": code_run_id,
                        "workspace_id": record.workspace_id,
                        "engine": record.engine,
                    },
                )
                step_no = len(await self.repository.list_tool_calls(code_run_id))

                while True:
                    current_turn = await self.repository.next_queued_code_turn(code_run_id)
                    if current_turn is None:
                        break
                    deadline = time.monotonic() + self.settings.code_max_runtime_minutes * 60
                    await self.repository.update_code_run(
                        code_run_id,
                        status=CodeRunStatus.RUNNING,
                        active_turn_id=current_turn.turn_id,
                    )
                    await self.repository.update_code_turn(
                        current_turn.turn_id, CodeTurnStatus.RUNNING
                    )
                    await self.emit(
                        record.session_id,
                        "code.turn.started",
                        f"第 {current_turn.ordinal} 轮代码协作开始",
                        {
                            "code_run_id": code_run_id,
                            "turn_id": current_turn.turn_id,
                            "ordinal": current_turn.ordinal,
                        },
                    )

                    async def list_verification_profiles(_: Dict[str, Any]) -> CodeToolResult:
                        profiles = self.verification.profiles()
                        content = json.dumps(
                            [
                                {"profile_id": profile_id, "argv": argv}
                                for profile_id, argv in sorted(profiles.items())
                            ],
                            ensure_ascii=False,
                        )
                        return CodeToolResult(content or "[]")

                    async def run_verification(
                        arguments: Dict[str, Any],
                        _turn: Any = current_turn,
                        _root: Optional[Path] = isolated_root,
                        _review_revision: int = int(record.review_revision or 0) + 1,
                    ) -> CodeToolResult:
                        profile_id = arguments.get("profile_id")
                        if not isinstance(profile_id, str) or not profile_id.strip():
                            return CodeToolResult("error: profile_id is required", is_error=True)
                        assert _root is not None
                        verification = await self.verification.run(
                            code_run_id=code_run_id,
                            turn_id=_turn.turn_id,
                            isolated_root=_root,
                            review_revision=_review_revision,
                            profile_id=profile_id.strip(),
                        )
                        await self.emit(
                            record.session_id,
                            "code.verification.completed",
                            f"验证 {profile_id}：{verification.status}",
                            {
                                "code_run_id": code_run_id,
                                "turn_id": _turn.turn_id,
                                "verification_id": verification.verification_id,
                                "profile_id": profile_id,
                                "status": verification.status,
                                "exit_code": verification.exit_code,
                            },
                        )
                        content = json.dumps(
                            {
                                "verification_id": verification.verification_id,
                                "status": verification.status,
                                "exit_code": verification.exit_code,
                                "stdout": verification.stdout_excerpt,
                                "stderr": verification.stderr_excerpt,
                            },
                            ensure_ascii=False,
                        )
                        return CodeToolResult(
                            content,
                            is_error=(
                                verification.status
                                in {
                                    CodeVerificationStatus.ERROR.value,
                                    CodeVerificationStatus.CANCELLED.value,
                                }
                            ),
                        )

                    context = await run_sync(self._workspace_context, isolated_root)
                    registry = self.tool_registry
                    if record.agent_mode == "read_only":
                        registry = ToolRegistry(
                            ToolSpec(name)
                            for name in (
                                "list_files",
                                "read_file",
                                "search",
                                "git_status",
                                "git_diff",
                            )
                        )
                        context += (
                            "\n\nAgent mode: READ ONLY. Inspect and report. "
                            "Never call create_file, patch_file, delete_file, "
                            "or verification tools."
                        )
                    executor = CodeToolExecutor(
                        code_run_id=code_run_id,
                        session_id=record.session_id,
                        turn_id=current_turn.turn_id,
                        root=isolated_root,
                        baseline_manifest=dict(record.baseline_manifest or {}),
                        repository=self.repository,
                        workspace_service=self.workspaces,
                        run_store=self.run_store,
                        registry=registry,
                        external_tools={
                            "list_verification_profiles": list_verification_profiles,
                            "run_verification": run_verification,
                        },
                    )
                    executor.changed_paths.update(
                        await run_sync(self._changed_paths, record, isolated_root)
                    )
                    turns = await self.repository.list_code_turns(code_run_id)
                    prior = [
                        f"{item.ordinal}. {item.instruction}: {item.summary or item.status}"
                        for item in turns
                        if item.ordinal < current_turn.ordinal
                    ]
                    if prior:
                        context += "\n\nPrior collaboration turns:\n" + "\n".join(prior[-10:])
                    active_turn = current_turn
                    active_executor = executor
                    sequence = 0
                    turn_step = 0

                    async def engine_event(
                        event_type: str,
                        message: str,
                        payload: Dict[str, Any],
                        _turn: Any = active_turn,
                    ) -> None:
                        nonlocal sequence
                        sequence += 1
                        await self.emit(
                            record.session_id,
                            event_type,
                            message,
                            {
                                **payload,
                                "code_run_id": code_run_id,
                                "turn_id": _turn.turn_id,
                                "engine": record.engine,
                                "sequence": sequence,
                            },
                        )

                    async def execute_and_audit(
                        name: str,
                        arguments: Dict[str, Any],
                        call_id: str,
                        _executor: CodeToolExecutor = active_executor,
                        _turn: Any = active_turn,
                    ) -> CodeToolResult:
                        nonlocal step_no, turn_step
                        step_no += 1
                        turn_step += 1
                        if turn_step > self.settings.code_max_steps:
                            raise RuntimeError("code turn exceeded maximum tool calls")
                        details = {
                            "step_no": step_no,
                            "tool_name": name,
                            "tool_call_id": call_id,
                        }
                        await engine_event("code.tool.started", f"正在执行 {name}", details)
                        renewed = await self.repository.renew_code_run_lease(
                            code_run_id,
                            self._owner_id,
                            self._lease_tokens[code_run_id],
                            self.settings.code_lease_seconds,
                        )
                        if not renewed:
                            raise ConflictError("code run lease was lost")
                        result = await _executor.execute(
                            step_no,
                            name,
                            arguments,
                            external_call_id=f"{_turn.turn_id}:{call_id}",
                        )
                        await engine_event(
                            "code.tool.failed" if result.is_error else "code.tool.completed",
                            _executor.audit_summary(name, result)[:500],
                            {
                                **details,
                                "affected_paths": result.affected_paths,
                                "diff_summary": result.diff_summary,
                            },
                        )
                        injected = await self._drain_injections(
                            code_run_id, record.session_id, _turn.turn_id
                        )
                        if injected:
                            result.content += (
                                "\n\n[New user instructions injected at this tool boundary]\n"
                                + injected
                            )
                        return result

                    async def execute(
                        name: str, arguments: Dict[str, Any], call_id: str
                    ) -> CodeToolResult:
                        operation = asyncio.create_task(execute_and_audit(name, arguments, call_id))
                        try:
                            return await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            await operation
                            raise

                    final_summary = await asyncio.wait_for(
                        self.engine.run(
                            EngineRequest(
                                code_run_id,
                                active_turn.instruction,
                                isolated_root,
                                context,
                                source_root=Path(workspace.root_path),
                            ),
                            execute,
                            engine_event,
                        ),
                        timeout=max(0.1, deadline - time.monotonic()),
                    )
                    await self.repository.complete_code_turn_with_injections(
                        code_run_id, current_turn.turn_id, final_summary
                    )
                    # Requests that missed a tool boundary remain queued as
                    # APPEND; do not inject them into their own subsequent turn.
                    self._injections.pop(code_run_id, None)
                    await self.repository.update_code_run(code_run_id, active_turn_id=None)
                    await self.emit(
                        record.session_id,
                        "code.turn.completed",
                        final_summary or f"第 {current_turn.ordinal} 轮已完成",
                        {
                            "code_run_id": code_run_id,
                            "turn_id": current_turn.turn_id,
                            "ordinal": current_turn.ordinal,
                        },
                    )
                    current_turn = None

                if heartbeat.done():
                    heartbeat.result()
                record = await self.repository.get_code_run(code_run_id)
                await self._finalize_review(
                    record,
                    isolated_root,
                    CodeRunStatus.REVIEW_REQUIRED,
                    final_summary or "代码协作轮次已完成",
                )
            except asyncio.CancelledError:
                await self.repository.cancel_open_code_turns(
                    code_run_id, include_injected=not self._closing
                )
                if self._closing:
                    pending = self._injections.pop(code_run_id, [])
                    await self.repository.retry_code_injections(
                        code_run_id, [turn_id for turn_id, _ in pending]
                    )
                preserve = self._cancel_preserve.pop(code_run_id, False)
                if preserve:
                    record = await self.repository.get_code_run(code_run_id)
                    if isolated_root is not None and isolated_root.is_dir():
                        try:
                            await self._finalize_review(
                                record,
                                isolated_root,
                                CodeRunStatus.PAUSED,
                                final_summary or "代码任务已停止，修改已保留",
                            )
                        except Exception as exc:
                            await self.repository.update_code_run(
                                code_run_id,
                                status=CodeRunStatus.FAILED,
                                error={"type": type(exc).__name__, "message": str(exc)},
                                active_turn_id=None,
                                final_summary="审核保存失败，隔离区修改已保留，可恢复后重试",
                            )
                            return
                    else:
                        await self.repository.update_code_run(
                            code_run_id,
                            status=CodeRunStatus.PAUSED,
                            final_summary="代码任务已停止，尚未产生修改",
                        )
                    await self.emit(
                        record.session_id,
                        "code.run.paused",
                        "代码任务已停止，当前修改已保留",
                        {"code_run_id": code_run_id},
                    )
                return
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                if current_turn is not None:
                    await self.repository.update_code_turn(
                        current_turn.turn_id, CodeTurnStatus.FAILED, error=error
                    )
                await self.repository.cancel_open_code_turns(code_run_id, include_injected=False)
                pending = self._injections.pop(code_run_id, [])
                await self.repository.retry_code_injections(
                    code_run_id, [turn_id for turn_id, _ in pending]
                )
                record = await self.repository.get_code_run(code_run_id)
                if isolated_root is not None and isolated_root.is_dir():
                    try:
                        await self._finalize_review(
                            record,
                            isolated_root,
                            CodeRunStatus.FAILED,
                            final_summary or "代码任务执行失败，已保留现有修改",
                        )
                    except Exception as review_exc:
                        error["review_error"] = str(review_exc)
                await self.repository.update_code_run(
                    code_run_id, status=CodeRunStatus.FAILED, error=error, active_turn_id=None
                )
                await self.emit(
                    record.session_id,
                    "code.run.failed",
                    "代码任务执行失败",
                    {"error_type": type(exc).__name__, "message": str(exc)},
                )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self.repository.release_code_run_lease(
                    code_run_id,
                    self._owner_id,
                    self._lease_tokens.pop(code_run_id, 0),
                )

    async def _lease_heartbeat(self, code_run_id: str) -> None:
        interval = max(5, self.settings.code_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await self.repository.renew_code_run_lease(
                code_run_id,
                self._owner_id,
                self._lease_tokens[code_run_id],
                self.settings.code_lease_seconds,
            )
            if not renewed:
                raise ConflictError("code run lease was lost")

    async def reconcile_incomplete_tools(self, code_run_id: str, isolated_root: Path) -> List[str]:
        unknown: List[str] = []
        calls = await self.repository.list_incomplete_tool_calls(code_run_id)
        for call in calls:
            before = dict(call.before_hashes or {})
            expected = dict(call.expected_after_hashes or {})
            if not expected:
                await self.repository.resolve_incomplete_tool_call(
                    call.tool_call_id,
                    phase=CodeToolPhase.FAILED,
                    status="interrupted_retryable",
                    result_excerpt="interrupted before a durable result was recorded",
                    after_hashes={},
                )
                continue
            current: Dict[str, Optional[str]] = {}
            for relative in expected:
                try:
                    path = self.workspaces.safe_path(isolated_root, relative, allow_missing=True)
                    current[relative] = file_hash(path)
                except (OSError, PermissionError, ValueError):
                    current[relative] = "unreadable"
            if all(current.get(path) == digest for path, digest in expected.items()):
                await self.repository.resolve_incomplete_tool_call(
                    call.tool_call_id,
                    phase=CodeToolPhase.SUCCEEDED,
                    status="reconciled",
                    result_excerpt="reconciled after runtime restart",
                    after_hashes=current,
                )
            elif all(current.get(path) == digest for path, digest in before.items()):
                await self.repository.resolve_incomplete_tool_call(
                    call.tool_call_id,
                    phase=CodeToolPhase.FAILED,
                    status="interrupted_retryable",
                    result_excerpt="no filesystem effect detected after interruption",
                    after_hashes=current,
                )
            else:
                unknown.append(call.tool_call_id)
                await self.repository.resolve_incomplete_tool_call(
                    call.tool_call_id,
                    phase=CodeToolPhase.UNKNOWN,
                    status="unknown",
                    result_excerpt="filesystem state does not match before or expected hashes",
                    after_hashes=current,
                )
        return unknown

    def _changed_paths(self, record: Any, isolated_root: Path) -> List[str]:
        current = self.workspaces.snapshot_manifest(isolated_root)
        baseline = dict(record.baseline_manifest or {})
        return sorted(
            path for path in set(baseline) | set(current) if baseline.get(path) != current.get(path)
        )

    async def _finalize_review(
        self,
        record: Any,
        isolated_root: Path,
        status: CodeRunStatus,
        summary: str,
    ) -> None:
        changed = await run_sync(self._changed_paths, record, isolated_root)
        diff, changed = self.run_store.build_diff(record.code_run_id, isolated_root, changed)
        if not changed and not record.review_revision:
            final_status = (
                CodeRunStatus.PAUSED if status == CodeRunStatus.REVIEW_REQUIRED else status
            )
            await self.repository.update_code_run(
                record.code_run_id,
                status=final_status,
                final_summary=summary,
                changed_paths=[],
                diff_ref="",
                review_manifest={},
            )
            return
        if not changed and status == CodeRunStatus.REVIEW_REQUIRED:
            status = CodeRunStatus.PAUSED
        revision = int(record.review_revision or 0) + 1
        manifest = self.run_store.preserve_review(
            record.code_run_id, revision, isolated_root, changed
        )
        diff_ref = self.run_store.preserve_review_diff(record.code_run_id, revision, diff)
        await self.repository.create_code_review(
            record.code_run_id,
            revision=revision,
            changed_paths=changed,
            manifest=manifest,
            diff_ref=diff_ref,
            status=status,
            final_summary=summary,
        )
        await self.emit(
            record.session_id,
            "code.review.required"
            if status == CodeRunStatus.REVIEW_REQUIRED
            else "code.review.saved",
            "代码修改已生成，等待确认应用" if status == CodeRunStatus.REVIEW_REQUIRED else summary,
            {
                "code_run_id": record.code_run_id,
                "review_revision": revision,
                "changed_paths": changed,
                "diff_ref": diff_ref,
                "diff_size": len(diff.encode("utf-8")),
            },
        )

    async def refresh_review(self, record: Any, isolated_root: Path, summary: str) -> None:
        """Publish a new immutable review after an agent merges into its parent."""
        await self._finalize_review(
            record,
            isolated_root,
            CodeRunStatus.REVIEW_REQUIRED,
            summary,
        )

    @staticmethod
    def _workspace_context(root: Path) -> str:
        def git(arguments: List[str], fallback: str = "") -> str:
            try:
                result = subprocess.run(
                    ["git", *arguments],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
                return result.stdout.strip() if result.returncode == 0 else fallback
            except OSError:
                return fallback

        sections = [
            f"root: {root}",
            f"branch: {git(['branch', '--show-current'], '-') or '-'}",
            f"status:\n{git(['status', '--short'], 'not a Git repository') or 'clean'}",
            f"recent commits:\n{git(['log', '--oneline', '-5'], 'none') or 'none'}",
        ]
        for name in ("AGENTS.md", "README.md", "pyproject.toml", "package.json"):
            path = root / name
            if path.is_file() and path.stat().st_size <= 128 * 1024:
                text = path.read_text(encoding="utf-8", errors="replace")[:4000]
                sections.append(f"{name}:\n{text}")
        return "\n\n".join(sections)

    async def close(self) -> None:
        self._closing = True
        jobs = list(self._jobs.items())
        for run_id, job in jobs:
            self._cancel_preserve[run_id] = False
            self._restart_requested.discard(run_id)
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        await self.engine.close()
