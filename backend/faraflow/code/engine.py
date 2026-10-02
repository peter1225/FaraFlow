"""Engine seam: runtimes own files and review, engines own model turns."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from faraflow.config import Settings

from .adapter import CodeAdapter
from .protocol import build_code_system_prompt
from .tools import CodeToolResult

ExecuteTool = Callable[[str, Dict[str, Any], str], Awaitable[CodeToolResult]]
EmitEvent = Callable[[str, str, Dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class EngineRequest:
    run_id: str
    instruction: str
    root: Path
    workspace_context: str
    source_root: Optional[Path] = None


class CodeEngine(Protocol):
    async def health(self) -> Dict[str, Any]: ...

    async def run(self, request: EngineRequest, execute: ExecuteTool, emit: EmitEvent) -> str: ...

    async def close(self) -> None: ...

    async def release(self, run_id: str) -> None: ...


class NativeCodeEngine:
    def __init__(self, settings: Settings, adapter: CodeAdapter) -> None:
        self.settings = settings
        self.adapter = adapter

    async def health(self) -> Dict[str, Any]:
        return {"status": "ok", "engine": "native"}

    async def run(self, request: EngineRequest, execute: ExecuteTool, emit: EmitEvent) -> str:
        messages: List[Dict[str, str]] = [{"role": "user", "content": request.instruction}]
        prompt = build_code_system_prompt(request.workspace_context)
        for step in range(1, self.settings.code_max_steps + 1):
            decision = await asyncio.wait_for(
                self.adapter.next_decision(messages, system_prompt=prompt),
                timeout=self.settings.code_timeout_seconds,
            )
            messages.append({"role": "assistant", "content": decision.raw_response})
            if decision.kind == "final":
                return decision.answer or "代码任务已完成"
            assert decision.tool_name is not None
            result = await execute(decision.tool_name, decision.arguments or {}, str(step))
            messages.append(
                {
                    "role": "user",
                    "content": f"Tool result for {decision.tool_name}:\n{result.content}",
                }
            )
        raise RuntimeError("code run exceeded maximum model steps")

    async def close(self) -> None:
        return None

    async def release(self, run_id: str) -> None:
        return None
