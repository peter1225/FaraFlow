from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from faraflow.workspace.run_store import file_hash
from faraflow.workspace.service import MAX_TEXT_FILE_BYTES, WorkspaceService


class CodePolicy:
    """Stateful path and read-before-write policy for one isolated code run."""

    def __init__(self, root: Path, workspace_service: WorkspaceService) -> None:
        self.root = root.resolve()
        self.workspace_service = workspace_service
        self._fresh_reads: Dict[str, Optional[str]] = {}

    def path(self, value: Any, *, allow_missing: bool = False) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be a non-empty string")
        return self.workspace_service.safe_path(
            self.root, value.strip(), allow_missing=allow_missing
        )

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def record_read(self, path: Path) -> str:
        relative = self.relative(path)
        self._fresh_reads[relative] = file_hash(path)
        return relative

    def require_fresh_read(self, path: Path) -> str:
        relative = self.relative(path)
        if relative not in self._fresh_reads:
            raise PermissionError(f"read_file is required before modifying {relative}")
        if self._fresh_reads[relative] != file_hash(path):
            raise PermissionError(f"{relative} changed after it was read; read it again")
        return relative

    def invalidate_read(self, relative: str) -> None:
        self._fresh_reads.pop(relative, None)

    @staticmethod
    def validate_text(content: Any, *, field: str = "content") -> str:
        if not isinstance(content, str):
            raise ValueError(f"{field} must be a string")
        if len(content.encode("utf-8")) > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"{field} exceeds 1 MiB text limit")
        return content

    @classmethod
    def exact_patch(cls, text: str, old_text: Any, new_text: Any) -> Tuple[str, str]:
        old = cls.validate_text(old_text, field="old_text")
        new = cls.validate_text(new_text, field="new_text")
        if not old:
            raise ValueError("old_text must be a non-empty string")
        count = text.count(old)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        updated = text.replace(old, new, 1)
        cls.validate_text(updated, field="updated file")
        return updated, old
