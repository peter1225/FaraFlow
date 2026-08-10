from dataclasses import dataclass
from typing import Dict, Iterable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mutates_workspace: bool = False


class ToolRegistry:
    """Explicit allow-list for every operation exposed to the coding model."""

    def __init__(self, tools: Iterable[ToolSpec]) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate code tool registration: {tool.name}")
            self._tools[tool.name] = tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> Iterable[str]:
        return self._tools.keys()

    @classmethod
    def default(cls) -> "ToolRegistry":
        return cls(
            [
                ToolSpec("list_files"),
                ToolSpec("read_file"),
                ToolSpec("search"),
                ToolSpec("create_file", mutates_workspace=True),
                ToolSpec("patch_file", mutates_workspace=True),
                ToolSpec("delete_file", mutates_workspace=True),
                ToolSpec("git_status"),
                ToolSpec("git_diff"),
            ]
        )
