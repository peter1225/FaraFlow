from __future__ import annotations

import json
import time
from pathlib import Path
from subprocess import run
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from faraflow.api.main import create_app
from faraflow.code.adapter import CodeAdapter
from faraflow.code.protocol import CodeDecision
from faraflow.code.tools import CodeToolExecutor
from faraflow.config import Settings
from faraflow.domain.enums import CodeRunStatus
from faraflow.domain.schemas import WorkspaceCreate, WorkspaceDirectorySelection
from faraflow.infra.database import Database
from faraflow.infra.repository import Repository
from faraflow.workspace.run_store import CodeRunStore, file_hash
from faraflow.workspace.service import WorkspaceService
from fastapi.testclient import TestClient


def local_settings(tmp_path: Path, workspace_root: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        api_host="127.0.0.1",
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
        code_work_root=tmp_path / "code-work",
        enable_local_workspaces=True,
        workspace_allowed_roots=[workspace_root],
        code_base_url="http://coding.invalid/v1",
        code_model="test-coder",
    )


def wait_for_status(client: TestClient, code_run_id: str, status: str) -> dict:
    deadline = time.monotonic() + 10
    body = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/code-runs/{code_run_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] == status:
            return body
        if body["status"] == "FAILED":
            raise AssertionError(body)
        time.sleep(0.05)
    raise AssertionError(f"code run did not reach {status}: {body}")


def wait_for_review(client: TestClient, code_run_id: str, revision: int) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/v1/code-runs/{code_run_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] == "REVIEW_REQUIRED" and body["review_revision"] == revision:
            return body
        if body["status"] == "FAILED":
            raise AssertionError(body)
        time.sleep(0.05)
    raise AssertionError(f"code run did not reach review revision {revision}")


def code_decisions(old_title: str, new_title: str) -> list[CodeDecision]:
    return [
        CodeDecision(
            kind="tool",
            raw_response=(
                '<tool_call>{"name":"read_file","args":{"path":"README.md",'
                '"start":1,"end":20}}</tool_call>'
            ),
            tool_name="read_file",
            arguments={"path": "README.md", "start": 1, "end": 20},
        ),
        CodeDecision(
            kind="tool",
            raw_response=(
                '<tool_call>{"name":"patch_file","args":{"path":"README.md",'
                f'"old_text":"{old_title}","new_text":"{new_title}"}}</tool_call>'
            ),
            tool_name="patch_file",
            arguments={
                "path": "README.md",
                "old_text": old_title,
                "new_text": new_title,
            },
        ),
        CodeDecision(
            kind="final",
            raw_response="<final>README title updated.</final>",
            answer="README title updated.",
        ),
    ]


