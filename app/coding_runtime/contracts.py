from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CodingRuntimeEvent:
    """Provider-neutral event emitted by a coding runtime adapter."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    provider_event: str | None = None


@dataclass(frozen=True, slots=True)
class CodingRuntimeCapabilities:
    steering: bool = False
    follow_up: bool = False
    confirmations: bool = False
    session_resume: bool = False
    read_only: bool = True
    approved_edits: bool = False
    container_commands: bool = False


@dataclass(frozen=True, slots=True)
class CodingModel:
    profile_id: int
    model_id: str
    provider_family: str
    is_local: bool
    context_window: int = 32768
    max_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class CodingRuntimeStart:
    workspace: Path
    model: CodingModel
    read_tools: tuple[str, ...] = ("read", "grep", "find", "ls")
    write_tools: tuple[str, ...] = ()
    command_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodingPrompt:
    message: str
    history: tuple[tuple[str, str], ...] = ()


class CodingRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class CodingRuntime(Protocol):
    def capabilities(self) -> CodingRuntimeCapabilities: ...
    async def start(self, request: CodingRuntimeStart) -> None: ...
    def run(self, prompt: CodingPrompt) -> AsyncIterator[CodingRuntimeEvent]: ...
    async def steer(self, message: str) -> None: ...
    async def follow_up(self, message: str) -> None: ...
    async def confirm(self, request_id: str, approved: bool) -> None: ...
    async def command_result(self, request_id: str, result: dict) -> None: ...
    async def abort(self) -> None: ...
    async def close(self) -> None: ...
