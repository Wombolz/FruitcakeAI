from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Protocol


Formatter = Callable[[Dict[str, Any], bool], str]


@dataclass(frozen=True)
class AdapterExecutionResult:
    normalized: Dict[str, Any]
    formatter: Formatter
    raw_payload: Any | None = None


class APIAdapter(Protocol):
    service_name: str

    async def execute(
        self,
        *,
        endpoint: str,
        query_params: Dict[str, Any],
        api_key: str | None,
    ) -> AdapterExecutionResult:
        ...
