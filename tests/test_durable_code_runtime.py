from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from faraflow.code.adapter import CodeAdapter
from faraflow.code.runtime import CodeRuntime
from faraflow.code.service import CodeRunService
from faraflow.config import Settings
from faraflow.domain.enums import CodeApplyStatus, CodeRunStatus, CodeToolPhase
from faraflow.domain.schemas import CodeTurnCreate, WorkspaceCreate
from faraflow.infra.database import Database
from faraflow.infra.events import EventBus
from faraflow.infra.outbox import EventOutboxDispatcher
from faraflow.infra.repository import ConflictError, Repository
from faraflow.workspace.run_store import CodeRunStore, file_hash
from faraflow.workspace.service import WorkspaceService


def settings_for(tmp_path: Path, root: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        api_host="127.0.0.1",
        database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/durable.db",
        artifact_root=tmp_path / "artifacts",
        browser_state_root=tmp_path / "browser-state",
        code_work_root=tmp_path / "code-work",
        enable_local_workspaces=True,
        workspace_allowed_roots=[root],
        code_base_url="http://coding.invalid/v1",
        code_model="test-coder",
    )


async def durable_services(tmp_path: Path, root: Path):
    settings = settings_for(tmp_path, root)
    database = Database(settings.database_url)
    await database.create_schema()
    repository = Repository(database.session_factory)
    store = CodeRunStore(settings.artifact_root)
    workspaces = WorkspaceService(settings, repository, store)
    adapter = CodeAdapter(settings)
    runtime = CodeRuntime(settings, repository, workspaces, store, adapter, EventBus())
    service = CodeRunService(repository, workspaces, store, runtime)
    return database, repository, store, workspaces, adapter, runtime, service


@pytest.mark.asyncio
async def test_event_sequences_are_ordered_and_cursor_addressable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    database, repository, _, _, adapter, runtime, _ = await durable_services(tmp_path, root)
    try:
        for number in range(1, 8):
            event = await repository.append_event("session", "test.event", f"event {number}")
            assert event.sequence == number
            assert event.payload["sequence"] == number
        rows = await repository.list_events("session", after_sequence=4)
        assert [item.sequence for item in rows] == [5, 6, 7]
        event_bus = EventBus()
        queue = await event_bus.subscribe("session")
        dispatcher = EventOutboxDispatcher(repository, event_bus, poll_interval_seconds=0.01)
        dispatcher.start()
        try:
            delivered = await asyncio.wait_for(queue.get(), timeout=2)
            assert delivered.sequence == 1
            deadline = asyncio.get_running_loop().time() + 2
            while await repository.list_pending_event_outbox():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("outbox was not drained")
                await asyncio.sleep(0.01)
        finally:
            await dispatcher.close()
            await event_bus.unsubscribe("session", queue)
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_code_run_lease_has_single_owner(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    database, repository, _, _, adapter, runtime, _ = await durable_services(tmp_path, root)
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Lease", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="lease",
            model="test-coder",
        )
        token = await repository.acquire_code_run_lease(run.code_run_id, "one", 60)
        assert token is not None
        assert not await repository.acquire_code_run_lease(run.code_run_id, "two", 60)
        await repository.release_code_run_lease(run.code_run_id, "one", token)
        assert await repository.acquire_code_run_lease(run.code_run_id, "two", 60)
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


