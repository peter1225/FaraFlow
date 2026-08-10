from typing import List, Optional

from fastapi import APIRouter, Query, Request, status

from faraflow.domain.schemas import CodeDiffView, CodeRunCreate, CodeRunView, SessionEvent

from .dependencies import get_container, require_local_workspace_request

router = APIRouter(prefix="/v1/code-runs", tags=["code"])


@router.post("", response_model=CodeRunView, status_code=status.HTTP_201_CREATED)
async def create_code_run(payload: CodeRunCreate, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.create(payload)


@router.get("", response_model=List[CodeRunView])
async def list_code_runs(
    request: Request,
    workspace_id: Optional[str] = Query(default=None),
) -> List[CodeRunView]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.list(workspace_id)


@router.get("/{code_run_id}", response_model=CodeRunView)
async def get_code_run(code_run_id: str, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.get(code_run_id)


@router.get("/{code_run_id}/diff", response_model=CodeDiffView)
async def get_code_diff(code_run_id: str, request: Request) -> CodeDiffView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.diff(code_run_id)


@router.get("/{code_run_id}/events", response_model=List[SessionEvent])
async def list_code_events(code_run_id: str, request: Request) -> List[SessionEvent]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    record = await container.repository.get_code_run(code_run_id)
    rows = await container.repository.list_events(record.session_id)
    return [
        SessionEvent(
            event_id=item.event_id,
            session_id=item.session_id,
            event_type=item.event_type,
            message=item.message,
            payload=item.payload,
            created_at=item.created_at,
        )
        for item in rows
    ]


@router.post("/{code_run_id}/apply", response_model=CodeRunView)
async def apply_code_run(code_run_id: str, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.apply(code_run_id)


@router.post("/{code_run_id}/revert", response_model=CodeRunView)
async def revert_code_run(code_run_id: str, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.revert(code_run_id)


@router.post("/{code_run_id}/discard", response_model=CodeRunView)
async def discard_code_run(code_run_id: str, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.discard(code_run_id)
