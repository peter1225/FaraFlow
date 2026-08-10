import asyncio
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from faraflow.config import Settings
from faraflow.domain.schemas import (
    WorkspaceCreate,
    WorkspaceDirectorySelection,
    WorkspaceFileView,
    WorkspaceTreeEntry,
    WorkspaceView,
)
from faraflow.infra.repository import ConflictError, Repository

from .run_store import CodeRunStore, file_hash

IGNORED_NAMES = {
    ".git",
    ".faraflow",
    ".pico",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
    "dist",
    "build",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
SENSITIVE_NAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
MAX_TEXT_FILE_BYTES = 1024 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_READ_LINES = 200


def _run_git(
    root: Path, arguments: List[str], *, check: bool = True, strip: bool = True
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip() if strip else result.stdout


class WorkspaceService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        run_store: CodeRunStore,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.run_store = run_store
        self._directory_picker_lock = asyncio.Lock()

    def assert_enabled(self) -> None:
        if not self.settings.enable_local_workspaces:
            raise ConflictError("local workspaces are disabled")

    def _allowed_root(self, path: Path) -> bool:
        for allowed in self.settings.workspace_allowed_roots:
            try:
                path.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False

    def validate_root(self, raw_path: str) -> Path:
        self.assert_enabled()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("workspace path must be absolute")
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise ValueError("workspace path is not a directory")
        if not self._allowed_root(resolved):
            raise PermissionError("workspace path is outside configured allowed roots")
        return resolved

    async def pick_directory(self) -> WorkspaceDirectorySelection:
        self.assert_enabled()
        async with self._directory_picker_lock:
            try:
                selected = await asyncio.to_thread(self._open_directory_picker)
            except Exception as exc:
                raise ConflictError(f"native folder picker is unavailable: {exc}") from exc
        if not selected:
            return WorkspaceDirectorySelection()
        root = await asyncio.to_thread(self.validate_root, selected)
        return WorkspaceDirectorySelection(path=str(root), name=root.name or str(root))

    def _open_directory_picker(self) -> Optional[str]:
        if os.name != "nt":
            raise RuntimeError("native folder selection is currently supported on Windows")
        import tkinter
        from tkinter import filedialog

        window = tkinter.Tk()
        window.withdraw()
        try:
            window.attributes("-topmost", True)
            window.update_idletasks()
            initial_directory = next(
                (path for path in self.settings.workspace_allowed_roots if path.is_dir()),
                None,
            )
            return filedialog.askdirectory(
                parent=window,
                title="选择 FaraFlow 代码工作区",
                initialdir=str(initial_directory) if initial_directory else None,
                mustexist=True,
            ) or None
        finally:
            window.destroy()

    @staticmethod
    def is_sensitive(relative: Path) -> bool:
        for part in relative.parts:
            lowered = part.lower()
            if lowered in IGNORED_NAMES:
                return True
            if lowered.startswith(".env") and lowered != ".env.example":
                return True
        name = relative.name.lower()
        return name in SENSITIVE_NAMES or relative.suffix.lower() in SENSITIVE_SUFFIXES

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except OSError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    def safe_path(self, root: Path, relative: str, *, allow_missing: bool = False) -> Path:
        requested = Path(relative or ".")
        if requested.is_absolute():
            raise ValueError("workspace tool paths must be relative")
        lexical = root / requested
        current = root
        for part in requested.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise ValueError("path escapes workspace")
            current = current / part
            if current.exists() and (current.is_symlink() or self._is_reparse_point(current)):
                target = current.resolve()
                if target != root and root not in target.parents:
                    raise ValueError("path escapes workspace through a link")
        resolved = lexical.resolve(strict=not allow_missing)
        if resolved != root and root not in resolved.parents:
            raise ValueError("path escapes workspace")
        relative_path = resolved.relative_to(root)
        if self.is_sensitive(relative_path):
            raise PermissionError(f"access to {relative_path.as_posix()} is blocked")
        if relative_path.parts and self._git_ignored(root, [relative_path.as_posix()]):
            raise PermissionError(f"access to ignored path {relative_path.as_posix()} is blocked")
        return resolved

    @staticmethod
    def _git_ignored(root: Path, relative_paths: Iterable[str]) -> Set[str]:
        candidates = list(relative_paths)
        if not candidates:
            return set()
        try:
            git_root = Path(_run_git(root, ["rev-parse", "--show-toplevel"])).resolve()
            if git_root != root:
                workspace_relative = root.relative_to(git_root).as_posix()
                if WorkspaceService._git_ignored(git_root, [workspace_relative]):
                    # A separately registered folder must not become entirely
                    # inaccessible merely because its parent repository ignores it.
                    return set()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            pass
        ignored: Set[str] = set()
        for offset in range(0, len(candidates), 100):
            try:
                result = subprocess.run(
                    ["git", "check-ignore", "--", *candidates[offset : offset + 100]],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return ignored
            if result.returncode not in {0, 1}:
                return ignored
            ignored.update(
                line.strip().replace("\\", "/") for line in result.stdout.splitlines()
            )
        return ignored

    async def register(self, request: WorkspaceCreate) -> WorkspaceView:
        root = await asyncio.to_thread(self.validate_root, request.root_path)
        git_root, branch, dirty = await asyncio.to_thread(self.git_facts, root)
        record = await self.repository.create_workspace(
            request,
            root_path=str(root),
            repository_kind="git" if git_root else "directory",
            git_root=str(git_root) if git_root else None,
            branch=branch,
        )
        return self.to_view(record, is_dirty=dirty)

    async def list(self) -> List[WorkspaceView]:
        rows = await self.repository.list_workspaces()
        facts = await asyncio.gather(
            *(asyncio.to_thread(self.git_facts, Path(record.root_path)) for record in rows)
        )
        views = []
        for record, (_, branch, dirty) in zip(rows, facts):
            record.branch = branch
            views.append(self.to_view(record, is_dirty=dirty))
        return views

    async def get(self, workspace_id: str) -> WorkspaceView:
        record = await self.repository.get_workspace(workspace_id)
        _, branch, dirty = await asyncio.to_thread(
            self.git_facts, Path(record.root_path)
        )
        record.branch = branch
        return self.to_view(record, is_dirty=dirty)

    async def deactivate(self, workspace_id: str) -> None:
        await self.repository.deactivate_workspace(workspace_id)

    @staticmethod
    def to_view(record: Any, *, is_dirty: bool) -> WorkspaceView:
        return WorkspaceView(
            workspace_id=record.workspace_id,
            name=record.name,
            root_path=record.root_path,
            repository_kind=record.repository_kind,
            git_root=record.git_root,
            branch=record.branch,
            is_dirty=is_dirty,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def git_facts(root: Path) -> Tuple[Optional[Path], Optional[str], bool]:
        try:
            git_root_text = _run_git(root, ["rev-parse", "--show-toplevel"])
            git_root = Path(git_root_text).resolve()
            if git_root != root:
                relative_root = root.relative_to(git_root).as_posix()
                if WorkspaceService._git_ignored(git_root, [relative_root]):
                    return None, None, False
            branch = _run_git(root, ["branch", "--show-current"], check=False) or None
            dirty = bool(_run_git(root, ["status", "--porcelain"], check=False))
            return git_root, branch, dirty
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None, None, False

    async def tree(self, workspace_id: str, relative: str = ".") -> List[WorkspaceTreeEntry]:
        record = await self.repository.get_workspace(workspace_id)
        return await asyncio.to_thread(self._tree, Path(record.root_path), relative)

    def _tree(self, root: Path, relative: str) -> List[WorkspaceTreeEntry]:
        directory = self.safe_path(root, relative)
        if not directory.is_dir():
            raise ValueError("path is not a directory")
        entries: List[WorkspaceTreeEntry] = []
        children = sorted(
            directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
        )
        ignored = self._git_ignored(
            root, [child.relative_to(root).as_posix() for child in children]
        )
        for child in children:
            rel = child.relative_to(root)
            if (
                self.is_sensitive(rel)
                or rel.as_posix() in ignored
                or child.is_symlink()
                or self._is_reparse_point(child)
            ):
                continue
            entries.append(
                WorkspaceTreeEntry(
                    path=rel.as_posix(),
                    name=child.name,
                    kind="directory" if child.is_dir() else "file",
                    size=child.stat().st_size if child.is_file() else None,
                )
            )
            if len(entries) >= 500:
                break
        return entries

    async def read_file(
        self, workspace_id: str, relative: str, start: int = 1, end: int = 200
    ) -> WorkspaceFileView:
        record = await self.repository.get_workspace(workspace_id)
        return await asyncio.to_thread(
            self._read_file, Path(record.root_path), relative, start, end
        )

    def _read_file(
        self, root: Path, relative: str, start: int, end: int
    ) -> WorkspaceFileView:
        path = self.safe_path(root, relative)
        if not path.is_file():
            raise ValueError("path is not a file")
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            raise ValueError("file exceeds 1 MiB text limit")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("binary files are not supported")
        text = raw.decode("utf-8")
        lines = text.splitlines()
        start = max(1, start)
        end = min(max(start, end), start + MAX_READ_LINES - 1, len(lines) or 1)
        content = "\n".join(lines[start - 1 : end])
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_READ_BYTES:
            content = encoded[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        return WorkspaceFileView(
            path=Path(relative).as_posix(),
            content=content,
            start_line=start,
            end_line=end,
            total_lines=len(lines),
        )

    def snapshot_manifest(self, root: Path) -> Dict[str, Optional[str]]:
        manifest: Dict[str, Optional[str]] = {}
        for path in self._iter_files(root):
            relative = path.relative_to(root).as_posix()
            manifest[relative] = file_hash(path)
        return manifest

    def _iter_files(self, root: Path) -> Iterable[Path]:
        for directory, names, files in os.walk(root):
            base = Path(directory)
            names[:] = [
                name
                for name in names
                if name.lower() not in IGNORED_NAMES
                and not (base / name).is_symlink()
                and not self._is_reparse_point(base / name)
            ]
            for name in files:
                path = base / name
                relative = path.relative_to(root)
                if self.is_sensitive(relative) or path.is_symlink() or self._is_reparse_point(path):
                    continue
                try:
                    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
                        continue
                    raw = path.read_bytes()
                    if b"\x00" in raw:
                        continue
                    raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                yield path

    def prepare_isolation(self, code_run_id: str, workspace: Any) -> Dict[str, Any]:
        source_root = Path(workspace.root_path).resolve()
        target = (self.settings.code_work_root / code_run_id).resolve()
        if target.exists():
            raise ConflictError("code run isolation directory already exists")
        git_root, _, dirty = self.git_facts(source_root)
        base_revision: Optional[str] = None
        isolation_kind = "snapshot"
        if git_root is not None and git_root == source_root and not dirty:
            base_revision = _run_git(source_root, ["rev-parse", "HEAD"])
            target.parent.mkdir(parents=True, exist_ok=True)
            _run_git(source_root, ["worktree", "add", "--detach", str(target), base_revision])
            isolation_kind = "worktree"
        else:
            target.mkdir(parents=True, exist_ok=False)
            if git_root is not None:
                base_revision = _run_git(source_root, ["rev-parse", "HEAD"], check=False) or None
                listed = _run_git(
                    source_root,
                    ["ls-files", "-co", "--exclude-standard", "-z"],
                    check=False,
                    strip=False,
                )
                relative_paths = [item for item in listed.split("\x00") if item]
                self._copy_selected(source_root, target, relative_paths)
            else:
                self._copy_tree(source_root, target)
        manifest = self.snapshot_manifest(target)
        self.run_store.save_manifest(code_run_id, "baseline-manifest", manifest)
        return {
            "isolation_kind": isolation_kind,
            "isolated_path": str(target),
            "base_revision": base_revision,
            "baseline_manifest": manifest,
        }

    def _copy_selected(self, source: Path, target: Path, relative_paths: Iterable[str]) -> None:
        for relative in relative_paths:
            try:
                source_path = self.safe_path(source, relative)
            except (ValueError, PermissionError, OSError):
                continue
            if not source_path.is_file():
                continue
            if (
                source_path.stat().st_size > MAX_TEXT_FILE_BYTES
                or b"\x00" in source_path.read_bytes()
            ):
                continue
            try:
                source_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            destination = target / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)

    def _copy_tree(self, source: Path, target: Path) -> None:
        for source_path in self._iter_files(source):
            relative = source_path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)

    def cleanup_isolation(self, record: Any, workspace: Any) -> None:
        if not record.isolated_path:
            return
        target = Path(record.isolated_path).resolve()
        work_root = self.settings.code_work_root.resolve()
        if target != work_root and work_root not in target.parents:
            raise ValueError("refusing to clean an isolation path outside code work root")
        if not target.exists():
            return
        if record.isolation_kind == "worktree" and workspace.git_root:
            _run_git(Path(workspace.git_root), ["worktree", "remove", "--force", str(target)])
        else:
            shutil.rmtree(target)
