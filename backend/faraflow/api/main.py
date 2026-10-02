import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse

from faraflow.browser.playwright_runner import BrowserPool
from faraflow.code import CodeAdapter, CodeRunService, CodeRuntime
from faraflow.code.gc import CodeArtifactGarbageCollector
from faraflow.config import Settings, get_settings
from faraflow.desktop import (
    DesktopAdapter,
    DesktopBridge,
    DesktopPolicy,
    DesktopRunService,
    DesktopRuntime,
)
from faraflow.domain.schemas import SessionEvent
from faraflow.infra.artifacts import ArtifactStore
from faraflow.infra.async_utils import run_sync
from faraflow.infra.database import Database
from faraflow.infra.events import EventBus
from faraflow.infra.outbox import EventOutboxDispatcher
from faraflow.infra.repository import ConflictError, NotFoundError, Repository
from faraflow.model.chat_adapter import ChatAdapter
from faraflow.model.fara_adapter import FaraAdapter
from faraflow.runtime.agent_runtime import AgentRuntime
from faraflow.runtime.chat_service import ChatService
from faraflow.runtime.task_service import TaskService
from faraflow.workspace import CodeRunStore, WorkspaceService

from . import chats, code_runs, desktop_runs, tasks, workspaces
from .dependencies import (
    get_container,
    is_loopback_host,
    require_desktop_request,
    require_local_workspace_request,
)

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    database: Database
    repository: Repository
    artifacts: ArtifactStore
    event_bus: EventBus
    event_outbox: EventOutboxDispatcher
    browser_pool: BrowserPool
    fara: FaraAdapter
    chat_model: ChatAdapter
    runtime: AgentRuntime
    tasks: TaskService
    code_adapter: CodeAdapter
    code_runtime: CodeRuntime
    code_runs: CodeRunService
    code_gc: CodeArtifactGarbageCollector
    workspaces: WorkspaceService
    desktop_adapter: DesktopAdapter
    desktop_runtime: DesktopRuntime
    desktop_runs: DesktopRunService
    chat: ChatService


