from typing import List

from fastapi import APIRouter, HTTPException, Query, Request, status

from faraflow.domain.schemas import (
    WorkspaceCreate,
    WorkspaceDirectorySelection,
    WorkspaceFileView,
    WorkspaceTreeEntry,
    WorkspaceView,
)

from .dependencies import get_container, require_local_workspace_request

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


@router.post("/pick-directory", response_model=WorkspaceDirectorySelection)
async def pick_workspace_directory(request: Request) -> WorkspaceDirectorySelection:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    if request.headers.get("x-faraflow-local-action") != "pick-directory":
        raise HTTPException(status_code=403, detail="missing local folder picker header")
    return await container.workspaces.pick_directory()


@router.post("", response_model=WorkspaceView, status_code=status.HTTP_201_CREATED)
async def create_workspace(payload: WorkspaceCreate, request: Request) -> WorkspaceView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.workspaces.register(payload)


@router.get("", response_model=List[WorkspaceView])
async def list_workspaces(request: Request) -> List[WorkspaceView]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.workspaces.list()


@router.get("/{workspace_id}", response_model=WorkspaceView)
async def get_workspace(workspace_id: str, request: Request) -> WorkspaceView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.workspaces.get(workspace_id)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(workspace_id: str, request: Request) -> None:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    await container.workspaces.deactivate(workspace_id)


@router.get("/{workspace_id}/tree", response_model=List[WorkspaceTreeEntry])
async def workspace_tree(
    workspace_id: str,
    request: Request,
    path: str = Query(default="."),
) -> List[WorkspaceTreeEntry]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.workspaces.tree(workspace_id, path)


@router.get("/{workspace_id}/files", response_model=WorkspaceFileView)
async def workspace_file(
    workspace_id: str,
    request: Request,
    path: str = Query(...),
    start: int = Query(default=1, ge=1),
    end: int = Query(default=200, ge=1),
) -> WorkspaceFileView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.workspaces.read_file(workspace_id, path, start, end)
