from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request, status

from faraflow.domain.schemas import (
    ApprovalDecision,
    ApprovalView,
    SessionEvent,
    SkillManifest,
    TaskCreate,
    TaskView,
    UserResponse,
)

from .dependencies import get_container

router = APIRouter(prefix="/v1")


@router.post("/tasks", response_model=TaskView, status_code=status.HTTP_201_CREATED, tags=["tasks"])
async def create_task(payload: TaskCreate, request: Request) -> TaskView:
    return await get_container(request).tasks.create(payload)


@router.get("/tasks", response_model=List[TaskView], tags=["tasks"])
async def list_tasks(
    request: Request,
    tenant_id: Optional[str] = Query(default=None),
) -> List[TaskView]:
    return await get_container(request).tasks.list(tenant_id)


@router.get("/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
async def get_task(task_id: str, request: Request) -> TaskView:
    return await get_container(request).tasks.get(task_id)


@router.post("/tasks/{task_id}/start", response_model=TaskView, tags=["tasks"])
async def start_task(task_id: str, request: Request) -> TaskView:
    return await get_container(request).tasks.start(task_id)


@router.post("/tasks/{task_id}/pause", response_model=TaskView, tags=["tasks"])
async def pause_task(task_id: str, request: Request) -> TaskView:
    return await get_container(request).tasks.pause(task_id)


@router.post("/tasks/{task_id}/terminate", response_model=TaskView, tags=["tasks"])
async def terminate_task(task_id: str, request: Request) -> TaskView:
    return await get_container(request).tasks.terminate(task_id)


@router.post("/tasks/{task_id}/rerun", response_model=TaskView, tags=["tasks"])
async def rerun_task(task_id: str, request: Request) -> TaskView:
    return await get_container(request).tasks.rerun(task_id)


@router.post("/tasks/{task_id}/respond", response_model=TaskView, tags=["tasks"])
async def respond_to_task(task_id: str, payload: UserResponse, request: Request) -> TaskView:
    return await get_container(request).tasks.respond(task_id, payload)


@router.get("/tasks/{task_id}/events", response_model=List[SessionEvent], tags=["events"])
async def list_task_events(task_id: str, request: Request) -> List[SessionEvent]:
    return await get_container(request).tasks.events(task_id)


@router.get(
    "/tasks/{task_id}/actions",
    response_model=List[Dict[str, Any]],
    tags=["events"],
)
async def list_task_actions(task_id: str, request: Request) -> List[Dict[str, Any]]:
    container = get_container(request)
    task = await container.repository.get_task(task_id)
    records = await container.repository.list_actions(task.session_id)
    return [serialize_action(item) for item in records]


@router.get(
    "/tasks/{task_id}/approvals",
    response_model=List[ApprovalView],
    tags=["approvals"],
)
async def list_task_approvals(task_id: str, request: Request) -> List[ApprovalView]:
    return await get_container(request).tasks.approvals(task_id)


@router.post(
    "/tasks/{task_id}/approvals/{approval_id}",
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


@router.get("/skills", response_model=List[SkillManifest], tags=["skills"])
async def list_skills(
    request: Request,
    category: Optional[str] = Query(default=None),
    enabled: Optional[bool] = Query(default=True),
) -> List[SkillManifest]:
    return await get_container(request).tasks.skills(category, enabled)


@router.post(
    "/skills",
    response_model=SkillManifest,
    status_code=status.HTTP_201_CREATED,
    tags=["skills"],
)
async def register_skill(payload: SkillManifest, request: Request) -> SkillManifest:
    return await get_container(request).tasks.register_skill(payload)


def serialize_action(record: Any) -> Dict[str, Any]:
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
