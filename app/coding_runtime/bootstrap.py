"""Application lifecycle wiring; no runtime process starts during construction."""
from __future__ import annotations
from pathlib import Path

from app.coding_runtime.service import CodingServiceLimits, CodingSessionService

_service: CodingSessionService | None = None


def get_coding_session_service() -> CodingSessionService | None:
    from app.config import settings

    global _service
    if not settings.coding_runtime_enabled:
        return None
    if _service is None:
        from app.coding_runtime.pi_runtime import PiCodingRuntime
        from app.coding_runtime.container_commands import DockerCommandRunner
        from app.db.session import AsyncSessionLocal

        _service = CodingSessionService(
            AsyncSessionLocal,
            lambda: PiCodingRuntime(
                executable=settings.coding_runtime_executable,
                base_url=settings.local_api_base,
                api_key=settings.local_api_key,
                require_read_approval=settings.coding_require_read_approval,
            ),
            limits=CodingServiceLimits(
                max_active_sessions=settings.coding_max_active_sessions,
                idle_seconds=settings.coding_runtime_idle_seconds,
                run_seconds=settings.coding_run_timeout_seconds,
            ),
            admin_roles=tuple(settings.admin_roles),
            checkout_root=Path(settings.storage_dir) / "coding_checkouts",
            review_capture_root=Path(settings.storage_dir) / ".coding_review_captures" if settings.coding_review_capture_enabled else None,
            command_runner=DockerCommandRunner(settings.coding_command_image) if settings.coding_command_image else None,
        )
    return _service