def test_workspace_code_run_review_apply_revert_and_conflict(tmp_path: Path) -> None:
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    readme = workspace_root / "README.md"
    readme.write_text("# Original\n\nBody\n", encoding="utf-8")
    (workspace_root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (workspace_root / ".env.example").write_text("TOKEN=", encoding="utf-8")
    (workspace_root / "node_modules").mkdir()
    (workspace_root / "node_modules" / "hidden.js").write_text("x", encoding="utf-8")

    app = create_app(local_settings(tmp_path, workspace_root))
    with TestClient(app) as client:
        app.state.container.workspaces.pick_directory = AsyncMock(
            return_value=WorkspaceDirectorySelection(
                path=str(workspace_root), name=workspace_root.name
            )
        )
        missing_header = client.post("/v1/workspaces/pick-directory")
        assert missing_header.status_code == 403
        picked = client.post(
            "/v1/workspaces/pick-directory",
            headers={"X-FaraFlow-Local-Action": "pick-directory"},
        )
        assert picked.status_code == 200, picked.text
        assert picked.json() == {
            "path": str(workspace_root),
            "name": workspace_root.name,
        }
        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=code_decisions("# Original", "# Updated")
        )
        created = client.post(
            "/v1/workspaces",
            json={"name": "Example", "root_path": str(workspace_root)},
        )
        assert created.status_code == 201, created.text
        workspace = created.json()
        assert workspace["repository_kind"] == "directory"

        tree = client.get(f"/v1/workspaces/{workspace['workspace_id']}/tree")
        assert tree.status_code == 200
        names = {item["name"] for item in tree.json()}
        assert "README.md" in names
        assert ".env.example" in names
        assert ".env" not in names
        assert "node_modules" not in names

        chat = client.post(
            "/v1/chats",
            json={"title": "Code", "workspace_id": workspace["workspace_id"]},
        )
        assert chat.status_code == 201, chat.text
        reply = client.post(
            f"/v1/chats/{chat.json()['chat_id']}/messages",
            json={"content": "Update the README title", "requested_mode": "code"},
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["route"] == "code"
        code_run_id = reply.json()["code_run"]["code_run_id"]

        review = wait_for_status(client, code_run_id, "REVIEW_REQUIRED")
        assert review["changed_paths"] == ["README.md"]
        assert readme.read_text(encoding="utf-8").startswith("# Original")
        blocked_delete = client.delete(f"/v1/workspaces/{workspace['workspace_id']}")
        assert blocked_delete.status_code == 409
        patch = client.get(f"/v1/code-runs/{code_run_id}/diff").json()["diff"]
        assert "-# Original" in patch
        assert "+# Updated" in patch
        event_rows = client.get(f"/v1/code-runs/{code_run_id}/events").json()
        assert [item["sequence"] for item in event_rows] == list(
            range(1, len(event_rows) + 1)
        )
        cursor = event_rows[-2]["sequence"]
        after = client.get(
            f"/v1/code-runs/{code_run_id}/events?after_sequence={cursor}"
        ).json()
        assert [item["sequence"] for item in after] == [event_rows[-1]["sequence"]]
        with client.websocket_connect(
            f"/v1/sessions/{review['session_id']}/events?after_sequence={cursor}"
        ) as socket:
            replayed = socket.receive_json()
        assert replayed["sequence"] == event_rows[-1]["sequence"]
        patch_artifact = client.get(f"/v1/artifacts/code-runs/{code_run_id}/diff.patch")
        assert patch_artifact.status_code == 200
        private_baseline = client.get(
            f"/v1/artifacts/code-runs/{code_run_id}/baseline/README.md"
        )
        assert private_baseline.status_code == 404

        applied = client.post(
            f"/v1/code-runs/{code_run_id}/apply",
            json={"review_revision": review["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "APPLIED"
        assert readme.read_text(encoding="utf-8").startswith("# Updated")

        reverted = client.post(f"/v1/code-runs/{code_run_id}/revert")
        assert reverted.status_code == 200, reverted.text
        assert reverted.json()["status"] == "REVERTED"
        assert readme.read_text(encoding="utf-8").startswith("# Original")

        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=code_decisions("# Original", "# Agent")
        )
        conflict_run = client.post(
            "/v1/code-runs",
            json={
                "workspace_id": workspace["workspace_id"],
                "instruction": "Change the title again",
            },
        )
        assert conflict_run.status_code == 201, conflict_run.text
        conflict_id = conflict_run.json()["code_run_id"]
        conflict_review = wait_for_status(client, conflict_id, "REVIEW_REQUIRED")
        readme.write_text("# Human edit\n", encoding="utf-8")
        conflict = client.post(
            f"/v1/code-runs/{conflict_id}/apply",
            json={"review_revision": conflict_review["review_revision"]},
        )
        assert conflict.status_code == 409
        assert readme.read_text(encoding="utf-8") == "# Human edit\n"
        discarded = client.post(f"/v1/code-runs/{conflict_id}/discard")
        assert discarded.status_code == 200
        assert discarded.json()["status"] == "DISCARDED"

        removed = client.delete(f"/v1/workspaces/{workspace['workspace_id']}")
        assert removed.status_code == 204
        assert workspace_root.is_dir()
        assert readme.exists()
        detached_chat = client.get(f"/v1/chats/{chat.json()['chat_id']}")
        assert detached_chat.json()["workspace_id"] is None


def test_code_run_continues_in_same_isolation_and_versions_review(tmp_path: Path) -> None:
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    readme = workspace_root / "README.md"
    readme.write_text("# Original\n", encoding="utf-8")
    app = create_app(local_settings(tmp_path, workspace_root))

    with TestClient(app) as client:
        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=code_decisions("# Original", "# First")
        )
        workspace = client.post(
            "/v1/workspaces",
            json={"name": "Collaboration", "root_path": str(workspace_root)},
        ).json()
        created = client.post(
            "/v1/code-runs",
            json={
                "workspace_id": workspace["workspace_id"],
                "instruction": "Set the first title",
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["code_run_id"]
        first = wait_for_review(client, run_id, 1)
        isolation = Path(app.state.container.settings.code_work_root) / run_id
        assert (isolation / "README.md").read_text("utf-8") == "# First\n"
        assert readme.read_text("utf-8") == "# Original\n"

        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=code_decisions("# First", "# Second")
        )
        continued = client.post(
            f"/v1/code-runs/{run_id}/turns",
            json={"instruction": "Change it again", "auto_start": True},
        )
        assert continued.status_code == 200, continued.text
        second = wait_for_review(client, run_id, 2)
        versioned_patch = client.get(f"/v1/artifacts/code-runs/{run_id}/diff-2.patch")
        assert versioned_patch.status_code == 200
        assert "+# Second" in versioned_patch.text
        assert [turn["status"] for turn in second["turns"]] == [
            "COMPLETED",
            "COMPLETED",
        ]
        assert {call["turn_id"] for call in second["tool_calls"]} == {
            turn["turn_id"] for turn in second["turns"]
        }

        stale = client.post(
            f"/v1/code-runs/{run_id}/apply", json={"review_revision": 1}
        )
        assert stale.status_code == 409

        # Apply the immutable reviewed snapshot rather than the mutable isolation.
        (isolation / "README.md").write_text("# Tampered\n", encoding="utf-8")
        applied = client.post(
            f"/v1/code-runs/{run_id}/apply", json={"review_revision": 2}
        )
        assert applied.status_code == 200, applied.text
        assert readme.read_text("utf-8") == "# Second\n"
        assert first["session_id"] == second["session_id"]


def test_queued_code_turns_run_in_order(tmp_path: Path) -> None:
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    (workspace_root / "README.md").write_text("# Demo\n", encoding="utf-8")
    app = create_app(local_settings(tmp_path, workspace_root))

    with TestClient(app) as client:
        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=[
                CodeDecision(kind="final", raw_response="<final>one</final>", answer="one"),
                CodeDecision(kind="final", raw_response="<final>two</final>", answer="two"),
            ]
        )
        workspace = client.post(
            "/v1/workspaces", json={"name": "Queue", "root_path": str(workspace_root)}
        ).json()
        created = client.post(
            "/v1/code-runs",
            json={
                "workspace_id": workspace["workspace_id"],
                "instruction": "First turn",
                "auto_start": False,
            },
        ).json()
        run_id = created["code_run_id"]
        queued = client.post(
            f"/v1/code-runs/{run_id}/turns",
            json={"instruction": "Second turn", "auto_start": True},
        )
        assert queued.status_code == 200, queued.text
        paused = wait_for_status(client, run_id, "PAUSED")
        assert [
            (turn["ordinal"], turn["status"], turn["summary"])
            for turn in paused["turns"]
        ] == [
            (1, "COMPLETED", "one"),
            (2, "COMPLETED", "two"),
        ]
        assert paused["review_revision"] == 0
        assert (workspace_root / "README.md").read_text("utf-8") == "# Demo\n"
        discarded = client.post(f"/v1/code-runs/{run_id}/discard")
        assert discarded.status_code == 200


def test_code_chat_continues_latest_open_run(tmp_path: Path) -> None:
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    (workspace_root / "README.md").write_text("# Demo\n", encoding="utf-8")
    app = create_app(local_settings(tmp_path, workspace_root))

    with TestClient(app) as client:
        app.state.container.code_adapter.next_decision = AsyncMock(
            side_effect=[
                CodeDecision(kind="final", raw_response="<final>one</final>", answer="one"),
                CodeDecision(kind="final", raw_response="<final>two</final>", answer="two"),
            ]
        )
        workspace = client.post(
            "/v1/workspaces", json={"name": "Chat", "root_path": str(workspace_root)}
        ).json()
        chat = client.post(
            "/v1/chats", json={"title": "Code", "workspace_id": workspace["workspace_id"]}
        ).json()
        first = client.post(
            f"/v1/chats/{chat['chat_id']}/messages",
            json={"content": "First", "requested_mode": "code"},
        ).json()
        run_id = first["code_run"]["code_run_id"]
        wait_for_status(client, run_id, "PAUSED")

        second = client.post(
            f"/v1/chats/{chat['chat_id']}/messages",
            json={"content": "Second", "requested_mode": "code"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["code_run"]["code_run_id"] == run_id
        result = wait_for_status(client, run_id, "PAUSED")
        assert [turn["instruction"] for turn in result["turns"]] == ["First", "Second"]


def test_recovery_endpoint_starts_queued_turn(tmp_path: Path) -> None:
    workspace_root = tmp_path / "project"
    workspace_root.mkdir()
    (workspace_root / "README.md").write_text("# Demo\n", encoding="utf-8")
    app = create_app(local_settings(tmp_path, workspace_root))
    with TestClient(app) as client:
        app.state.container.code_adapter.next_decision = AsyncMock(
            return_value=CodeDecision(
                kind="final", raw_response="<final>resumed</final>", answer="resumed"
            )
        )
        workspace = client.post(
            "/v1/workspaces",
            json={"name": "Recovery", "root_path": str(workspace_root)},
        ).json()
        run = client.post(
            "/v1/code-runs",
            json={
                "workspace_id": workspace["workspace_id"],
                "instruction": "Queued work",
                "auto_start": False,
            },
        ).json()
        recovery = client.get(
            f"/v1/code-runs/{run['code_run_id']}/recovery"
        )
        assert recovery.status_code == 200
        assert recovery.json()["recoverable"]
        assert recovery.json()["queued_turns"] == 1
        resumed = client.post(
            f"/v1/code-runs/{run['code_run_id']}/resume", json={}
        )
        assert resumed.status_code == 200, resumed.text
        result = wait_for_status(client, run["code_run_id"], "PAUSED")
        assert result["turns"][0]["summary"] == "resumed"


@pytest.mark.asyncio
async def test_native_picker_result_still_uses_workspace_path_policy(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    selected_root = allowed_root / "selected"
    selected_root.mkdir(parents=True)
    service = WorkspaceService(
        local_settings(tmp_path, allowed_root),
        AsyncMock(),
        CodeRunStore(tmp_path / "artifacts"),
    )
    service._open_directory_picker = lambda: str(selected_root)  # type: ignore[method-assign]

    selection = await service.pick_directory()

    assert selection.path == str(selected_root.resolve())
    assert selection.name == "selected"

    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    service._open_directory_picker = lambda: str(outside_root)  # type: ignore[method-assign]
    with pytest.raises(PermissionError, match="outside configured allowed roots"):
        await service.pick_directory()


@pytest.mark.asyncio
async def test_code_tools_enforce_read_paths_and_exact_patches(tmp_path: Path) -> None:
    root = tmp_path / "isolated"
    root.mkdir()
    readme = root / "README.md"
    readme.write_text("alpha\nbeta\n", encoding="utf-8")
    settings = local_settings(tmp_path, root)
    repository = AsyncMock()
    journal_count = 0

    def begin_tool_call(**_: object) -> tuple[SimpleNamespace, bool]:
        nonlocal journal_count
        journal_count += 1
        return (
            SimpleNamespace(
                tool_call_id=f"tool_{journal_count}",
                phase="RUNNING",
                result_excerpt="",
            ),
            True,
        )

    repository.begin_tool_call.side_effect = begin_tool_call
    store = CodeRunStore(tmp_path / "artifacts")
    workspace_service = WorkspaceService(settings, repository, store)
    executor = CodeToolExecutor(
        code_run_id="code_test",
        session_id="sess_test",
        root=root,
        baseline_manifest={"README.md": file_hash(readme)},
        repository=repository,
        workspace_service=workspace_service,
        run_store=store,
    )
    assert "run_shell" not in executor.registry

    no_read = await executor.execute(
        1,
        "patch_file",
        {"path": "README.md", "old_text": "alpha", "new_text": "changed"},
    )
    assert no_read.is_error
    assert "read_file is required" in no_read.content

    read = await executor.execute(2, "read_file", {"path": "README.md"})
    assert not read.is_error
    ambiguous = await executor.execute(
        3,
        "patch_file",
        {"path": "README.md", "old_text": "a", "new_text": "x"},
    )
    assert ambiguous.is_error
    assert "exactly once" in ambiguous.content

    patched = await executor.execute(
        4,
        "patch_file",
        {"path": "README.md", "old_text": "alpha", "new_text": "updated"},
    )
    assert not patched.is_error
    assert readme.read_text(encoding="utf-8").startswith("updated")
    assert patched.before_hashes["README.md"] != patched.after_hashes["README.md"]
    assert "--- a/README.md" in patched.unified_diff
    assert "+updated" in patched.unified_diff
    recorded = repository.complete_tool_call.await_args.kwargs
    assert recorded["before_hashes"] == patched.before_hashes
    assert recorded["after_hashes"] == patched.after_hashes
    assert recorded["unified_diff"] == patched.unified_diff

    escaped = await executor.execute(5, "read_file", {"path": "../outside.txt"})
    assert escaped.is_error
    sensitive = await executor.execute(
        6, "create_file", {"path": ".env", "content": "SECRET=1"}
    )
    assert sensitive.is_error

    first_status = await executor.execute(7, "git_status", {})
    repeated_status = await executor.execute(8, "git_status", {})
    assert not first_status.is_error
    assert repeated_status.is_error
    assert "repeated identical" in repeated_status.content


@pytest.mark.asyncio
async def test_code_adapter_corrects_invalid_protocol_twice(tmp_path: Path) -> None:
    settings = local_settings(tmp_path, tmp_path)
    settings.code_base_url = "http://coding.test/v1"
    settings.code_model = "qwen-code"
    responses = iter(
        [
            "not a protocol response",
            '<tool_call>{"name":</tool_call>',
            '<final>Protocol corrected.</final>',
        ]
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(responses)}}]},
        )

    adapter = CodeAdapter(settings)
    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(
        base_url=settings.code_base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        decision = await adapter.next_decision(
            [{"role": "user", "content": "Update README"}],
            system_prompt="Use the protocol.",
        )
    finally:
        await adapter.close()

    assert decision.kind == "final"
    assert decision.answer == "Protocol corrected."
    assert len(requests) == 3
    final_payload = json.loads(requests[-1].content)
    corrections = [
        item
        for item in final_payload["messages"]
        if item["role"] == "user" and "violated the protocol" in item["content"]
    ]
    assert len(corrections) == 2


def test_local_workspace_settings_require_allowed_roots_and_loopback(tmp_path: Path) -> None:
    missing_roots = Settings(
        _env_file=None,
        enable_local_workspaces=True,
        api_host="127.0.0.1",
        workspace_allowed_roots=[],
    )
    with pytest.raises(ValueError, match="ALLOWED_ROOTS"):
        missing_roots.prepare_directories()

    public_bind = Settings(
        _env_file=None,
        enable_local_workspaces=True,
        api_host="0.0.0.0",
        workspace_allowed_roots=[tmp_path],
        artifact_root=tmp_path / "artifacts-2",
        browser_state_root=tmp_path / "browser-state-2",
        code_work_root=tmp_path / "code-work-2",
    )
    with pytest.raises(ValueError, match="loopback"):
        public_bind.prepare_directories()

    workspace_root = tmp_path / "local-only"
    workspace_root.mkdir()
    non_test_settings = local_settings(tmp_path / "remote-check", workspace_root)
    non_test_settings.environment = "development"
    with TestClient(create_app(non_test_settings)) as client:
        assert client.get("/v1/workspaces").status_code == 403


def test_git_workspace_uses_worktree_when_clean_and_snapshot_when_dirty(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    (repository_root / "README.md").write_text("clean\n", encoding="utf-8")
    (repository_root / ".gitignore").write_text("secret/\n", encoding="utf-8")
    (repository_root / "secret").mkdir()
    (repository_root / "secret" / "token.txt").write_text("hidden", encoding="utf-8")
    for arguments in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "FaraFlow Test"],
        ["add", "README.md", ".gitignore"],
        ["commit", "-m", "initial"],
    ):
        completed = run(
            ["git", *arguments],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    settings = local_settings(tmp_path, repository_root)
    service = WorkspaceService(
        settings,
        AsyncMock(),
        CodeRunStore(tmp_path / "artifacts"),
    )
    workspace = SimpleNamespace(
        root_path=str(repository_root),
        git_root=str(repository_root),
    )

    clean = service.prepare_isolation("code_clean", workspace)
    assert clean["isolation_kind"] == "worktree"
    assert (Path(clean["isolated_path"]) / "README.md").read_text(encoding="utf-8") == "clean\n"
    with pytest.raises(PermissionError, match="ignored path"):
        service.safe_path(repository_root, "secret/token.txt")
    service.cleanup_isolation(SimpleNamespace(**clean), workspace)
    assert not Path(clean["isolated_path"]).exists()

    (repository_root / "README.md").write_text("dirty\n", encoding="utf-8")
    dirty = service.prepare_isolation("code_dirty", workspace)
    assert dirty["isolation_kind"] == "snapshot"
    assert (Path(dirty["isolated_path"]) / "README.md").read_text(encoding="utf-8") == "dirty\n"
    assert not (Path(dirty["isolated_path"]) / "secret").exists()
    service.cleanup_isolation(SimpleNamespace(**dirty), workspace)
    assert not Path(dirty["isolated_path"]).exists()


@pytest.mark.asyncio
async def test_running_code_runs_are_interrupted_after_restart(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/restart.db")
    await database.create_schema()
    repository = Repository(database.session_factory)
    workspace = await repository.create_workspace(
        WorkspaceCreate(name="Restart", root_path=str(tmp_path)),
        root_path=str(tmp_path),
        repository_kind="directory",
        git_root=None,
        branch=None,
    )
    code_run = await repository.create_code_run(
        workspace_id=workspace.workspace_id,
        instruction="test restart",
    )
    await repository.update_code_run(code_run.code_run_id, status=CodeRunStatus.RUNNING)

    assert await repository.interrupt_running_code_runs() == 1
    interrupted = await repository.get_code_run(code_run.code_run_id)
    assert interrupted.status == CodeRunStatus.INTERRUPTED.value
    assert interrupted.error == {"reason": "server_restarted"}
    await database.dispose()
