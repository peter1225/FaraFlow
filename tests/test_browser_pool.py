from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from faraflow.browser.playwright_runner import BrowserPool


class FakeTracing:
    async def stop(self, *, path: str) -> None:
        del path


class FailingCloseContext:
    def __init__(self) -> None:
        self.tracing = FakeTracing()

    async def storage_state(self, *, path: str) -> None:
        del path

    async def close(self) -> None:
        raise RuntimeError("context close failed")


@pytest.mark.asyncio
async def test_close_session_releases_capacity_when_context_close_fails(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        max_concurrent_sessions=1,
        browser_state_root=tmp_path / "browser-state",
    )
    artifacts = SimpleNamespace(
        session_dir=lambda session_id: tmp_path / "artifacts" / session_id
    )
    pool = BrowserPool(settings, artifacts)  # type: ignore[arg-type]
    pool._sessions["sess_1"] = SimpleNamespace(context=FailingCloseContext())
    pool._capacity = asyncio.Semaphore(0)

    with pytest.raises(RuntimeError, match="context close failed"):
        await pool.close_session("sess_1")

    await asyncio.wait_for(pool._capacity.acquire(), timeout=0.1)