def build_container(settings: Settings) -> Container:
    database = Database(settings.database_url)
    repository = Repository(database.session_factory)
    artifacts = ArtifactStore(settings.artifact_root)
    event_bus = EventBus()
    event_outbox = EventOutboxDispatcher(repository, event_bus)
    browser_pool = BrowserPool(settings, artifacts)
    fara = FaraAdapter(settings)
    chat_model = ChatAdapter(settings)
    runtime = AgentRuntime(repository, browser_pool, fara, event_bus)
    task_service = TaskService(repository, runtime)
    code_adapter = CodeAdapter(settings)
    code_run_store = CodeRunStore(
        artifacts.root,
        quota_bytes=int(settings.code_artifact_quota_gb * 1024 * 1024 * 1024),
    )
    workspace_service = WorkspaceService(settings, repository, code_run_store)
    code_runtime = CodeRuntime(
        settings,
        repository,
        workspace_service,
        code_run_store,
        code_adapter,
        event_bus,
    )
    code_run_service = CodeRunService(repository, workspace_service, code_run_store, code_runtime)
    code_gc = CodeArtifactGarbageCollector(settings, repository, code_run_store)
    desktop_adapter = DesktopAdapter(settings)
    desktop_bridge = DesktopBridge()
    desktop_policy = DesktopPolicy(
        allowed_apps=settings.desktop_allowed_apps,
        max_actions=settings.desktop_max_steps,
        require_confirmation=settings.desktop_require_confirmation,
        unattended_mode=settings.desktop_unattended_mode,
    )
    desktop_runtime = DesktopRuntime(
        settings,
        repository,
        artifacts,
        desktop_adapter,
        desktop_bridge,
        desktop_policy,
        event_bus,
    )
    desktop_run_service = DesktopRunService(
        settings, repository, desktop_runtime, desktop_bridge, desktop_policy
    )
    chat_service = ChatService(
        repository, chat_model, task_service, code_run_service, desktop_run_service
    )
    return Container(
        settings=settings,
        database=database,
        repository=repository,
        artifacts=artifacts,
        event_bus=event_bus,
        event_outbox=event_outbox,
        browser_pool=browser_pool,
        fara=fara,
        chat_model=chat_model,
        runtime=runtime,
        tasks=task_service,
        code_adapter=code_adapter,
        code_runtime=code_runtime,
        code_runs=code_run_service,
        code_gc=code_gc,
        workspaces=workspace_service,
        desktop_adapter=desktop_adapter,
        desktop_runtime=desktop_runtime,
        desktop_runs=desktop_run_service,
        chat=chat_service,
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    active_settings = settings or get_settings()
    active_settings.prepare_directories()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(active_settings)
        app.state.container = container
        await container.database.create_schema()
        await container.repository.interrupt_running_code_runs()
        await container.code_runs.recover_incomplete_applies()
        await run_sync(container.code_runtime.run_store.detach_legacy_review_links)
        gc_result = await container.code_gc.collect()
        if gc_result["quota_exceeded"]:
            logger.warning("Code artifact quota exceeded: %s", gc_result)
        await container.repository.interrupt_running_desktop_runs()
        await container.tasks.seed_skills()
        container.event_outbox.start()
        logger.info("FaraFlow API started")
        try:
            yield
        finally:
            await container.event_outbox.close()
            await container.runtime.close()
            await container.code_runtime.close()
            await container.desktop_runtime.close()
            await container.browser_pool.close()
            await container.chat_model.close()
            await container.fara.close()
            await container.code_adapter.close()
            await container.desktop_adapter.close()
            await container.database.dispose()

    app = FastAPI(
        title="FaraFlow API",
        version="0.1.0",
        description="Controlled chat, browser automation, and local coding workspace platform",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> ORJSONResponse:
        return ORJSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError) -> ORJSONResponse:
        return ORJSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def permission_handler(_: Request, exc: PermissionError) -> ORJSONResponse:
        return ORJSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_handler(_: Request, exc: ValueError) -> ORJSONResponse:
        return ORJSONResponse(status_code=422, content={"detail": str(exc)})

    app.include_router(chats.router)
    app.include_router(tasks.router)
    app.include_router(workspaces.router)
    app.include_router(code_runs.router)
    app.include_router(desktop_runs.router)

    @app.get("/health/live", tags=["health"])
    async def health_live() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(request: Request) -> Dict[str, Any]:
        container = get_container(request)
        model, chat_model, coding_model, desktop_model, code_engine = await asyncio.gather(
            container.fara.health(),
            container.chat_model.health(),
            container.code_adapter.health(),
            container.desktop_adapter.health(),
            container.code_runtime.engine.health(),
        )
        code_ready = not container.settings.enable_local_workspaces or (
            coding_model["status"] == "ok" and code_engine["status"] == "ok"
        )
        chat_ready = chat_model["status"] == "ok"
        desktop_ready = (
            not container.settings.enable_desktop_control or desktop_model["status"] == "ok"
        )
        return {
            "status": (
                "ok"
                if model["status"] == "ok" and chat_ready and code_ready and desktop_ready
                else "degraded"
            ),
            "database": "ok",
            "model_endpoint": model,
            "chat_model_endpoint": chat_model,
            "coding_model_endpoint": coding_model,
            "code_engine": code_engine,
            "desktop_model_endpoint": desktop_model,
            "desktop_control": {
                "enabled": container.settings.enable_desktop_control,
                "capture_mode": container.settings.desktop_capture_mode,
                "allowed_apps": container.settings.desktop_allowed_apps,
                "require_confirmation": container.settings.desktop_require_confirmation,
                "unattended_mode": container.settings.desktop_unattended_mode,
            },
            "local_workspaces": {
                "enabled": container.settings.enable_local_workspaces,
                "allowed_roots": [str(path) for path in container.settings.workspace_allowed_roots],
            },
        }

    @app.get("/v1/artifacts/{artifact_path:path}", tags=["artifacts"])
    async def get_artifact(artifact_path: str, request: Request) -> FileResponse:
        normalized = artifact_path.replace("\\", "/").strip("/")
        # Authorize the resolved path so aliases containing '..' cannot bypass
        # the private code storage boundary.
        try:
            path = get_container(request).artifacts.resolve_public_path(normalized)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        normalized = path.relative_to(get_container(request).artifacts.root).as_posix()
        if normalized == "blobs" or normalized.startswith("blobs/"):
            raise HTTPException(status_code=404, detail="artifact not found")
        if normalized.startswith("code-runs/"):
            parts = normalized.split("/")
            filename = parts[-1] if parts else ""
            versioned_diff = (
                filename.startswith("diff-")
                and filename.endswith(".patch")
                and filename[5:-6].isdigit()
            )
            if len(parts) != 3 or (filename != "diff.patch" and not versioned_diff):
                raise HTTPException(status_code=404, detail="artifact not found")
            require_local_workspace_request(request, get_container(request).settings)
        elif len(normalized.split("/")) >= 2 and normalized.split("/")[1] == "desktop":
            require_desktop_request(request, get_container(request).settings)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        media_type = {
            ".png": "image/png",
            ".zip": "application/zip",
            ".json": "application/json",
            ".patch": "text/plain; charset=utf-8",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.websocket("/v1/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        container: Container = websocket.app.state.container
        is_code_session = False
        try:
            await container.repository.get_session(session_id)
        except NotFoundError:
            try:
                await container.repository.get_code_run_by_session(session_id)
                is_code_session = True
            except NotFoundError:
                try:
                    await container.repository.get_desktop_run_by_session(session_id)
                    is_code_session = True
                except NotFoundError:
                    await websocket.close(code=4404, reason="session not found")
                    return
        host = websocket.client.host if websocket.client else ""
        if is_code_session and not (
            is_loopback_host(host)
            or (container.settings.environment == "test" and host == "testclient")
        ):
            await websocket.close(code=4403, reason="code events require loopback access")
            return
        try:
            cursor = max(0, int(websocket.query_params.get("after_sequence", "0")))
        except ValueError:
            await websocket.close(code=4400, reason="invalid event cursor")
            return
        queue = await container.event_bus.subscribe(session_id)
        await websocket.accept()

        async def replay() -> None:
            nonlocal cursor
            while True:
                records = await container.repository.list_events(
                    session_id, limit=500, after_sequence=cursor
                )
                if not records:
                    return
                for record in records:
                    if record.sequence <= cursor:
                        continue
                    await websocket.send_json(
                        SessionEvent(
                            event_id=record.event_id,
                            session_id=record.session_id,
                            sequence=record.sequence,
                            event_type=record.event_type,
                            message=record.message,
                            payload=record.payload,
                            created_at=record.created_at,
                        ).model_dump(mode="json")
                    )
                    cursor = record.sequence
                if len(records) < 500:
                    return

        try:
            # Subscribe first, then replay. Events committed during replay are
            # either in the database page or in the queue and are deduplicated by cursor.
            await replay()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    await replay()
                    continue
                if event.sequence <= cursor:
                    continue
                if event.sequence > cursor + 1:
                    await replay()
                    if event.sequence <= cursor:
                        continue
                await websocket.send_json(event.model_dump(mode="json"))
                cursor = event.sequence
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await container.event_bus.unsubscribe(session_id, queue)

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "faraflow.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
