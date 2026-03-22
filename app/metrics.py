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
from typing import Dict


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
    chat_validation_retry_count: int = 0
    chat_validation_invalid_link_count: int = 0
    chat_validation_empty_retry_count: int = 0
    chat_orchestration_kill_switch_suppressed_count: int = 0
    chat_simple_latency_count: int = 0
    chat_simple_latency_total_ms: float = 0.0
    chat_orchestrated_latency_count: int = 0
    chat_orchestrated_latency_total_ms: float = 0.0
    chat_stage_latency_counts: Dict[str, int] = field(default_factory=dict)
    chat_stage_latency_totals_ms: Dict[str, float] = field(default_factory=dict)
    document_ingest_started_count: int = 0
    document_ingest_succeeded_count: int = 0
    document_ingest_failed_count: int = 0
    document_ingest_recovered_count: int = 0

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

    def inc_chat_validation_retry_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_validation_retry_count += n

    def inc_chat_validation_invalid_link_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_validation_invalid_link_count += n

    def inc_chat_validation_empty_retry_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_validation_empty_retry_count += n

    def inc_chat_orchestration_kill_switch_suppressed_count(self, n: int = 1) -> None:
        with self._lock:
            self.chat_orchestration_kill_switch_suppressed_count += n

    def record_chat_latency(self, *, mode: str, elapsed_ms: float) -> None:
        with self._lock:
            if mode == "chat_orchestrated":
                self.chat_orchestrated_latency_count += 1
                self.chat_orchestrated_latency_total_ms += float(elapsed_ms)
            else:
                self.chat_simple_latency_count += 1
                self.chat_simple_latency_total_ms += float(elapsed_ms)

    def record_chat_stage_latency(self, *, stage: str, elapsed_ms: float) -> None:
        key = str(stage or "").strip().lower()
        if not key:
            return
        with self._lock:
            self.chat_stage_latency_counts[key] = self.chat_stage_latency_counts.get(key, 0) + 1
            self.chat_stage_latency_totals_ms[key] = (
                self.chat_stage_latency_totals_ms.get(key, 0.0) + float(elapsed_ms)
            )

    def inc_document_ingest_started_count(self, n: int = 1) -> None:
        with self._lock:
            self.document_ingest_started_count += n

    def inc_document_ingest_succeeded_count(self, n: int = 1) -> None:
        with self._lock:
            self.document_ingest_succeeded_count += n

    def inc_document_ingest_failed_count(self, n: int = 1) -> None:
        with self._lock:
            self.document_ingest_failed_count += n

    def inc_document_ingest_recovered_count(self, n: int = 1) -> None:
        with self._lock:
            self.document_ingest_recovered_count += n

    def snapshot(self) -> dict:
        with self._lock:
            simple_avg = (
                self.chat_simple_latency_total_ms / self.chat_simple_latency_count
                if self.chat_simple_latency_count
                else 0.0
            )
            orchestrated_avg = (
                self.chat_orchestrated_latency_total_ms / self.chat_orchestrated_latency_count
                if self.chat_orchestrated_latency_count
                else 0.0
            )
            stage_avgs = {
                stage: round(
                    self.chat_stage_latency_totals_ms.get(stage, 0.0)
                    / max(1, self.chat_stage_latency_counts.get(stage, 0)),
                    2,
                )
                for stage in sorted(self.chat_stage_latency_counts)
            }
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
                "chat_validation_retry_count": self.chat_validation_retry_count,
                "chat_validation_invalid_link_count": self.chat_validation_invalid_link_count,
                "chat_validation_empty_retry_count": self.chat_validation_empty_retry_count,
                "chat_orchestration_kill_switch_suppressed_count": self.chat_orchestration_kill_switch_suppressed_count,
                "chat_simple_latency_count": self.chat_simple_latency_count,
                "chat_simple_latency_avg_ms": round(simple_avg, 2),
                "chat_orchestrated_latency_count": self.chat_orchestrated_latency_count,
                "chat_orchestrated_latency_avg_ms": round(orchestrated_avg, 2),
                "chat_orchestration_latency_delta_ms": round(orchestrated_avg - simple_avg, 2),
                "chat_stage_latency_counts": dict(sorted(self.chat_stage_latency_counts.items())),
                "chat_stage_latency_avg_ms": stage_avgs,
                "document_ingest_started_count": self.document_ingest_started_count,
                "document_ingest_succeeded_count": self.document_ingest_succeeded_count,
                "document_ingest_failed_count": self.document_ingest_failed_count,
                "document_ingest_recovered_count": self.document_ingest_recovered_count,
            }


# Module-level singleton
metrics = _Metrics()
