import difflib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CodeRunStore:
    def __init__(self, artifact_root: Path) -> None:
        self.root = (artifact_root / "code-runs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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

    def preserve_apply_backup(self, code_run_id: str, relative: str, source: Path) -> None:
        target = self._safe_artifact_path(code_run_id, "apply-backup", relative)
        marker = target.with_name(target.name + ".missing.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copy2(source, target)
        else:
            marker.write_text('{"missing":true}', encoding="utf-8")

    def baseline_bytes(self, code_run_id: str, relative: str) -> Optional[bytes]:
        path = self._safe_artifact_path(code_run_id, "baseline", relative)
        return path.read_bytes() if path.exists() else None

    def backup_bytes(self, code_run_id: str, relative: str) -> Optional[bytes]:
        path = self._safe_artifact_path(code_run_id, "apply-backup", relative)
        return path.read_bytes() if path.exists() else None

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

    def diff_reference(self, code_run_id: str) -> str:
        return f"/v1/artifacts/code-runs/{code_run_id}/diff.patch"

    def save_manifest(self, code_run_id: str, name: str, manifest: Dict[str, object]) -> None:
        path = self.run_dir(code_run_id) / f"{name}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
