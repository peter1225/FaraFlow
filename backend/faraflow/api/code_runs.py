from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request, status

from faraflow.domain.schemas import (
    CodeAgentCreate,
    CodeDiffView,
    CodeRecoveryDecision,
    CodeRecoveryView,
    CodeRunApply,
    CodeRunCreate,
    CodeRunResume,
    CodeRunView,
    CodeTurnCreate,
    CodeVerificationCreate,
    CodeVerificationProfileView,
    CodeVerificationView,
    SessionEvent,
)

from .dependencies import get_container, require_local_workspace_request

router = APIRouter(prefix="/v1/code-runs", tags=["code"])


@router.post("/maintenance/gc", response_model=Dict[str, Any])
async def collect_code_artifacts(request: Request) -> Dict[str, Any]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_gc.collect()


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


@router.post(
    "/{code_run_id}/agents", response_model=CodeRunView, status_code=status.HTTP_201_CREATED
)
async def create_code_agent(
    code_run_id: str, payload: CodeAgentCreate, request: Request
) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.create_agent(code_run_id, payload)


@router.get("/{code_run_id}/agents", response_model=List[CodeRunView])
async def list_code_agents(code_run_id: str, request: Request) -> List[CodeRunView]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.list_agents(code_run_id)


@router.get("/{code_run_id}/diff", response_model=CodeDiffView)
async def get_code_diff(code_run_id: str, request: Request) -> CodeDiffView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.diff(code_run_id)


@router.post("/{code_run_id}/turns", response_model=CodeRunView)
async def continue_code_run(
    code_run_id: str, payload: CodeTurnCreate, request: Request
) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.continue_run(code_run_id, payload)


@router.post("/{code_run_id}/pause", response_model=CodeRunView)
async def pause_code_run(code_run_id: str, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.pause(code_run_id)


@router.get("/{code_run_id}/recovery", response_model=CodeRecoveryView)
async def get_code_recovery(code_run_id: str, request: Request) -> CodeRecoveryView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.recovery(code_run_id)


@router.post(
    "/{code_run_id}/recovery/tools/{tool_call_id}",
    response_model=CodeRecoveryView,
)
async def decide_code_recovery(
    code_run_id: str,
    tool_call_id: str,
    payload: CodeRecoveryDecision,
    request: Request,
) -> CodeRecoveryView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.decide_recovery(code_run_id, tool_call_id, payload)


@router.post("/{code_run_id}/resume", response_model=CodeRunView)
async def resume_code_run(
    code_run_id: str, payload: CodeRunResume, request: Request
) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.resume(code_run_id, payload)


@router.get(
    "/{code_run_id}/verification-profiles",
    response_model=List[CodeVerificationProfileView],
)
async def list_verification_profiles(
    code_run_id: str, request: Request
) -> List[CodeVerificationProfileView]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    await container.repository.get_code_run(code_run_id)
    return container.code_runs.verification_profiles()


@router.post("/{code_run_id}/verifications", response_model=CodeVerificationView)
async def run_code_verification(
    code_run_id: str, payload: CodeVerificationCreate, request: Request
) -> CodeVerificationView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.verify(code_run_id, payload.profile_id)


@router.get("/{code_run_id}/verifications", response_model=List[CodeVerificationView])
async def list_code_verifications(code_run_id: str, request: Request) -> List[CodeVerificationView]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.verifications(code_run_id)


@router.get("/{code_run_id}/events", response_model=List[SessionEvent])
async def list_code_events(
    code_run_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
) -> List[SessionEvent]:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    record = await container.repository.get_code_run(code_run_id)
    rows = await container.repository.list_events(record.session_id, after_sequence=after_sequence)
    return [
        SessionEvent(
            event_id=item.event_id,
            session_id=item.session_id,
            sequence=item.sequence,
            event_type=item.event_type,
            message=item.message,
            payload=item.payload,
            created_at=item.created_at,
        )
        for item in rows
    ]


@router.post("/{code_run_id}/apply", response_model=CodeRunView)
async def apply_code_run(code_run_id: str, payload: CodeRunApply, request: Request) -> CodeRunView:
    container = get_container(request)
    require_local_workspace_request(request, container.settings)
    return await container.code_runs.apply(code_run_id, payload)


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
