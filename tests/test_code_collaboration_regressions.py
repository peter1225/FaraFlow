"""Regression coverage for storage, agent review and durable injection boundaries."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from faraflow.api.main import create_app
from faraflow.code.engine import NativeCodeEngine
from faraflow.code.gc import CodeArtifactGarbageCollector
from faraflow.code.protocol import CodeDecision
from faraflow.domain.enums import CodeRunStatus, CodeTurnStatus
from faraflow.domain.schemas import (
    CodeAgentCreate,
    CodeRunApply,
    CodeRunCreate,
    CodeRunResume,
    CodeTurnCreate,
    WorkspaceCreate,
)
from faraflow.infra.database import CodeRunRecord
from faraflow.infra.repository import ConflictError, now_utc
from faraflow.workspace.run_store import CodeRunStore, file_hash
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_code_workspace import code_decisions, local_settings
from test_durable_code_runtime import durable_services


@pytest.fixture
async def services(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    result = await durable_services(tmp_path, root)
    database, repository, _, _, adapter, runtime, _ = result
    runtime.settings.pico_state_root = tmp_path / "pico-state"
    workspace = await repository.create_workspace(
        WorkspaceCreate(name="Regression", root_path=str(root)),
        root_path=str(root),
        repository_kind="directory",
        git_root=None,
        branch=None,
    )
    try:
        yield root, workspace, result
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


async def settle(runtime, run_id):
    job = runtime._jobs.get(run_id)
    if job is not None:
        await job


async def create_review(workspace, adapter, runtime, service):
    adapter.next_decision = AsyncMock(side_effect=code_decisions("# Original", "# Updated"))
    run = await service.create(
        CodeRunCreate(
            workspace_id=workspace.workspace_id,
            instruction="Update README",
        )
    )
    await settle(runtime, run.code_run_id)
    return await service.get(run.code_run_id)


async def age_run(database, run_id):
    async with database.session_factory() as db:
        await db.execute(
            update(CodeRunRecord)
            .where(CodeRunRecord.code_run_id == run_id)
            .values(finished_at=now_utc() - timedelta(days=40))
        )
        await db.commit()


@pytest.mark.asyncio
async def test_gc_preserves_applied_backups_until_revert(services):
    root, workspace, result = services
    database, repository, store, _, adapter, runtime, service = result
    run = await create_review(workspace, adapter, runtime, service)
    await service.apply(run.code_run_id, CodeRunApply(review_revision=run.review_revision))
    await age_run(database, run.code_run_id)
    collector = CodeArtifactGarbageCollector(runtime.settings, repository, store)
    await collector.collect()
    assert store.backup_path(run.code_run_id, "README.md").is_file()
    assert not store.review_path(run.code_run_id, run.review_revision, "README.md").exists()

    await service.revert(run.code_run_id)
    assert (root / "README.md").read_text("utf-8") == "# Original\n"
    await age_run(database, run.code_run_id)
    await collector.collect()
    assert not store.backup_path(run.code_run_id, "README.md").exists()
    assert (root / "README.md").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_revert_rejects_unavailable_or_corrupt_backup(services, damage):
    root, workspace, result = services
    _, _, store, _, adapter, runtime, service = result
    run = await create_review(workspace, adapter, runtime, service)
    await service.apply(run.code_run_id, CodeRunApply(review_revision=run.review_revision))
    backup = store.backup_path(run.code_run_id, "README.md")
    if damage == "missing":
        backup.unlink()
    else:
        backup.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ConflictError, match="backup"):
        await service.revert(run.code_run_id)
    assert (root / "README.md").read_text("utf-8") == "# Updated\n"
    assert (await service.get(run.code_run_id)).status == CodeRunStatus.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["patch", "delete"])
async def test_agent_merge_and_revert_preserve_parent_diff_and_original_source(services, operation):
    root, workspace, result = services
    _, _, _, _, adapter, runtime, service = result
    adapter.next_decision = AsyncMock(
        return_value=CodeDecision(
            kind="final",
            raw_response="<final>Ready</final>",
            answer="Ready",
        )
    )
    parent = await service.create(
        CodeRunCreate(
            workspace_id=workspace.workspace_id,
            instruction="Prepare",
        )
    )
    await settle(runtime, parent.code_run_id)
    decisions = code_decisions("# Original", "# Updated")
    if operation == "delete":
        decisions[1] = CodeDecision(
            kind="tool",
            raw_response="delete",
            tool_name="delete_file",
            arguments={"path": "README.md"},
        )
    adapter.next_decision = AsyncMock(side_effect=decisions)
    child = await service.create_agent(
        parent.code_run_id,
        CodeAgentCreate(
            instruction="Modify README",
            mode="isolated_write",
        ),
    )
    await settle(runtime, child.code_run_id)
    child = await service.get(child.code_run_id)
    await service.apply(child.code_run_id, CodeRunApply(review_revision=child.review_revision))
    diff = await service.diff(parent.code_run_id)
    assert "-# Original" in diff.diff
    assert "--- a/README.md" in diff.diff
    if operation == "delete":
        assert "+++ /dev/null" in diff.diff
    assert (root / "README.md").read_text("utf-8") == "# Original\n"

    await service.revert(child.code_run_id)
    parent = await service.get(parent.code_run_id)
    assert parent.status == CodeRunStatus.PAUSED
    assert (await service.diff(parent.code_run_id)).diff == ""
    with pytest.raises(ConflictError):
        await service.apply(parent.code_run_id, CodeRunApply(review_revision=diff.review_revision))
    assert (root / "README.md").read_text("utf-8") == "# Original\n"


@pytest.mark.asyncio
async def test_child_revert_after_parent_apply_is_rejected(services):
    root, workspace, result = services
    _, _, _, _, adapter, runtime, service = result
    adapter.next_decision = AsyncMock(
        return_value=CodeDecision(
            kind="final",
            raw_response="<final>Ready</final>",
            answer="Ready",
        )
    )
    parent = await service.create(
        CodeRunCreate(
            workspace_id=workspace.workspace_id,
            instruction="Prepare",
        )
    )
    await settle(runtime, parent.code_run_id)
    adapter.next_decision = AsyncMock(side_effect=code_decisions("# Original", "# Updated"))
    child = await service.create_agent(
        parent.code_run_id,
        CodeAgentCreate(
            instruction="Modify README",
            mode="isolated_write",
        ),
    )
    await settle(runtime, child.code_run_id)
    child = await service.get(child.code_run_id)
    await service.apply(child.code_run_id, CodeRunApply(review_revision=child.review_revision))
    parent = await service.get(parent.code_run_id)
    await service.apply(parent.code_run_id, CodeRunApply(review_revision=parent.review_revision))
    with pytest.raises(ConflictError, match="parent"):
        await service.revert(child.code_run_id)
    assert (root / "README.md").read_text("utf-8") == "# Updated\n"
    await service.revert(parent.code_run_id)
    assert (root / "README.md").read_text("utf-8") == "# Original\n"


def test_private_blob_and_review_paths_are_never_public(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    settings = local_settings(tmp_path, root)
    settings.enable_local_workspaces = False
    app = create_app(settings)
    with TestClient(app) as client:
        store = app.state.container.code_runtime.run_store
        digest = store.put_blob(b"private source")
        for prefix in ("blobs", "sess/%2e%2e/blobs", "sess%5c..%5cblobs"):
            response = client.get(f"/v1/artifacts/{prefix}/sha256/{digest[:2]}/{digest}")
            assert response.status_code == 404
        private = store.run_dir("code_test") / "baseline" / "README.md"
        private.parent.mkdir(parents=True)
        private.write_text("private source", encoding="utf-8")
        response = client.get("/v1/artifacts/sess/%2e%2e/code-runs/code_test/baseline/README.md")
        assert response.status_code == 404


def test_review_mutation_cannot_corrupt_other_reviews_or_blob(tmp_path: Path):
    root = tmp_path / "work"
    root.mkdir()
    source = root / "same.txt"
    source.write_text("shared", encoding="utf-8")
    store = CodeRunStore(tmp_path / "artifacts")
    digest = store.preserve_review("code_one", 1, root, ["same.txt"])["same.txt"]
    store.preserve_review("code_two", 1, root, ["same.txt"])
    first = store.review_path("code_one", 1, "same.txt")
    first.write_text("edited", encoding="utf-8")
    assert file_hash(store.review_path("code_two", 1, "same.txt")) == digest
    assert file_hash(store.blob_root / digest[:2] / digest) == digest


def test_existing_hardlinked_reviews_are_detached(tmp_path: Path):
    store = CodeRunStore(tmp_path / "artifacts")
    digest = store.put_blob(b"original")
    source = store.blob_root / digest[:2] / digest
    first = store.review_path("code_one", 1, "file.txt")
    second = store.review_path("code_two", 1, "file.txt")
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    os.link(source, first)
    os.link(source, second)
    assert store.detach_legacy_review_links() == 2
    first.write_text("modified", encoding="utf-8")
    assert file_hash(second) == digest
    assert file_hash(source) == digest
    assert store.detach_legacy_review_links() == 0


@pytest.mark.asyncio
async def test_backup_validation_precedes_every_restore(services):
    root, _, result = services
    _, _, store, _, _, _, service = result
    first = root / "README.md"
    second = root / "second.txt"
    second.write_text("second original", encoding="utf-8")
    expected = {
        "README.md": store.preserve_apply_backup("code_restore", "README.md", first),
        "second.txt": store.preserve_apply_backup("code_restore", "second.txt", second),
    }
    first.write_text("first updated", encoding="utf-8")
    second.write_text("second updated", encoding="utf-8")
    store.backup_path("code_restore", "second.txt").unlink()
    with pytest.raises(ConflictError, match="backup"):
        service._restore_backups(
            "code_restore",
            root,
            list(expected),
            operation_id="restore",
            expected_hashes=expected,
        )
    assert first.read_text("utf-8") == "first updated"
    assert second.read_text("utf-8") == "second updated"


@pytest.mark.asyncio
async def test_quota_failure_is_persisted_and_can_be_resumed(services):
    _, workspace, result = services
    _, _, store, _, adapter, runtime, service = result
    store.quota_bytes = 1
    run = await create_review(workspace, adapter, runtime, service)
    assert run.status == CodeRunStatus.FAILED
    assert "quota" in run.error["message"]
    assert run.code_run_id not in runtime._jobs
    store.quota_bytes = 1024
    adapter.next_decision = AsyncMock(
        return_value=CodeDecision(
            kind="final",
            raw_response="<final>Recovered</final>",
            answer="Recovered",
        )
    )
    await service.resume(run.code_run_id, CodeRunResume())
    await settle(runtime, run.code_run_id)
    assert (await service.get(run.code_run_id)).status == CodeRunStatus.REVIEW_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_injected_instruction_completes_only_with_active_turn(services, fail):
    _, workspace, result = services
    _, repository, _, _, adapter, runtime, service = result
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    class BoundaryEngine(NativeCodeEngine):
        async def run(self, request, execute, emit):
            started.set()
            await release.wait()
            response = await execute("read_file", {"path": "README.md"}, "read")
            assert "followup" in response.content
            observed.extend(await repository.list_code_turns(request.run_id))
            if fail:
                raise RuntimeError("connection lost before model receipt")
            return "completed"

    runtime.engine = BoundaryEngine(runtime.settings, adapter)
    run = await service.create(
        CodeRunCreate(
            workspace_id=workspace.workspace_id,
            instruction="initial",
        )
    )
    await started.wait()
    await service.continue_run(
        run.code_run_id,
        CodeTurnCreate(
            instruction="followup",
            busy_policy="inject",
        ),
    )
    release.set()
    await settle(runtime, run.code_run_id)
    assert observed[1].status == CodeTurnStatus.INJECTED.value
    turns = await repository.list_code_turns(run.code_run_id)
    assert turns[1].status == (
        CodeTurnStatus.QUEUED.value if fail else CodeTurnStatus.COMPLETED.value
    )
    if fail:
        assert (await service.recovery(run.code_run_id)).queued_turns == 1


@pytest.mark.asyncio
async def test_restart_requeues_injected_turns(services):
    _, workspace, result = services
    _, repository, _, _, _, _, _ = result
    run = await repository.create_code_run(
        workspace_id=workspace.workspace_id,
        instruction="initial",
        model="test-coder",
    )
    await repository.update_code_run(run.code_run_id, status=CodeRunStatus.RUNNING)
    injected = await repository.create_code_turn(run.code_run_id, "followup")
    await repository.update_code_turn(injected.turn_id, CodeTurnStatus.INJECTED)
    await repository.interrupt_running_code_runs()
    queued = await repository.next_queued_code_turn(run.code_run_id)
    turns = await repository.list_code_turns(run.code_run_id)
    assert turns[1].status == CodeTurnStatus.QUEUED.value
    assert queued is not None
