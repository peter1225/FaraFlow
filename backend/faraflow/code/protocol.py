import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional


class CodeProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class CodeDecision:
    kind: Literal["tool", "final"]
    raw_response: str
    tool_name: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    answer: Optional[str] = None


_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FINAL_RE = re.compile(r"<final>\s*(.*?)\s*</final>", re.DOTALL)


def parse_code_response(content: str) -> CodeDecision:
    tool_matches = list(_TOOL_RE.finditer(content))
    final_matches = list(_FINAL_RE.finditer(content))
    if len(tool_matches) + len(final_matches) != 1:
        raise CodeProtocolError("response must contain exactly one action block")
    if tool_matches:
        tool_match = tool_matches[0]
        try:
            payload = json.loads(tool_match.group(1))
        except json.JSONDecodeError as exc:
            raise CodeProtocolError(f"invalid tool JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise CodeProtocolError("tool call must be a JSON object")
        name = payload.get("name")
        arguments = payload.get("args", {})
        if not isinstance(name, str) or not name.strip():
            raise CodeProtocolError("tool call name is required")
        if not isinstance(arguments, dict):
            raise CodeProtocolError("tool call args must be an object")
        return CodeDecision(
            kind="tool",
            raw_response=content,
            tool_name=name.strip(),
            arguments=arguments,
        )
    if final_matches:
        final_match = final_matches[0]
        return CodeDecision(
            kind="final",
            raw_response=content,
            answer=final_match.group(1).strip(),
        )
    raise CodeProtocolError("response must contain one <tool_call> or <final> block")


def build_code_system_prompt(workspace_context: str) -> str:
    return f"""You are FaraFlow's local coding agent. Work only inside the isolated workspace.
Use tools to inspect the repository before changing files. Never claim a change unless a tool
successfully made it. Existing files require read_file before patch_file or delete_file.
Do not request shell commands, commits, pushes, credentials, or files outside the workspace.

Return exactly one action per response, using one of these formats:
<tool_call>{{"name":"read_file","args":{{"path":"README.md","start":1,"end":200}}}}</tool_call>
<final>Concise summary of the work and remaining limitations.</final>

Available tools:
- list_files(path=".")
- read_file(path, start=1, end=200)
- search(pattern, path=".")
- create_file(path, content)
- patch_file(path, old_text, new_text)
- delete_file(path)
- git_status()
- git_diff()

Workspace context:
{workspace_context}
"""
