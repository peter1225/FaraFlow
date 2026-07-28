import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, cast

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse

from faraflow.browser.playwright_runner import BrowserPool
from faraflow.config import Settings, get_settings
from faraflow.domain.schemas import (
    ApprovalDecision,
    ApprovalView,
    SessionEvent,
    SkillManifest,
    TaskCreate,
    TaskView,
    UserResponse,
)
from faraflow.infra.artifacts import ArtifactStore
from faraflow.infra.database import ActionRecord, Database
from faraflow.infra.events import EventBus
from faraflow.infra.repository import ConflictError, NotFoundError, Repository
from faraflow.model.fara_adapter import FaraAdapter
from faraflow.runtime.agent_runtime import AgentRuntime
from faraflow.runtime.task_service import TaskService

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    database: Database
    repository: Repository
    artifacts: ArtifactStore
    event_bus: EventBus
    browser_pool: BrowserPool
    fara: FaraAdapter
    runtime: AgentRuntime
    tasks: TaskService


def build_container(settings: Settings) -> Container:
    database = Database(settings.database_url)
    repository = Repository(database.session_factory)
    artifacts = ArtifactStore(settings.artifact_root)
    event_bus = EventBus()
    browser_pool = BrowserPool(settings, artifacts)
    fara = FaraAdapter(settings)
    runtime = AgentRuntime(repository, browser_pool, fara, event_bus)
    tasks = TaskService(repository, runtime)
    return Container(
        settings=settings,
        database=database,
        repository=repository,
        artifacts=artifacts,
        event_bus=event_bus,
        browser_pool=browser_pool,
        fara=fara,
        runtime=runtime,
        tasks=tasks,
    )


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(active_settings)
        app.state.container = container
        await container.database.create_schema()
        await container.tasks.seed_skills()
        logger.info("FaraFlow API started")
        try:
            yield
        finally:
            await container.runtime.close()
            await container.browser_pool.close()
            await container.fara.close()
            await container.database.dispose()

    app = FastAPI(
        title="FaraFlow API",
        version="0.1.0",
        description="Enterprise browser automation platform powered by Microsoft Fara1.5",
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

    @app.get("/health/live", tags=["health"])
    async def health_live() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def health_ready(request: Request) -> Dict[str, Any]:
        container = get_container(request)
        model = await container.fara.health()
        return {
            "status": "ok" if model["status"] == "ok" else "degraded",
            "database": "ok",
            "model_endpoint": model,
        }

    @app.post(
        "/v1/tasks",
        response_model=TaskView,
        status_code=status.HTTP_201_CREATED,
        tags=["tasks"],
    )
    async def create_task(payload: TaskCreate, request: Request) -> TaskView:
        return await get_container(request).tasks.create(payload)

    @app.get("/v1/tasks", response_model=List[TaskView], tags=["tasks"])
    async def list_tasks(
        request: Request,
        tenant_id: Optional[str] = Query(default=None),
    ) -> List[TaskView]:
        return await get_container(request).tasks.list(tenant_id)

    @app.get("/v1/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
    async def get_task(task_id: str, request: Request) -> TaskView:
        return await get_container(request).tasks.get(task_id)

    @app.post("/v1/tasks/{task_id}/start", response_model=TaskView, tags=["tasks"])
    async def start_task(task_id: str, request: Request) -> TaskView:
        return await get_container(request).tasks.start(task_id)

    @app.post("/v1/tasks/{task_id}/pause", response_model=TaskView, tags=["tasks"])
    async def pause_task(task_id: str, request: Request) -> TaskView:
        return await get_container(request).tasks.pause(task_id)

    @app.post("/v1/tasks/{task_id}/terminate", response_model=TaskView, tags=["tasks"])
    async def terminate_task(task_id: str, request: Request) -> TaskView:
        return await get_container(request).tasks.terminate(task_id)

    @app.post("/v1/tasks/{task_id}/respond", response_model=TaskView, tags=["tasks"])
    async def respond_to_task(task_id: str, payload: UserResponse, request: Request) -> TaskView:
        return await get_container(request).tasks.respond(task_id, payload)

    @app.get(
        "/v1/tasks/{task_id}/events",
        response_model=List[SessionEvent],
        tags=["events"],
    )
    async def list_task_events(task_id: str, request: Request) -> List[SessionEvent]:
        return await get_container(request).tasks.events(task_id)

    @app.get(
        "/v1/tasks/{task_id}/actions",
        response_model=List[Dict[str, Any]],
        tags=["events"],
    )
    async def list_task_actions(task_id: str, request: Request) -> List[Dict[str, Any]]:
        container = get_container(request)
        task = await container.repository.get_task(task_id)
        records = await container.repository.list_actions(task.session_id)
        return [serialize_action(item) for item in records]

    @app.get(
        "/v1/tasks/{task_id}/approvals",
        response_model=List[ApprovalView],
        tags=["approvals"],
    )
    async def list_task_approvals(task_id: str, request: Request) -> List[ApprovalView]:
        return await get_container(request).tasks.approvals(task_id)

    @app.post(
        "/v1/tasks/{task_id}/approvals/{approval_id}",
        response_model=ApprovalView,
        tags=["approvals"],
    )
    async def decide_task_approval(
        task_id: str,
        approval_id: str,
        payload: ApprovalDecision,
        request: Request,
    ) -> ApprovalView:
        return await get_container(request).tasks.decide_approval(task_id, approval_id, payload)

    @app.get("/v1/skills", response_model=List[SkillManifest], tags=["skills"])
    async def list_skills(
        request: Request,
        category: Optional[str] = Query(default=None),
        enabled: Optional[bool] = Query(default=True),
    ) -> List[SkillManifest]:
        return await get_container(request).tasks.skills(category, enabled)

    @app.post(
        "/v1/skills",
        response_model=SkillManifest,
        status_code=status.HTTP_201_CREATED,
        tags=["skills"],
    )
    async def register_skill(payload: SkillManifest, request: Request) -> SkillManifest:
        return await get_container(request).tasks.register_skill(payload)

    @app.get("/v1/artifacts/{artifact_path:path}", tags=["artifacts"])
    async def get_artifact(artifact_path: str, request: Request) -> FileResponse:
        try:
            path = get_container(request).artifacts.resolve_public_path(artifact_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        media_type = {
            ".png": "image/png",
            ".zip": "application/zip",
            ".json": "application/json",
        }.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.websocket("/v1/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        container: Container = websocket.app.state.container
        try:
            await container.repository.get_session(session_id)
        except NotFoundError:
            await websocket.close(code=4404, reason="session not found")
            return
        await websocket.accept()
        existing = await container.repository.list_events(session_id)
        for record in existing:
            await websocket.send_json(
                SessionEvent(
                    event_id=record.event_id,
                    session_id=record.session_id,
                    event_type=record.event_type,
                    message=record.message,
                    payload=record.payload,
                    created_at=record.created_at,
                ).model_dump(mode="json")
            )
        queue = await container.event_bus.subscribe(session_id)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            await container.event_bus.unsubscribe(session_id, queue)

    return app


def serialize_action(record: ActionRecord) -> Dict[str, Any]:
    return {
        "action_id": record.action_id,
        "task_id": record.task_id,
        "session_id": record.session_id,
        "step_no": record.step_no,
        "action_type": record.action_type,
        "action_parameters": record.action_parameters,
        "page_url": record.page_url,
        "executor_type": record.executor_type,
        "screenshot_before": record.screenshot_before,
        "screenshot_after": record.screenshot_after,
        "execution_result": record.execution_result,
        "verification_result": record.verification_result,
        "created_at": record.created_at,
    }


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
