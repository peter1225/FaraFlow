import shutil
from datetime import timedelta
from typing import Any, Dict

from faraflow.config import Settings
from faraflow.domain.enums import CodeRunStatus
from faraflow.infra.async_utils import run_sync
from faraflow.infra.repository import Repository, now_utc
from faraflow.workspace.run_store import CodeRunStore


class CodeArtifactGarbageCollector:
    def __init__(self, settings: Settings, repository: Repository, run_store: CodeRunStore) -> None:
        self.settings = settings
        self.repository = repository
        self.run_store = run_store

    async def collect(self) -> Dict[str, Any]:
        review_cutoff = now_utc() - timedelta(days=self.settings.code_review_retention_days)
        expired_reviews = await self.repository.list_expired_terminal_code_runs(review_cutoff)
        for run in expired_reviews:
            await run_sync(
                self.run_store.cleanup_run_artifacts,
                run.code_run_id,
                keep_backups=run.status == CodeRunStatus.APPLIED.value,
            )
            await self.repository.expire_code_run_artifacts(run.code_run_id)

        pico_cutoff = now_utc() - timedelta(days=self.settings.pico_state_retention_days)
        expired_pico = await self.repository.list_expired_terminal_code_runs(pico_cutoff)
        pico_root = self.settings.pico_state_root.resolve()
        removed_pico = 0
        for run in expired_pico:
            target = (pico_root / run.code_run_id).resolve()
            if target != pico_root and pico_root in target.parents and target.exists():
                await run_sync(shutil.rmtree, target)
                removed_pico += 1

        referenced = await self.repository.list_referenced_review_hashes()
        deleted = await run_sync(self.run_store.garbage_collect_blobs, referenced)
        usage = await run_sync(self.run_store.blob_usage_bytes)
        quota = int(self.settings.code_artifact_quota_gb * 1024 * 1024 * 1024)
        return {
            "expired_runs": len(expired_reviews),
            "removed_pico_states": removed_pico,
            **deleted,
            "blob_usage_bytes": usage,
            "quota_bytes": quota,
            "quota_exceeded": usage > quota,
        }
