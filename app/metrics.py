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
    task_model_planning_large_calls: int = 0
    task_model_execution_small_calls: int = 0
    task_model_final_large_calls: int = 0
    task_model_fallback_to_large_count: int = 0
    task_model_fallback_success_count: int = 0
    task_model_fallback_failure_count: int = 0
    scheduler_llm_unavailable_ticks: int = 0
    scheduler_dispatch_suppressed_count: int = 0
    scheduler_recurring_backlog_skipped_count: int = 0
    scheduler_stale_running_recovered_count: int = 0
    chat_complexity_simple_count: int = 0
    chat_complexity_complex_count: int = 0
    chat_complexity_routed_complex_count: int = 0

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

    def inc_task_model_planning_large_calls(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_planning_large_calls += n

    def inc_task_model_execution_small_calls(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_execution_small_calls += n

    def inc_task_model_final_large_calls(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_final_large_calls += n

    def inc_task_model_fallback_to_large_count(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_fallback_to_large_count += n

    def inc_task_model_fallback_success_count(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_fallback_success_count += n

    def inc_task_model_fallback_failure_count(self, n: int = 1) -> None:
        with self._lock:
            self.task_model_fallback_failure_count += n

    def inc_scheduler_llm_unavailable_ticks(self, n: int = 1) -> None:
        with self._lock:
            self.scheduler_llm_unavailable_ticks += n

    def inc_scheduler_dispatch_suppressed_count(self, n: int = 1) -> None:
        with self._lock:
            self.scheduler_dispatch_suppressed_count += n

    def inc_scheduler_recurring_backlog_skipped_count(self, n: int = 1) -> None:
        with self._lock:
            self.scheduler_recurring_backlog_skipped_count += n

    def inc_scheduler_stale_running_recovered_count(self, n: int = 1) -> None:
        with self._lock:
            self.scheduler_stale_running_recovered_count += n

    def inc_chat_complexity_simple_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_complexity_simple_count += n

    def inc_chat_complexity_complex_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_complexity_complex_count += n

    def inc_chat_complexity_routed_complex_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_complexity_routed_complex_count += n

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_requests": self.total_requests,
                "error_count": self.error_count,
                "total_tool_calls": self.total_tool_calls,
                "active_ws_sessions": self.active_ws_sessions,
                "task_model_planning_large_calls": self.task_model_planning_large_calls,
                "task_model_execution_small_calls": self.task_model_execution_small_calls,
                "task_model_final_large_calls": self.task_model_final_large_calls,
                "task_model_fallback_to_large_count": self.task_model_fallback_to_large_count,
                "task_model_fallback_success_count": self.task_model_fallback_success_count,
                "task_model_fallback_failure_count": self.task_model_fallback_failure_count,
                "scheduler_llm_unavailable_ticks": self.scheduler_llm_unavailable_ticks,
                "scheduler_dispatch_suppressed_count": self.scheduler_dispatch_suppressed_count,
                "scheduler_recurring_backlog_skipped_count": self.scheduler_recurring_backlog_skipped_count,
                "scheduler_stale_running_recovered_count": self.scheduler_stale_running_recovered_count,
                "chat_complexity_simple_count": self.chat_complexity_simple_count,
                "chat_complexity_complex_count": self.chat_complexity_complex_count,
                "chat_complexity_routed_complex_count": self.chat_complexity_routed_complex_count,
            }


# Module-level singleton
metrics = _Metrics()
