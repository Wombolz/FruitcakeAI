"""
FruitcakeAI v5 — In-memory request metrics.

Simple thread-safe counters for the /admin/metrics endpoint.
No persistence — resets on server restart. No Prometheus needed for a home server.

Usage:
    from app.metrics import metrics
    metrics.inc_requests()
    metrics.inc_tool_calls()
    metrics.ws_connect()
    metrics.ws_disconnect()
    metrics.snapshot()  # → dict for JSON response
"""

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _Metrics:
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    total_requests: int = 0
    error_count: int = 0
    total_tool_calls: int = 0
    active_ws_sessions: int = 0

    def inc_requests(self) -> None:
        with self._lock:
            self.total_requests += 1

    def inc_errors(self) -> None:
        with self._lock:
            self.error_count += 1

    def inc_tool_calls(self, n: int = 1) -> None:
        with self._lock:
            self.total_tool_calls += n

    def ws_connect(self) -> None:
        with self._lock:
            self.active_ws_sessions += 1

    def ws_disconnect(self) -> None:
        with self._lock:
            self.active_ws_sessions = max(0, self.active_ws_sessions - 1)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_requests": self.total_requests,
                "error_count": self.error_count,
                "total_tool_calls": self.total_tool_calls,
                "active_ws_sessions": self.active_ws_sessions,
            }


# Module-level singleton
metrics = _Metrics()
