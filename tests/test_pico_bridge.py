"""Real subprocess/RPC tests; no Pico installation or model required."""

import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from faraflow.api.main import create_app
from faraflow.code import pico_bridge
from faraflow.code.engine import EngineRequest
from faraflow.code.pico_bridge import PicoCodeEngine
from faraflow.code.registry import ToolRegistry
from faraflow.code.tools import CodeToolExecutor
from faraflow.config import Settings
from fastapi.testclient import TestClient
from test_code_workspace import local_settings, wait_for_status

PREAMBLE = '''
import json, sys, time
def send(**frame):
    print(json.dumps(dict(jsonrpc="2.0", **frame)), flush=True)
def read():
    return json.loads(sys.stdin.readline())
send(method="ready", params={"protocol": 1, "version": "0.1.7"})
if "--check" in sys.argv:
    sys.exit(0)
request = read()["params"]
'''
FAKE_WORKER = PREAMBLE + '''
calls = [
    ("read_file", {"path": "../outside.txt"}),
    ("read_file", {"path": ".env"}),
    ("patch_file", {"path": "README.md", "old_text": "Original", "new_text": "Updated"}),
    ("read_file", {"path": "README.md"}),
    ("patch_file", {"path": "README.md", "old_text": "Original", "new_text": "Updated"}),
    ("read_file", {"path": "README.md"}),
]
for index, (name, arguments) in enumerate(calls):
    send(id=str(index), method="tool.execute", params={"name": name, "arguments": arguments})
    response = read()["result"]
    assert response["is_error"] == (index < 3), response
send(method="event", params={"kind": "usage", "total_tokens": 12})
send(method="event", params={"kind": "text", "text": "Updated README"})
send(id="run", result={"status": "completed", "summary": "Updated README"})
time.sleep(60)
'''


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / "fake_worker.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(pico_bridge, "WORKER", path)
    return path