def test_content_addressed_reviews_deduplicate_and_enforce_quota(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    source = root / "same.txt"
    source.write_text("same content", encoding="utf-8")
    store = CodeRunStore(tmp_path / "artifacts", quota_bytes=32)

    first = store.preserve_review("code_one", 1, root, ["same.txt"])
    second = store.preserve_review("code_two", 1, root, ["same.txt"])
    assert first == second
    assert len(list(store.blob_root.glob("*/*"))) == 1

    source.write_text("x" * 40, encoding="utf-8")
    with pytest.raises(RuntimeError, match="quota"):
        store.preserve_review("code_three", 1, root, ["same.txt"])


@pytest.mark.asyncio
async def test_busy_turn_policies_are_routed_to_runtime(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    database, repository, _, _, adapter, runtime, service = await durable_services(tmp_path, root)
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Busy", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="initial",
            model="test-coder",
        )
        await repository.update_code_run(run.code_run_id, status=CodeRunStatus.RUNNING)
        runtime.inject = AsyncMock()  # type: ignore[method-assign]
        await service.continue_run(
            run.code_run_id,
            CodeTurnCreate(instruction="inject now", auto_start=False, busy_policy="inject"),
        )
        runtime.inject.assert_awaited_once()  # type: ignore[attr-defined]
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_turns_receive_unique_ordinals(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    database, repository, _, _, adapter, runtime, _ = await durable_services(tmp_path, root)
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Turns", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="initial",
            model="test-coder",
        )
        turns = await asyncio.gather(
            *(repository.create_code_turn(run.code_run_id, f"turn {number}") for number in range(5))
        )
        assert sorted(turn.ordinal for turn in turns) == [2, 3, 4, 5, 6]
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_workspace_fencing_rejects_stale_owner(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    database, repository, _, _, adapter, runtime, _ = await durable_services(tmp_path, root)
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Fence", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        first = await repository.acquire_workspace_mutation_lease(workspace.workspace_id, "one", 60)
        assert first == 1
        assert (
            await repository.acquire_workspace_mutation_lease(workspace.workspace_id, "two", 60)
            is None
        )
        await repository.release_workspace_mutation_lease(workspace.workspace_id, "one", first)
        second = await repository.acquire_workspace_mutation_lease(
            workspace.workspace_id, "two", 60
        )
        assert second == 2
        with pytest.raises(ConflictError, match="stale"):
            await repository.assert_workspace_mutation_fence(workspace.workspace_id, "one", first)
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_incomplete_write_is_reconciled_from_hashes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "README.md"
    source.write_text("before\n", encoding="utf-8")
    database, repository, _, workspaces, adapter, runtime, service = await durable_services(
        tmp_path, root
    )
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Recovery", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="recover",
            model="test-coder",
        )
        isolation = workspaces.prepare_isolation(run.code_run_id, workspace)
        run = await repository.update_code_run(run.code_run_id, **isolation)
        isolated_root = Path(run.isolated_path or "")
        target = isolated_root / "README.md"
        before = file_hash(target)
        after_content = "after\n"
        expected = hashlib.sha256(after_content.encode("utf-8")).hexdigest()
        call, _ = await repository.begin_tool_call(
            code_run_id=run.code_run_id,
            session_id=run.session_id,
            turn_id=None,
            external_call_id="turn:call",
            step_no=1,
            tool_name="patch_file",
            arguments={"path": "README.md"},
            request_digest="digest",
            before_hashes={"README.md": before},
            expected_after_hashes={"README.md": expected},
        )
        target.write_bytes(after_content.encode("utf-8"))

        assert await runtime.reconcile_incomplete_tools(run.code_run_id, isolated_root) == []
        assert await repository.list_incomplete_tool_calls(run.code_run_id) == []
        calls = await repository.list_tool_calls(run.code_run_id)
        assert calls[0].tool_call_id == call.tool_call_id
        assert calls[0].phase == CodeToolPhase.SUCCEEDED.value
        assert calls[0].status == "reconciled"

        unknown_call, _ = await repository.begin_tool_call(
            code_run_id=run.code_run_id,
            session_id=run.session_id,
            turn_id=None,
            external_call_id="turn:unknown",
            step_no=2,
            tool_name="patch_file",
            arguments={"path": "README.md"},
            request_digest="unknown",
            before_hashes={"README.md": "not-before"},
            expected_after_hashes={"README.md": "not-after"},
        )
        assert await runtime.reconcile_incomplete_tools(run.code_run_id, isolated_root) == [
            unknown_call.tool_call_id
        ]
        recovery = await service.recovery(run.code_run_id)
        assert not recovery.recoverable
        assert recovery.incomplete_tool_calls == [unknown_call.tool_call_id]
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_incomplete_apply_is_rolled_back_on_startup_recovery(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "README.md"
    source.write_text("original\n", encoding="utf-8")
    database, repository, store, _, adapter, runtime, service = await durable_services(
        tmp_path, root
    )
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Apply", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="apply",
            model="test-coder",
        )
        await repository.update_code_run(run.code_run_id, status=CodeRunStatus.REVIEW_REQUIRED)
        store.preserve_apply_backup(run.code_run_id, "README.md", source)
        journal = await repository.create_apply_journal(
            code_run_id=run.code_run_id,
            workspace_id=workspace.workspace_id,
            review_revision=1,
            paths=["README.md"],
        )
        await repository.update_apply_journal(
            journal.journal_id,
            status=CodeApplyStatus.APPLYING,
            # Simulate a crash after os.replace and before the completed-path
            # checkpoint is committed.
            completed_paths=[],
        )
        source.write_text("partially applied\n", encoding="utf-8")

        assert await service.recover_incomplete_applies() == 1
        assert source.read_text("utf-8") == "original\n"
        assert await repository.list_incomplete_apply_journals() == []
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_corrupt_apply_backup_is_reported_without_blocking_startup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "README.md"
    source.write_text("original\n", encoding="utf-8")
    database, repository, store, _, adapter, runtime, service = await durable_services(
        tmp_path, root
    )
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Corrupt", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="apply",
            model="test-coder",
        )
        await repository.update_code_run(run.code_run_id, status=CodeRunStatus.REVIEW_REQUIRED)
        digest = store.preserve_apply_backup(run.code_run_id, "README.md", source)
        journal = await repository.create_apply_journal(
            code_run_id=run.code_run_id,
            workspace_id=workspace.workspace_id,
            review_revision=1,
            paths=["README.md"],
        )
        await repository.update_apply_journal(
            journal.journal_id,
            status=CodeApplyStatus.APPLYING,
            backup_hashes={"README.md": digest},
        )
        store.backup_path(run.code_run_id, "README.md").write_text("corrupt\n", encoding="utf-8")
        source.write_text("partially applied\n", encoding="utf-8")

        assert await service.recover_incomplete_applies() == 0
        failed = await repository.get_code_run(run.code_run_id)
        assert failed.status == CodeRunStatus.FAILED.value
        assert failed.error["reason"] == "apply_recovery_failed"
        assert source.read_text("utf-8") == "partially applied\n"
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_verification_profile_is_revision_bound_and_becomes_stale(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    database, repository, _, workspaces, adapter, runtime, service = await durable_services(
        tmp_path, root
    )
    runtime.settings.code_verification_profiles = {
        "smoke": [sys.executable, "-c", "print('verified')"],
        "large-output": [sys.executable, "-c", "print('x' * 4096)"],
        "timeout": [sys.executable, "-c", "import time; time.sleep(5)"],
    }
    runtime.settings.code_verification_output_bytes = 1024
    runtime.settings.code_verification_timeout_seconds = 1
    try:
        workspace = await repository.create_workspace(
            WorkspaceCreate(name="Verify", root_path=str(root)),
            root_path=str(root),
            repository_kind="directory",
            git_root=None,
            branch=None,
        )
        run = await repository.create_code_run(
            workspace_id=workspace.workspace_id,
            instruction="verify",
            model="test-coder",
        )
        isolation = workspaces.prepare_isolation(run.code_run_id, workspace)
        await repository.update_code_run(
            run.code_run_id,
            status=CodeRunStatus.REVIEW_REQUIRED,
            review_revision=1,
            **isolation,
        )
        result = await service.verify(run.code_run_id, "smoke")
        assert result.status.value == "PASSED"
        assert "verified" in result.stdout_excerpt
        assert not result.stale

        truncated = await service.verify(run.code_run_id, "large-output")
        assert truncated.status.value == "PASSED"
        assert truncated.stdout_excerpt.endswith("...[output truncated]")

        timed_out = await service.verify(run.code_run_id, "timeout")
        assert timed_out.status.value == "TIMED_OUT"

        await repository.update_code_run(run.code_run_id, review_revision=2)
        history = await service.verifications(run.code_run_id)
        assert history[0].stale
    finally:
        await runtime.close()
        await adapter.close()
        await database.dispose()
