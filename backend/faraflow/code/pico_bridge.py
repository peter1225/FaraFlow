"""One supervised worker per code run, using bounded JSON-RPC over stdio."""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

from faraflow.config import Settings

from .engine import EmitEvent, EngineRequest, ExecuteTool
from .registry import ToolRegistry

PICO_VERSION = "0.1.7"
PICO_COMMIT = "d6c7a648fd7ee63e472c0d438f1c8299d9b8f871"
MAX_FRAME_BYTES = 8 * 1024 * 1024
WORKER = Path(__file__).with_name("pico_worker.py")


class PicoCodeEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._health_cache: Dict[str, Any] = {}
        self._health_checked = 0.0
        self._health_lock = asyncio.Lock()

    async def health(self) -> Dict[str, Any]:
        async with self._health_lock:
            if self._health_cache and time.monotonic() - self._health_checked < 30:
                return self._health_cache
            result = {"engine": "pico", "version": PICO_VERSION, "status": "unavailable"}
            if self.settings.pico_python:
                process = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        self.settings.pico_python,
                        "-I",
                        "-u",
                        str(WORKER),
                        "--check",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=MAX_FRAME_BYTES,
                        **(
                            {"creationflags": subprocess.CREATE_NO_WINDOW}
                            if os.name == "nt"
                            else {}
                        ),
                    )
                    hello = await asyncio.wait_for(
                        self._read(process), self.settings.pico_startup_timeout_seconds
                    )
                    if hello.get("method") == "ready" and hello.get("params") == {
                        "protocol": 1,
                        "version": PICO_VERSION,
                    }:
                        result["status"] = "ok"
                except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                    pass
                finally:
                    if process is not None:
                        await self._stop(process)
            self._health_cache = result
            self._health_checked = time.monotonic()
            return result

    async def run(self, request: EngineRequest, execute: ExecuteTool, emit: EmitEvent) -> str:
        if not self.settings.code_base_url or not self.settings.code_model:
            raise RuntimeError("coding model is not configured")
        if not self.settings.pico_python:
            raise RuntimeError("FARAFLOW_PICO_PYTHON must point to the Pico Python 3.12 executable")
        state = (self.settings.pico_state_root / request.run_id).resolve()
        # Private history must never be placed in the source tree or served as artifacts.
        for forbidden in (request.root, request.source_root, self.settings.artifact_root.resolve()):
            if forbidden is None:
                continue
            forbidden = forbidden.resolve()
            if state == forbidden or forbidden in state.parents:
                raise ValueError("FARAFLOW_PICO_STATE_ROOT must be outside source/artifact roots")
        # Keep one Pico session directory for all turns of this CodeRun.
        state.mkdir(parents=True, exist_ok=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            }
        }
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        process = await asyncio.create_subprocess_exec(
            self.settings.pico_python,
            "-I",
            "-u",
            str(WORKER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(state),
            env=env,
            limit=MAX_FRAME_BYTES,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        seen = set()
        try:
            hello = await asyncio.wait_for(
                self._read(process), timeout=self.settings.pico_startup_timeout_seconds
            )
            if hello.get("method") != "ready" or hello.get("params") != {
                "protocol": 1,
                "version": PICO_VERSION,
            }:
                raise RuntimeError(
                    "Pico worker unavailable: install the pinned Python 3.12 runtime"
                )
            await emit(
                "code.engine.ready",
                "Pico 执行引擎已就绪",
                {
                    "engine": "pico",
                    "version": PICO_VERSION,
                    "upstream_commit": PICO_COMMIT,
                },
            )
            await self._write(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": "run",
                    "method": "run",
                    "params": {
                        "run_id": request.run_id,
                        "instruction": request.instruction,
                        "root": str(request.root),
                        "state": str(state),
                        "context": request.workspace_context,
                        "base_url": self.settings.code_base_url,
                        "api_key": self.settings.code_api_key,
                        "model": self.settings.code_model,
                        "max_steps": self.settings.code_max_steps,
                        "max_tokens": self.settings.code_max_tokens,
                        "model_timeout": self.settings.code_timeout_seconds,
                        "context_window_tokens": self.settings.pico_context_window_tokens,
                    },
                },
            )
            while True:
                frame = await self._read(process)
                if frame.get("method") == "tool.execute":
                    call_id = frame.get("id")
                    params = frame.get("params", {})
                    if not isinstance(call_id, str) or call_id in seen:
                        raise RuntimeError("Pico worker sent a duplicate or invalid tool request")
                    seen.add(call_id)
                    name, arguments = params.get("name"), params.get("arguments")
                    if name not in ToolRegistry.default() or not isinstance(arguments, dict):
                        raise RuntimeError("Pico worker requested a tool outside the allow-list")
                    # The host supplies run identity, path root and step numbering, never the model.
                    result = await execute(name, arguments, call_id)
                    await self._write(
                        process,
                        {
                            "jsonrpc": "2.0",
                            "id": call_id,
                            "result": {
                                "content": result.content,
                                "is_error": result.is_error,
                            },
                        },
                    )
                elif frame.get("method") == "event":
                    params = frame.get("params", {})
                    kind = params.get("kind")
                    if kind == "text":
                        await emit("code.text", str(params.get("text", ""))[:16000], {})
                    elif kind == "usage":
                        usage = {
                            key: int(params.get(key, 0))
                            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        }
                        await emit("code.usage", "模型用量已记录", usage)
                    else:
                        raise RuntimeError("Pico worker sent an unsupported event")
                elif frame.get("id") == "run":
                    if "error" in frame:
                        # Do not expose raw provider errors, which can include keys and prompts.
                        raise RuntimeError("Pico turn failed; check model/tool compatibility")
                    result = frame.get("result", {})
                    if result.get("status") != "completed":
                        raise RuntimeError("Pico turn did not complete within its execution budget")
                    return str(result.get("summary", ""))[:16000]
                else:
                    raise RuntimeError("Pico worker sent an invalid protocol frame")
        finally:
            # No local shell or child-process tools are registered in this worker.
            await self._stop(process)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _read(process: asyncio.subprocess.Process) -> Dict[str, Any]:
        assert process.stdout is not None
        line = await process.stdout.readline()
        if not line or len(line) > MAX_FRAME_BYTES:
            raise RuntimeError("Pico worker exited or exceeded the protocol frame limit")
        frame = json.loads(line)
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            raise RuntimeError("invalid Pico JSON-RPC frame")
        return frame

    @staticmethod
    async def _write(process: asyncio.subprocess.Process, frame: Dict[str, Any]) -> None:
        assert process.stdin is not None
        data = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise RuntimeError("Pico request exceeds the protocol frame limit")
        process.stdin.write(data)
        await process.stdin.drain()
