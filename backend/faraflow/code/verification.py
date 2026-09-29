import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from faraflow.config import Settings
from faraflow.domain.enums import CodeVerificationStatus
from faraflow.infra.async_utils import run_sync
from faraflow.infra.repository import Repository
from faraflow.workspace.service import WorkspaceService


class VerificationRunner:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        workspaces: WorkspaceService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.workspaces = workspaces
        self.root = (settings.code_work_root / "verifications").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def profiles(self) -> Dict[str, List[str]]:
        return {
            profile_id: list(argv)
            for profile_id, argv in self.settings.code_verification_profiles.items()
            if profile_id.strip() and argv and all(isinstance(item, str) and item for item in argv)
        }

    async def run(
        self,
        *,
        code_run_id: str,
        turn_id: Optional[str],
        isolated_root: Path,
        review_revision: int,
        profile_id: str,
    ) -> Any:
        profiles = self.profiles()
        argv = profiles.get(profile_id)
        if argv is None:
            raise ValueError(f"unknown verification profile: {profile_id}")
        manifest = await run_sync(self.workspaces.snapshot_manifest, isolated_root)
        manifest_digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        command_digest = hashlib.sha256(
            json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record = await self.repository.create_code_verification(
            code_run_id=code_run_id,
            turn_id=turn_id,
            review_revision=review_revision,
            profile_id=profile_id,
            command_digest=command_digest,
            source_manifest_digest=manifest_digest,
        )
        target = (self.root / record.verification_id).resolve()
        if target.parent != self.root:
            raise ValueError("verification directory escaped work root")
        target.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        try:
            await run_sync(self.workspaces._copy_tree, isolated_root, target)
            await self.repository.update_code_verification(
                record.verification_id, status=CodeVerificationStatus.RUNNING
            )
            process = await self._start(argv, target)
            try:
                stdout, stderr = await asyncio.wait_for(
                    self._communicate(process),
                    timeout=self.settings.code_verification_timeout_seconds,
                )
                return await self.repository.update_code_verification(
                    record.verification_id,
                    status=(
                        CodeVerificationStatus.PASSED
                        if process.returncode == 0
                        else CodeVerificationStatus.FAILED
                    ),
                    exit_code=process.returncode,
                    stdout_excerpt=stdout,
                    stderr_excerpt=stderr,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except asyncio.TimeoutError:
                await self._terminate(process)
                return await self.repository.update_code_verification(
                    record.verification_id,
                    status=CodeVerificationStatus.TIMED_OUT,
                    stderr_excerpt="verification exceeded its timeout",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        except Exception as exc:
            return await self.repository.update_code_verification(
                record.verification_id,
                status=CodeVerificationStatus.ERROR,
                error={"type": type(exc).__name__, "message": str(exc)},
                stderr_excerpt=f"{type(exc).__name__}: {exc}"[:2000],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            await run_sync(shutil.rmtree, target, True)

    async def _start(
        self, argv: List[str], cwd: Path
    ) -> asyncio.subprocess.Process:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "LANG",
                "PYTHONUTF8",
                "PYTHONIOENCODING",
            }
        }
        options: Dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            options["start_new_session"] = True
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )

    async def _communicate(
        self, process: asyncio.subprocess.Process
    ) -> Tuple[str, str]:
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(self._read_limited(process.stdout))
        stderr_task = asyncio.create_task(self._read_limited(process.stderr))
        try:
            await process.wait()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return stdout, stderr
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    async def _read_limited(self, stream: asyncio.StreamReader) -> str:
        limit = self.settings.code_verification_output_bytes
        kept = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            remaining = limit - len(kept)
            if remaining > 0:
                kept.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        text = bytes(kept).decode("utf-8", errors="replace")
        return text + ("\n...[output truncated]" if truncated else "")

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        await process.wait()
