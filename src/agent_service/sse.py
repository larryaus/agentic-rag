"""Tiny in-process SSE event channel.

Producer-side calls `await channel.send(event_name, data)`; consumer-side
iterates `channel.events()` to receive `{"event": ..., "data": ...}` dicts
suitable for `sse_starlette.EventSourceResponse`.
"""

import asyncio
import json
from typing import Any, AsyncIterator


class SSEChannel:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

    async def send(self, event: str, data: Any) -> None:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        await self._queue.put((event, payload))

    async def close(self) -> None:
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            event, payload = item
            yield {"event": event, "data": payload}
