import difflib
import hashlib
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


def file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CodeRunStore:
    def __init__(self, artifact_root: Path, quota_bytes: Optional[int] = None) -> None:
        self.root = (artifact_root / "code-runs").resolve()
        self.blob_root = (artifact_root / "blobs" / "sha256").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.quota_bytes = quota_bytes
        self._blob_lock = threading.RLock()

    def put_blob(self, content: bytes) -> str:
        with self._blob_lock:
            return self._put_blob(content)

    def _put_blob(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.blob_root / digest[:2] / digest
        if target.exists():
            if file_hash(target) != digest:
                raise ValueError(f"content-addressed blob is corrupt: {digest}")
            return digest
        if (
            self.quota_bytes is not None
            and self.blob_usage_bytes() + len(content) > self.quota_bytes
        ):
            raise RuntimeError("code artifact quota exceeded; run artifact GC or raise the quota")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return digest

    def materialize_blob(self, digest: str, target: Path) -> None:
        source = self.blob_root / digest[:2] / digest
        if not source.is_file() or file_hash(source) != digest:
            raise ValueError(f"content-addressed blob is missing or corrupt: {digest}")
        self._copy_snapshot(source, target)

    @staticmethod
    def _copy_snapshot(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Each review owns its bytes: changing a review must not alter the CAS
        # object or any other review, including snapshots made by older versions.
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def detach_legacy_review_links(self) -> int:
        """Upgrade old hard-linked reviews without changing their stored bytes."""
        detached = 0
        for path in self.root.glob("*/reviews/**/*"):
            if path.is_file() and path.stat().st_nlink > 1:
                self._copy_snapshot(path, path)
                detached += 1
        return detached

    def run_dir(self, code_run_id: str) -> Path:
        if not code_run_id.replace("_", "").isalnum():
            raise ValueError("invalid code run id")
        directory = (self.root / code_run_id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _safe_artifact_path(self, code_run_id: str, category: str, relative: str) -> Path:
        base = (self.run_dir(code_run_id) / category).resolve()
        target = (base / Path(relative)).resolve()
        if target != base and base not in target.parents:
            raise ValueError("artifact path escapes run directory")
        return target

    def preserve_baseline(self, code_run_id: str, relative: str, source: Path) -> None:
        target = self._safe_artifact_path(code_run_id, "baseline", relative)
        marker = target.with_name(target.name + ".missing.json")
        if target.exists() or marker.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copy2(source, target)
        else:
            marker.write_text('{"missing":true}', encoding="utf-8")

    def ensure_baseline(
        self, code_run_id: str, relative: str, source: Path, expected_hash: Optional[str]
    ) -> None:
        target = self._safe_artifact_path(code_run_id, "baseline", relative)
        marker = target.with_name(target.name + ".missing.json")
        if target.exists() or marker.exists():
            return
        if file_hash(source) != expected_hash:
            raise ValueError(f"original baseline content is unavailable: {relative}")
        self.preserve_baseline(code_run_id, relative, source)

    def preserve_apply_backup(self, code_run_id: str, relative: str, source: Path) -> Optional[str]:
        target = self._safe_artifact_path(code_run_id, "apply-backup", relative)
        marker = target.with_name(target.name + ".missing.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            if marker.exists():
                marker.unlink()
            shutil.copy2(source, target)
            return file_hash(target)
        else:
            if target.exists():
                target.unlink()
            marker.write_text('{"missing":true}', encoding="utf-8")
            return None

    def baseline_bytes(self, code_run_id: str, relative: str) -> Optional[bytes]:
        path = self._safe_artifact_path(code_run_id, "baseline", relative)
        return path.read_bytes() if path.exists() else None

    def backup_bytes(self, code_run_id: str, relative: str) -> Optional[bytes]:
        path = self.backup_path(code_run_id, relative)
        return path.read_bytes() if path.exists() else None

    def backup_path(self, code_run_id: str, relative: str) -> Path:
        return self._safe_artifact_path(code_run_id, "apply-backup", relative)

    def preserve_review(
        self,
        code_run_id: str,
        revision: int,
        isolated_root: Path,
        changed_paths: Iterable[str],
    ) -> Dict[str, Optional[str]]:
        manifest: Dict[str, Optional[str]] = {}
        category = f"reviews/{revision}"
        for relative in sorted(set(changed_paths)):
            source = (isolated_root / Path(relative)).resolve()
            if source != isolated_root and isolated_root not in source.parents:
                raise ValueError("review source escapes isolated workspace")
            target = self._safe_artifact_path(code_run_id, category, relative)
            marker = target.with_name(target.name + ".missing.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                digest = self.put_blob(source.read_bytes())
                self.materialize_blob(digest, target)
                manifest[relative] = digest
            else:
                marker.write_text('{"missing":true}', encoding="utf-8")
                manifest[relative] = None
        return manifest

    def cleanup_run_artifacts(self, code_run_id: str, *, keep_backups: bool = False) -> None:
        directory = (self.root / code_run_id).resolve()
        if directory != self.root and self.root in directory.parents and directory.exists():
            if not keep_backups:
                shutil.rmtree(directory)
                return
            for entry in directory.iterdir():
                if entry.name in {"apply-backup", "applied-manifest.json"}:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()

    def garbage_collect_blobs(self, referenced: Iterable[str]) -> Dict[str, int]:
        keep = set(referenced)
        deleted_files = 0
        deleted_bytes = 0
        for path in self.blob_root.glob("*/*"):
            if not path.is_file() or path.name in keep:
                continue
            size = path.stat().st_size
            path.unlink()
            deleted_files += 1
            deleted_bytes += size
        for directory in self.blob_root.iterdir():
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        return {"deleted_files": deleted_files, "deleted_bytes": deleted_bytes}

    def blob_usage_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.blob_root.glob("*/*") if path.is_file())

    def review_path(self, code_run_id: str, revision: int, relative: str) -> Path:
        return self._safe_artifact_path(code_run_id, f"reviews/{revision}", relative)

    def review_diff_path(self, code_run_id: str, revision: int) -> Path:
        return self.run_dir(code_run_id) / f"diff-{revision}.patch"

    def build_diff(
        self, code_run_id: str, isolated_root: Path, changed_paths: Iterable[str]
    ) -> Tuple[str, List[str]]:
        chunks: List[str] = []
        normalized = sorted(set(changed_paths))
        for relative in normalized:
            old_bytes = self.baseline_bytes(code_run_id, relative)
            current = (isolated_root / Path(relative)).resolve()
            new_bytes = current.read_bytes() if current.exists() else None
            old_text = (old_bytes or b"").decode("utf-8", errors="replace").splitlines(True)
            new_text = (new_bytes or b"").decode("utf-8", errors="replace").splitlines(True)
            chunks.extend(
                difflib.unified_diff(
                    old_text,
                    new_text,
                    fromfile=f"a/{relative}" if old_bytes is not None else "/dev/null",
                    tofile=f"b/{relative}" if new_bytes is not None else "/dev/null",
                )
            )
        diff = "".join(chunks)
        path = self.run_dir(code_run_id) / "diff.patch"
        path.write_text(diff, encoding="utf-8")
        return diff, normalized

    def preserve_review_diff(self, code_run_id: str, revision: int, diff: str) -> str:
        path = self.review_diff_path(code_run_id, revision)
        path.write_text(diff, encoding="utf-8")
        return f"/v1/artifacts/code-runs/{code_run_id}/diff-{revision}.patch"

    def diff_reference(self, code_run_id: str) -> str:
        return f"/v1/artifacts/code-runs/{code_run_id}/diff.patch"

    def save_manifest(self, code_run_id: str, name: str, manifest: Mapping[str, object]) -> None:
        path = self.run_dir(code_run_id) / f"{name}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
