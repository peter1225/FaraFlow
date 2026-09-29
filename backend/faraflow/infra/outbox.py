import asyncio
from typing import Optional

from faraflow.domain.schemas import SessionEvent

from .events import EventBus
from .repository import Repository


class EventOutboxDispatcher:
    def __init__(
        self,
        repository: Repository,
        event_bus: EventBus,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.repository = repository
        self.event_bus = event_bus
        self.poll_interval_seconds = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._closed = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="faraflow-event-outbox"
            )

    async def _run(self) -> None:
        while not self._closed:
            rows = await self.repository.list_pending_event_outbox(limit=100)
            if not rows:
                await asyncio.sleep(self.poll_interval_seconds)
                continue
            for _, record in rows:
                try:
                    await self.event_bus.publish(
                        SessionEvent(
                            event_id=record.event_id,
                            session_id=record.session_id,
                            sequence=record.sequence,
                            event_type=record.event_type,
                            message=record.message,
                            payload=record.payload,
                            created_at=record.created_at,
                        )
                    )
                    await self.repository.mark_event_published(record.event_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.repository.mark_event_publish_failed(
                        record.event_id, f"{type(exc).__name__}: {exc}"
                    )
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
