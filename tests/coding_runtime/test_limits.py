from types import SimpleNamespace

import asyncio
import pytest

from app.config import Settings
from app.coding_runtime.service import CodingServiceLimits
from test_service import harness, finished, wait_for_status


def test_run_deadline_defaults_to_thirty_minutes_without_extending_other_limits(monkeypatch):
    monkeypatch.delenv('CODING_RUN_TIMEOUT_SECONDS', raising=False)
    settings = Settings(_env_file=None)
    limits = CodingServiceLimits()
    assert settings.coding_run_timeout_seconds == limits.run_seconds == 1800
    assert limits.startup_seconds == 30
    assert limits.stop_seconds == 10
    assert limits.idle_seconds == 300


@pytest.mark.parametrize('seconds', [600, 3600, 7200])
def test_bootstrap_honors_configured_run_deadline(monkeypatch, seconds):
    from app.coding_runtime import bootstrap
    import app.config

    monkeypatch.setenv('CODING_RUN_TIMEOUT_SECONDS', str(seconds))
    settings = Settings(_env_file=None, coding_runtime_enabled=True)
    assert settings.coding_run_timeout_seconds == seconds
    monkeypatch.setattr(app.config, 'settings', settings)
    monkeypatch.setattr(bootstrap, '_service', None)
    monkeypatch.setattr(bootstrap, 'CodingSessionService', lambda *args, **kwargs: SimpleNamespace(**kwargs))
    service = bootstrap.get_coding_session_service()
    assert service.limits.run_seconds == seconds
    assert service.limits.stop_seconds == 10
    assert service.limits.startup_seconds == 30
    assert bootstrap.get_coding_session_service() is service


@pytest.mark.asyncio
async def test_long_run_budget_does_not_allow_idle_eviction_or_delay_stop(tmp_path):
    async with harness(tmp_path, run_seconds=3600, idle_seconds=0.02, stop_seconds=0.05) as (service, sessions, factory, grant, session):
        factory.mode = 'ignores_abort'
        run = await service.submit(user_id=2, session_id=session.id, message='Wait until stopped')
        await wait_for_status(service, run.id, 'running')
        await asyncio.sleep(0.06)
        assert await service.expire_idle() == 0
        assert (await service.get_run(user_id=2, run_id=run.id)).status == 'running'
        async with asyncio.timeout(1):
            await service.stop(user_id=2, run_id=run.id)
        result, _ = await finished(service, run)
        assert result.status == 'cancelled'
        assert factory.instances[0].closed
