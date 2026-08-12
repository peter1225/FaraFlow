import json
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from faraflow.domain.schemas import (
    ChatCreate,
    ChatMessageCreate,
    ChatReply,
    ChatSummary,
    ChatView,
)

from .dependencies import get_container, require_desktop_request, require_local_workspace_request

router = APIRouter(prefix="/v1/chats", tags=["chat"])


@router.post("", response_model=ChatView, status_code=status.HTTP_201_CREATED)
async def create_chat(payload: ChatCreate, request: Request) -> ChatView:
    container = get_container(request)
    if payload.workspace_id is not None:
        require_local_workspace_request(request, container.settings)
    return await container.chat.create(payload)


@router.get("", response_model=List[ChatSummary])
async def list_chats(
    request: Request,
    tenant_id: Optional[str] = Query(default=None),
) -> List[ChatSummary]:
    return await get_container(request).chat.list(tenant_id)


@router.get("/{chat_id}", response_model=ChatView)
async def get_chat(chat_id: str, request: Request) -> ChatView:
    return await get_container(request).chat.get(chat_id)


@router.post("/{chat_id}/messages", response_model=ChatReply)
async def send_chat_message(
    chat_id: str,
    payload: ChatMessageCreate,
    request: Request,
) -> ChatReply:
    container = get_container(request)
    chat = await container.repository.get_chat(chat_id)
    if payload.requested_mode == "desktop":
        require_desktop_request(request, container.settings)
    if payload.requested_mode == "code" or (
        chat.workspace_id is not None and payload.requested_mode != "desktop"
    ):
        if chat.workspace_id is None:
            raise HTTPException(status_code=422, detail="code mode requires a workspace")
        require_local_workspace_request(request, container.settings)
    return await container.chat.respond(chat_id, payload)


@router.post("/{chat_id}/messages/stream")
async def stream_chat_message(
    chat_id: str,
    payload: ChatMessageCreate,
    request: Request,
) -> StreamingResponse:
    container = get_container(request)
    chat = await container.repository.get_chat(chat_id)
    if payload.requested_mode == "desktop":
        require_desktop_request(request, container.settings)
    if payload.requested_mode == "code" or (
        chat.workspace_id is not None and payload.requested_mode != "desktop"
    ):
        if chat.workspace_id is None:
            raise HTTPException(status_code=422, detail="code mode requires a workspace")
        require_local_workspace_request(request, container.settings)

    async def events() -> AsyncIterator[str]:
        async for event in container.chat.respond_stream(chat_id, payload):
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
