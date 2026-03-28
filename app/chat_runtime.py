from __future__ import annotations

import asyncio
from typing import Optional


class ChatRunManager:
    def __init__(self) -> None:
        self._active_runs: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: int, task: asyncio.Task) -> None:
        async with self._lock:
            self._active_runs[session_id] = task

    async def clear(self, session_id: int, task: Optional[asyncio.Task] = None) -> None:
        async with self._lock:
            current = self._active_runs.get(session_id)
            if current is None:
                return
            if task is not None and current is not task:
                return
            self._active_runs.pop(session_id, None)

    async def request_stop(self, session_id: int) -> bool:
        async with self._lock:
            task = self._active_runs.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def is_active(self, session_id: int) -> bool:
        async with self._lock:
            task = self._active_runs.get(session_id)
        return task is not None and not task.done()


_chat_run_manager: ChatRunManager | None = None


def get_chat_run_manager() -> ChatRunManager:
    global _chat_run_manager
    if _chat_run_manager is None:
        _chat_run_manager = ChatRunManager()
    return _chat_run_manager
