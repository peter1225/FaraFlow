from typing import List, Optional

from fastapi import APIRouter, Query, Request, status

from faraflow.domain.schemas import (
    DesktopApprovalDecision,
    DesktopApprovalView,
    DesktopRunCreate,
    DesktopRunView,
    DesktopWindowSelection,
    DesktopWindowView,
)

from .dependencies import get_container, require_desktop_request

router = APIRouter(prefix="/v1/desktop-runs", tags=["desktop"])


@router.post("", response_model=DesktopRunView, status_code=status.HTTP_201_CREATED)
async def create_desktop_run(payload: DesktopRunCreate, request: Request) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.create(payload)


@router.get("", response_model=List[DesktopRunView])
async def list_desktop_runs(
    request: Request,
    chat_id: Optional[str] = Query(default=None),
) -> List[DesktopRunView]:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.list(chat_id)


@router.get("/windows", response_model=List[DesktopWindowView])
async def list_windows(request: Request) -> List[DesktopWindowView]:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.windows()


@router.get("/{desktop_run_id}", response_model=DesktopRunView)
async def get_desktop_run(desktop_run_id: str, request: Request) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.get(desktop_run_id)


@router.post("/{desktop_run_id}/select-window", response_model=DesktopRunView)
async def select_window(
    desktop_run_id: str, payload: DesktopWindowSelection, request: Request
) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.select_window(desktop_run_id, payload.window_id)


@router.post("/{desktop_run_id}/start", response_model=DesktopRunView)
async def start_desktop_run(desktop_run_id: str, request: Request) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.start(desktop_run_id)


@router.post("/{desktop_run_id}/pause", response_model=DesktopRunView)
async def pause_desktop_run(desktop_run_id: str, request: Request) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.pause(desktop_run_id)


@router.post("/{desktop_run_id}/terminate", response_model=DesktopRunView)
async def terminate_desktop_run(desktop_run_id: str, request: Request) -> DesktopRunView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.terminate(desktop_run_id)


@router.get("/{desktop_run_id}/approvals", response_model=List[DesktopApprovalView])
async def list_desktop_approvals(
    desktop_run_id: str, request: Request
) -> List[DesktopApprovalView]:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.approvals(desktop_run_id)


@router.post(
    "/{desktop_run_id}/approvals/{approval_id}", response_model=DesktopApprovalView
)
async def decide_desktop_approval(
    desktop_run_id: str,
    approval_id: str,
    payload: DesktopApprovalDecision,
    request: Request,
) -> DesktopApprovalView:
    container = get_container(request)
    require_desktop_request(request, container.settings)
    return await container.desktop_runs.decide_approval(desktop_run_id, approval_id, payload)
