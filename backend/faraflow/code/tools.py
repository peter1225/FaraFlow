import asyncio
import difflib
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from faraflow.infra.repository import Repository
from faraflow.workspace.run_store import CodeRunStore, file_hash
from faraflow.workspace.service import (
    MAX_READ_BYTES,
    MAX_READ_LINES,
    MAX_TEXT_FILE_BYTES,
    WorkspaceService,
)

from .policy import CodePolicy
from .registry import ToolRegistry


@dataclass
class CodeToolResult:
    content: str
    is_error: bool = False
    affected_paths: List[str] = field(default_factory=list)
    diff_summary: List[str] = field(default_factory=list)
    before_hashes: Dict[str, Optional[str]] = field(default_factory=dict)
    after_hashes: Dict[str, Optional[str]] = field(default_factory=dict)
    unified_diff: str = ""


class CodeToolExecutor:
    def __init__(
        self,
        *,
        code_run_id: str,
        session_id: str,
        root: Path,
        baseline_manifest: Dict[str, Optional[str]],
        repository: Repository,
        workspace_service: WorkspaceService,
        run_store: CodeRunStore,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.code_run_id = code_run_id
        self.session_id = session_id
        self.root = root.resolve()
        self.baseline_manifest = baseline_manifest
        self.repository = repository
        self.workspace_service = workspace_service
        self.run_store = run_store
        self.registry = registry or ToolRegistry.default()
        self.policy = CodePolicy(self.root, workspace_service)
        self.changed_paths: Set[str] = set()
        self.previous_calls: Set[str] = set()

    async def execute(
        self, step_no: int, name: str, arguments: Dict[str, Any]
    ) -> CodeToolResult:
        key = json.dumps({"name": name, "args": arguments}, sort_keys=True, ensure_ascii=False)
        if key in self.previous_calls:
            result = CodeToolResult(
                f"error: repeated identical tool call for {name}; choose a different action",
                is_error=True,
            )
        elif name not in self.registry:
            result = CodeToolResult(f"error: unknown tool {name}", is_error=True)
        else:
            self.previous_calls.add(key)
            try:
                result = await asyncio.to_thread(self._dispatch, name, arguments)
            except Exception as exc:
                result = CodeToolResult(
                    f"error: {name} failed: {type(exc).__name__}: {exc}", is_error=True
                )
        await self.repository.append_tool_call(
            code_run_id=self.code_run_id,
            session_id=self.session_id,
            step_no=step_no,
            tool_name=name,
            arguments=self._redact_arguments(name, arguments),
            status="error" if result.is_error else "ok",
            result_excerpt=self.audit_summary(name, result),
            affected_paths=result.affected_paths,
            diff_summary=result.diff_summary,
            before_hashes=result.before_hashes,
            after_hashes=result.after_hashes,
            unified_diff=result.unified_diff,
        )
        return result

    @staticmethod
    def audit_summary(name: str, result: CodeToolResult) -> str:
        if result.is_error:
            return result.content[:1000]
        if name == "read_file":
            return f"read_file completed ({len(result.content)} characters returned to model)"
        if name == "search":
            count = 0 if result.content == "(no matches)" else len(result.content.splitlines())
            return f"search completed ({count} matches returned to model)"
        if name == "list_files":
            count = 0 if result.content == "(empty)" else len(result.content.splitlines())
            return f"list_files completed ({count} entries returned to model)"
        if name == "git_diff":
            return f"git_diff completed ({len(result.content)} characters returned to model)"
        return result.content[:1000]

    def _dispatch(self, name: str, arguments: Dict[str, Any]) -> CodeToolResult:
        if name == "list_files":
            return self._list_files(arguments)
        if name == "read_file":
            return self._read_file(arguments)
        if name == "search":
            return self._search(arguments)
        if name == "create_file":
            return self._create_file(arguments)
        if name == "patch_file":
            return self._patch_file(arguments)
        if name == "delete_file":
            return self._delete_file(arguments)
        if name == "git_status":
            return self._git_status()
        if name == "git_diff":
            return self._git_diff()
        raise ValueError("unsupported tool")

    def _path(self, value: Any, *, allow_missing: bool = False) -> Path:
        return self.policy.path(value, allow_missing=allow_missing)

    def _relative(self, path: Path) -> str:
        return self.policy.relative(path)

    def _list_files(self, arguments: Dict[str, Any]) -> CodeToolResult:
        directory = self._path(arguments.get("path", "."))
        if not directory.is_dir():
            raise ValueError("path is not a directory")
        lines: List[str] = []
        for child in sorted(
            directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
        ):
            relative = child.relative_to(self.root)
            if self.workspace_service.is_sensitive(relative):
                continue
            if child.is_symlink() or self.workspace_service._is_reparse_point(child):
                continue
            kind = "D" if child.is_dir() else "F"
            lines.append(f"[{kind}] {relative.as_posix()}")
            if len(lines) >= 500:
                break
        return CodeToolResult("\n".join(lines) or "(empty)")

    def _read_file(self, arguments: Dict[str, Any]) -> CodeToolResult:
        path = self._path(arguments.get("path"))
        if not path.is_file():
            raise ValueError("path is not a file")
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            raise ValueError("file exceeds 1 MiB text limit")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("binary files are not supported")
        text = raw.decode("utf-8")
        start = int(arguments.get("start", 1))
        end = int(arguments.get("end", start + MAX_READ_LINES - 1))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        end = min(end, start + MAX_READ_LINES - 1)
        lines = text.splitlines()
        selected = "\n".join(
            f"{number:>4}: {line}"
            for number, line in enumerate(lines[start - 1 : end], start=start)
        )
        encoded = selected.encode("utf-8")
        if len(encoded) > MAX_READ_BYTES:
            selected = encoded[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
            selected += "\n...[truncated]"
        relative = self.policy.record_read(path)
        return CodeToolResult(f"# {relative}\n{selected}")

    def _search(self, arguments: Dict[str, Any]) -> CodeToolResult:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        base = self._path(arguments.get("path", "."))
        expression = re.compile(pattern, re.IGNORECASE)
        files = [base] if base.is_file() else self.workspace_service._iter_files(base)
        matches: List[str] = []
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, start=1):
                if expression.search(line):
                    matches.append(f"{self._relative(path)}:{number}:{line}")
                    if len(matches) >= 200:
                        return CodeToolResult("\n".join(matches))
        return CodeToolResult("\n".join(matches) or "(no matches)")

    def _require_fresh_read(self, path: Path) -> str:
        return self.policy.require_fresh_read(path)

    def _create_file(self, arguments: Dict[str, Any]) -> CodeToolResult:
        path = self._path(arguments.get("path"), allow_missing=True)
        if path.exists():
            raise ValueError("create_file only accepts a path that does not exist")
        content = self.policy.validate_text(arguments.get("content"))
        relative = self._relative(path)
        self.run_store.preserve_baseline(self.code_run_id, relative, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self._mutation_result(relative, None, None, content)

    def _patch_file(self, arguments: Dict[str, Any]) -> CodeToolResult:
        path = self._path(arguments.get("path"))
        if not path.is_file():
            raise ValueError("path is not a file")
        relative = self._require_fresh_read(path)
        text = path.read_text(encoding="utf-8")
        before_hash = file_hash(path)
        updated, _ = self.policy.exact_patch(
            text, arguments.get("old_text"), arguments.get("new_text")
        )
        self.run_store.preserve_baseline(self.code_run_id, relative, path)
        path.write_text(updated, encoding="utf-8")
        self.policy.invalidate_read(relative)
        return self._mutation_result(relative, before_hash, text, updated)

    def _delete_file(self, arguments: Dict[str, Any]) -> CodeToolResult:
        path = self._path(arguments.get("path"))
        if not path.is_file():
            raise ValueError("path is not a file")
        relative = self._require_fresh_read(path)
        before_hash = file_hash(path)
        before_text = path.read_text(encoding="utf-8")
        self.run_store.preserve_baseline(self.code_run_id, relative, path)
        path.unlink()
        self.policy.invalidate_read(relative)
        return self._mutation_result(relative, before_hash, before_text, None)

    def _mutation_result(
        self,
        relative: str,
        before_hash: Optional[str],
        before_text: Optional[str],
        after_text: Optional[str],
    ) -> CodeToolResult:
        baseline = self.baseline_manifest.get(relative)
        current = file_hash(self.root / Path(relative))
        if current == baseline:
            self.changed_paths.discard(relative)
        else:
            self.changed_paths.add(relative)
        if baseline is None and current is not None:
            summary = f"created:{relative}"
        elif baseline is not None and current is None:
            summary = f"deleted:{relative}"
        else:
            summary = f"modified:{relative}"
        diff = "".join(
            difflib.unified_diff(
                (before_text or "").splitlines(True),
                (after_text or "").splitlines(True),
                fromfile=f"a/{relative}" if before_text is not None else "/dev/null",
                tofile=f"b/{relative}" if after_text is not None else "/dev/null",
            )
        )
        return CodeToolResult(
            summary,
            affected_paths=[relative],
            diff_summary=[summary],
            before_hashes={relative: before_hash},
            after_hashes={relative: current},
            unified_diff=diff,
        )

    def _git_status(self) -> CodeToolResult:
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return CodeToolResult(result.stdout.strip() or "clean")
        except OSError:
            pass
        return CodeToolResult(
            "\n".join(sorted(self.changed_paths)) or "clean (managed snapshot)"
        )

    def _git_diff(self) -> CodeToolResult:
        diff, changed = self.run_store.build_diff(
            self.code_run_id, self.root, self.changed_paths
        )
        return CodeToolResult(diff or "(no changes)", affected_paths=changed)

    @staticmethod
    def _redact_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        redacted = dict(arguments)
        for key in ("content", "old_text", "new_text"):
            value = redacted.get(key)
            if isinstance(value, str):
                redacted[key] = {
                    "redacted": True,
                    "length": len(value),
                    "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
        return redacted
