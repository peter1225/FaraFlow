import re
from pathlib import Path
from typing import Optional

_SAFE_SEGMENT = re.compile(r"^[a-zA-Z0-9_.-]+$")


class ArtifactStore:
    """Filesystem artifact store with traversal-safe references.

    The public interface intentionally returns opaque API paths instead of raw
    filesystem paths. An S3/MinIO adapter can replace this implementation
    without changing the runtime.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _validate(self, value: str) -> str:
        if not _SAFE_SEGMENT.fullmatch(value):
            raise ValueError(f"unsafe artifact path segment: {value!r}")
        return value

    def session_dir(self, session_id: str) -> Path:
        path = self.root / self._validate(session_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_bytes(
        self,
        session_id: str,
        filename: str,
        content: bytes,
        *,
        subdirectory: Optional[str] = None,
    ) -> str:
        base = self.session_dir(session_id)
        if subdirectory:
            base = base / self._validate(subdirectory)
            base.mkdir(parents=True, exist_ok=True)
        safe_name = self._validate(filename)
        target = (base / safe_name).resolve()
        if self.root not in target.parents:
            raise ValueError("artifact target escaped artifact root")
        target.write_bytes(content)
        relative = target.relative_to(self.root).as_posix()
        return f"/v1/artifacts/{relative}"

    def resolve_public_path(self, relative_path: str) -> Path:
        segments = [self._validate(segment) for segment in relative_path.split("/") if segment]
        target = self.root.joinpath(*segments).resolve()
        if self.root not in target.parents:
            raise ValueError("artifact path escaped artifact root")
        return target
