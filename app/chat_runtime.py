from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RecentPromptState:
    normalized_prompt: str
    fingerprint: str
    timestamp_monotonic: float
    active: bool


@dataclass
class RecentSendIdState:
    client_send_id: str
    timestamp_monotonic: float
    active: bool


class ChatRunManager:
    def __init__(self, *, duplicate_window_seconds: float = 300.0) -> None:
        self._active_runs: dict[int, asyncio.Task] = {}
        self._recent_prompts: dict[int, RecentPromptState] = {}
        self._recent_send_ids: dict[int, RecentSendIdState] = {}
        self._lock = asyncio.Lock()
        self._duplicate_window_seconds = duplicate_window_seconds

    @staticmethod
    def normalize_prompt(prompt: str) -> str:
        return " ".join(str(prompt or "").split()).strip()

    @staticmethod
    def fingerprint_prompt(prompt: str) -> str:
        normalized = ChatRunManager.normalize_prompt(prompt)
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

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

    async def claim_prompt(self, session_id: int, prompt: str) -> tuple[bool, bool, str]:
        normalized = self.normalize_prompt(prompt)
        fingerprint = self.fingerprint_prompt(prompt)
        if not normalized:
            return True, False, fingerprint

        now = time.monotonic()
        async with self._lock:
            current = self._recent_prompts.get(session_id)
            if (
                current is not None
                and current.normalized_prompt == normalized
                and (now - current.timestamp_monotonic) <= self._duplicate_window_seconds
            ):
                return False, current.active, current.fingerprint

            self._recent_prompts[session_id] = RecentPromptState(
                normalized_prompt=normalized,
                fingerprint=fingerprint,
                timestamp_monotonic=now,
                active=True,
            )
            return True, False, fingerprint

    async def claim_client_send_id(self, session_id: int, client_send_id: str) -> tuple[bool, bool]:
        normalized = str(client_send_id or "").strip()
        if not normalized:
            return True, False

        now = time.monotonic()
        async with self._lock:
            current = self._recent_send_ids.get(session_id)
            if (
                current is not None
                and current.client_send_id == normalized
                and (now - current.timestamp_monotonic) <= self._duplicate_window_seconds
            ):
                return False, current.active

            self._recent_send_ids[session_id] = RecentSendIdState(
                client_send_id=normalized,
                timestamp_monotonic=now,
                active=True,
            )
            return True, False

    async def mark_prompt_finished(self, session_id: int, prompt: str) -> None:
        normalized = self.normalize_prompt(prompt)
        if not normalized:
            return

        now = time.monotonic()
        async with self._lock:
            current = self._recent_prompts.get(session_id)
            if current is None or current.normalized_prompt != normalized:
                return
            current.active = False
            current.timestamp_monotonic = now

    async def mark_client_send_id_finished(self, session_id: int, client_send_id: str) -> None:
        normalized = str(client_send_id or "").strip()
        if not normalized:
            return

        now = time.monotonic()
        async with self._lock:
            current = self._recent_send_ids.get(session_id)
            if current is None or current.client_send_id != normalized:
                return
            current.active = False
            current.timestamp_monotonic = now


_chat_run_manager: ChatRunManager | None = None


def get_chat_run_manager() -> ChatRunManager:
    global _chat_run_manager
    if _chat_run_manager is None:
        _chat_run_manager = ChatRunManager()
    return _chat_run_manager
