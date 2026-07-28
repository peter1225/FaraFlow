from __future__ import annotations

import asyncio
from collections import defaultdict

from faraflow.domain.schemas import SessionEvent


class EventBus:
    def __init__(self) -> None:
        self._subscribers: defaultdict[str, set[asyncio.Queue[SessionEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> asyncio.Queue[SessionEvent]:
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers[session_id].add(queue)
        return queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[SessionEvent]) -> None:
        async with self._lock:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    async def publish(self, event: SessionEvent) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(event.session_id, set()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def subscriber_counts(self) -> dict[str, int]:
        return {session_id: len(queues) for session_id, queues in self._subscribers.items()}
