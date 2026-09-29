import os
import stat
from pathlib import Path


class DurableFileWriter:
    """Crash-aware same-filesystem replacement used by reviewed apply operations."""

    @classmethod
    def replace_from(
        cls, source: Path, target: Path, operation_id: str
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{operation_id}.tmp")
        data = source.read_bytes()
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        source_mode = stat.S_IMODE(source.stat().st_mode)
        os.chmod(temporary, source_mode)
        os.replace(temporary, target)
        cls._sync_file(target)
        cls._sync_directory(target.parent)

    @classmethod
    def delete(cls, target: Path, operation_id: str) -> None:
        if not target.exists():
            return
        tombstone = target.with_name(f".{target.name}.{operation_id}.delete")
        os.replace(target, tombstone)
        cls._sync_directory(target.parent)
        tombstone.unlink()
        cls._sync_directory(target.parent)

    @staticmethod
    def cleanup(target: Path, operation_id: str) -> None:
        for suffix in ("tmp", "delete"):
            temporary = target.with_name(f".{target.name}.{operation_id}.{suffix}")
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _sync_file(path: Path) -> None:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