def test_pico_review_apply_revert_and_conflict(tmp_path, worker):
    root = tmp_path / "project"
    root.mkdir()
    readme = root / "README.md"
    readme.write_text("# Original\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    settings = local_settings(tmp_path, root)
    settings.code_engine = "pico"
    settings.pico_python = sys.executable
    settings.pico_state_root = tmp_path / "private-pico"
    app = create_app(settings)
    with TestClient(app) as client:
        workspace = client.post("/v1/workspaces", json={
            "name": "Pico", "root_path": str(root),
        }).json()
        created = client.post("/v1/code-runs", json={
            "workspace_id": workspace["workspace_id"], "instruction": "Update README",
        })
        assert created.status_code == 201, created.text
        run_id = created.json()["code_run_id"]
        record = wait_for_status(client, run_id, "REVIEW_REQUIRED")
        assert readme.read_text("utf-8") == "# Original\n"
        assert [call["status"] for call in record["tool_calls"]] == [
            "error", "error", "error", "ok", "ok", "ok",
        ]
        assert "+# Updated" in client.get(f"/v1/code-runs/{run_id}/diff").json()["diff"]
        events = client.get(f"/v1/code-runs/{run_id}/events").json()
        assert any(event["event_type"] == "code.engine.ready" for event in events)
        assert any(event["event_type"] == "code.usage" for event in events)
        assert "TOKEN=secret" not in json.dumps(events)
        assert (settings.pico_state_root / run_id).is_dir()
        readme.write_text("# User edit\n", encoding="utf-8")
        assert client.post(f"/v1/code-runs/{run_id}/apply").status_code == 409
        readme.write_text("# Original\n", encoding="utf-8")
        assert client.post(f"/v1/code-runs/{run_id}/apply").status_code == 200
        assert readme.read_text("utf-8") == "# Updated\n"
        assert client.post(f"/v1/code-runs/{run_id}/revert").status_code == 200
        assert readme.read_text("utf-8") == "# Original\n"


def bridge_settings(tmp_path):
    return Settings(
        _env_file=None, pico_python=sys.executable, pico_state_root=tmp_path / "state",
        artifact_root=tmp_path / "artifacts", code_base_url="http://test/v1", code_model="test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", [
    'send(id="bad", method="tool.execute", params={"name":"exec", "arguments":{}})',
    'print("not json", flush=True)',
    'send(id="run", result={"status":"interrupted", "summary":"partial"})',
    'sys.exit(1)',
])
async def test_protocol_failure_never_executes_tools(tmp_path, worker, attack):
    worker.write_text(PREAMBLE + attack, encoding="utf-8")
    execute = AsyncMock()
    with pytest.raises((RuntimeError, ValueError)):
        await PicoCodeEngine(bridge_settings(tmp_path)).run(
            EngineRequest("run", "task", tmp_path / "isolated", ""), execute, AsyncMock(),
        )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_reaps_worker(tmp_path, worker, monkeypatch):
    worker.write_text(PREAMBLE + 'time.sleep(60)', encoding="utf-8")
    processes = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    ready = asyncio.Event()

    async def emit(*args):
        ready.set()

    task = asyncio.create_task(PicoCodeEngine(bridge_settings(tmp_path)).run(
        EngineRequest("run", "task", tmp_path / "isolated", ""), AsyncMock(), emit,
    ))
    await asyncio.wait_for(ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_missing_worker_does_not_fallback(tmp_path):
    engine = PicoCodeEngine(Settings(_env_file=None))
    assert (await engine.health())["status"] == "unavailable"
    with pytest.raises(RuntimeError):
        await engine.run(EngineRequest("run", "task", tmp_path, ""), AsyncMock(), AsyncMock())


def test_worker_schema_matches_host_allowlist():
    path = Path(pico_bridge.__file__).with_name("tool_schemas.json")
    specs = json.loads(path.read_text("utf-8"))
    assert {spec["name"] for spec in specs} == set(ToolRegistry.default().names)


@pytest.mark.asyncio
async def test_startup_timeout_reaps_worker(tmp_path, worker):
    worker.write_text("import time; time.sleep(60)", encoding="utf-8")
    settings = bridge_settings(tmp_path)
    settings.pico_startup_timeout_seconds = 0.1
    with pytest.raises(asyncio.TimeoutError):
        await PicoCodeEngine(settings).run(
            EngineRequest("run", "task", tmp_path / "isolated", ""), AsyncMock(), AsyncMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["artifacts", "source", "isolated"])
async def test_private_state_cannot_be_inside_public_or_code_roots(tmp_path, location):
    settings = bridge_settings(tmp_path)
    settings.pico_state_root = tmp_path / location / "pico"
    with pytest.raises(ValueError, match="STATE_ROOT"):
        await PicoCodeEngine(settings).run(
            EngineRequest("run", "task", tmp_path / "isolated", "", tmp_path / "source"),
            AsyncMock(), AsyncMock(),
        )


@pytest.mark.asyncio
async def test_worker_health_is_checked_and_cached(tmp_path, worker, monkeypatch):
    engine = PicoCodeEngine(bridge_settings(tmp_path))
    assert (await engine.health())["status"] == "ok"
    create = AsyncMock(side_effect=AssertionError("must use cached health"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert (await engine.health())["status"] == "ok"
    create.assert_not_awaited()


def test_discard_waits_for_inflight_write_before_cleanup(tmp_path, worker, monkeypatch):
    worker.write_text(PREAMBLE + '''
send(id="write", method="tool.execute", params={
    "name":"create_file", "arguments":{"path":"new.txt", "content":"staged"}})
read()
time.sleep(60)
''', encoding="utf-8")
    started, release, cancelling = threading.Event(), threading.Event(), threading.Event()
    dispatch = CodeToolExecutor._dispatch

    def slow_write(self, name, arguments):
        started.set()
        assert release.wait(10)
        return dispatch(self, name, arguments)

    monkeypatch.setattr(CodeToolExecutor, "_dispatch", slow_write)
    root = tmp_path / "project"
    root.mkdir()
    settings = local_settings(tmp_path, root)
    settings.code_engine = "pico"
    settings.pico_python = sys.executable
    settings.pico_state_root = tmp_path / "private-pico"
    app = create_app(settings)
    with TestClient(app) as client:
        original_cancel = app.state.container.code_runtime.cancel

        async def cancel(run_id):
            cancelling.set()
            await original_cancel(run_id)

        app.state.container.code_runtime.cancel = cancel
        workspace = client.post("/v1/workspaces", json={
            "name": "Pico", "root_path": str(root),
        }).json()
        run_id = client.post("/v1/code-runs", json={
            "workspace_id": workspace["workspace_id"], "instruction": "Create file",
        }).json()["code_run_id"]
        try:
            assert started.wait(5)
            with ThreadPoolExecutor(max_workers=1) as pool:
                stop = pool.submit(client.post, f"/v1/code-runs/{run_id}/discard")
                try:
                    assert cancelling.wait(5)
                    assert (settings.code_work_root / run_id).is_dir()
                    assert not stop.done()
                finally:
                    release.set()
                response = stop.result(timeout=10)
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "DISCARDED"
                assert response.json()["tool_calls"][0]["status"] == "ok"
            assert not (settings.code_work_root / run_id).exists()
            assert not (root / "new.txt").exists()
        finally:
            release.set()
