import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from faraflow.config import Settings
from faraflow.domain.enums import CodeRunStatus
from faraflow.domain.schemas import SessionEvent
from faraflow.infra.async_utils import run_sync
from faraflow.infra.events import EventBus
from faraflow.infra.repository import ConflictError, Repository
from faraflow.workspace.run_store import CodeRunStore
from faraflow.workspace.service import WorkspaceService

from .adapter import CodeAdapter
from .engine import CodeEngine, EngineRequest, NativeCodeEngine
from .pico_bridge import PicoCodeEngine
from .registry import ToolRegistry
from .tools import CodeToolExecutor, CodeToolResult


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
        self._jobs: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(settings.code_max_concurrent_runs)
        self.tool_registry = ToolRegistry.default()
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
            event_type=record.event_type,
            message=record.message,
            payload=record.payload,
            created_at=record.created_at,
        )
        await self.event_bus.publish(event)
        return event

    async def start(self, code_run_id: str) -> None:
        record = await self.repository.get_code_run(code_run_id)
        if CodeRunStatus(record.status) != CodeRunStatus.CREATED:
            raise ConflictError(f"cannot start code run in state {record.status}")
        async with self._lock:
            existing = self._jobs.get(code_run_id)
            if existing and not existing.done():
                raise ConflictError("code run is already running")
            job = asyncio.create_task(self._run(code_run_id), name=f"faraflow-code:{code_run_id}")
            self._jobs[code_run_id] = job
            job.add_done_callback(
                lambda finished, run_id=code_run_id: self._forget_job(run_id, finished)
            )

    def _forget_job(self, code_run_id: str, finished: asyncio.Task) -> None:
        if self._jobs.get(code_run_id) is finished:
            self._jobs.pop(code_run_id, None)

    async def cancel(self, code_run_id: str) -> None:
        async with self._lock:
            job = self._jobs.get(code_run_id)
            if job and not job.done():
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def _run(self, code_run_id: str) -> None:
        async with self._capacity:
            record = await self.repository.get_code_run(code_run_id)
            workspace = await self.repository.get_workspace(record.workspace_id)
            deadline = time.monotonic() + self.settings.code_max_runtime_minutes * 60
            try:
                await self.repository.update_code_run(code_run_id, status=CodeRunStatus.RUNNING)
                await self.emit(
                    record.session_id,
                    "code.run.started",
                    "代码任务开始执行",
                    {
                        "code_run_id": code_run_id,
                        "workspace_id": record.workspace_id,
                        "engine": self.settings.code_engine,
                    },
                )

                async def prepare() -> Any:
                    isolation = await run_sync(
                        self.workspaces.prepare_isolation, code_run_id, workspace
                    )
                    return await self.repository.update_code_run(code_run_id, **isolation)

                preparation = asyncio.create_task(prepare())
                try:
                    record = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    await preparation
                    raise
                isolated_root = Path(record.isolated_path or "")
                executor = CodeToolExecutor(
                    code_run_id=code_run_id,
                    session_id=record.session_id,
                    root=isolated_root,
                    baseline_manifest=dict(record.baseline_manifest or {}),
                    repository=self.repository,
                    workspace_service=self.workspaces,
                    run_store=self.run_store,
                    registry=self.tool_registry,
                )
                context = await run_sync(self._workspace_context, isolated_root)
                step = 0
                sequence = 0

                async def engine_event(
                    event_type: str, message: str, payload: Dict[str, Any]
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
                            "turn_id": code_run_id,
                            "engine": self.settings.code_engine,
                            "sequence": sequence,
                        },
                    )

                async def execute_and_audit(
                    name: str, arguments: Dict[str, Any], call_id: str
                ) -> CodeToolResult:
                    nonlocal step
                    step += 1
                    if step > self.settings.code_max_steps:
                        raise RuntimeError("code run exceeded maximum tool calls")
                    details = {"step_no": step, "tool_name": name, "tool_call_id": call_id}
                    await engine_event("code.tool.started", f"正在执行 {name}", details)
                    result = await executor.execute(step, name, arguments)
                    await engine_event(
                        "code.tool.failed" if result.is_error else "code.tool.completed",
                        executor.audit_summary(name, result)[:500],
                        {
                            **details,
                            "affected_paths": result.affected_paths,
                            "diff_summary": result.diff_summary,
                        },
                    )
                    return result

                async def execute(
                    name: str, arguments: Dict[str, Any], call_id: str
                ) -> CodeToolResult:
                    # Cancelling a to_thread write cannot stop its thread. Finish the
                    # operation and audit before discard is allowed to delete isolation.
                    operation = asyncio.create_task(execute_and_audit(name, arguments, call_id))
                    try:
                        return await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        await operation
                        raise

                final_summary = await asyncio.wait_for(
                    self.engine.run(
                        EngineRequest(
                            code_run_id, record.instruction, isolated_root, context,
                            source_root=Path(workspace.root_path),
                        ),
                        execute,
                        engine_event,
                    ),
                    timeout=max(0.1, deadline - time.monotonic()),
                )

                diff, changed = self.run_store.build_diff(
                    code_run_id, isolated_root, executor.changed_paths
                )
                if changed:
                    await self.repository.update_code_run(
                        code_run_id,
                        status=CodeRunStatus.REVIEW_REQUIRED,
                        final_summary=final_summary,
                        changed_paths=changed,
                        diff_ref=self.run_store.diff_reference(code_run_id),
                    )
                    await self.emit(
                        record.session_id,
                        "code.review.required",
                        "代码修改已生成，等待确认应用",
                        {
                            "code_run_id": code_run_id,
                            "changed_paths": changed,
                            "diff_ref": self.run_store.diff_reference(code_run_id),
                            "diff_size": len(diff.encode("utf-8")),
                        },
                    )
                else:
                    await self.repository.update_code_run(
                        code_run_id,
                        status=CodeRunStatus.APPLIED,
                        final_summary=final_summary,
                        changed_paths=[],
                    )
                    await self.emit(
                        record.session_id,
                        "code.run.completed",
                        final_summary or "代码任务完成，未产生文件修改",
                        {"code_run_id": code_run_id, "changed_paths": []},
                    )
                    await run_sync(self.workspaces.cleanup_isolation, record, workspace)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                await self.repository.update_code_run(
                    code_run_id,
                    status=CodeRunStatus.FAILED,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
                await self.emit(
                    record.session_id,
                    "code.run.failed",
                    "代码任务执行失败",
                    {"error_type": type(exc).__name__, "message": str(exc)},
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
        jobs = list(self._jobs.values())
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
