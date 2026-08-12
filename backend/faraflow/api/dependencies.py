import ipaddress
from typing import Any, cast

from fastapi import HTTPException, Request

from faraflow.config import Settings


def get_container(request: Request) -> Any:
    return cast(Any, request.app.state.container)


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_local_workspace_request(request: Request, settings: Settings) -> None:
    if not settings.enable_local_workspaces:
        raise HTTPException(status_code=409, detail="local workspaces are disabled")
    host = request.client.host if request.client else ""
    if settings.environment == "test" and host == "testclient":
        return
    if is_loopback_host(host):
        return
    raise HTTPException(status_code=403, detail="workspace APIs only accept loopback requests")


def require_desktop_request(request: Request, settings: Settings) -> None:
    if not settings.enable_desktop_control:
        raise HTTPException(status_code=409, detail="desktop control is disabled")
    host = request.client.host if request.client else ""
    if settings.environment == "test" and host == "testclient":
        return
    if is_loopback_host(host):
        return
    raise HTTPException(status_code=403, detail="desktop APIs only accept loopback requests")
