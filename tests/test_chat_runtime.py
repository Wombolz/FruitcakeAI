import asyncio

import pytest

from app.chat_runtime import ChatRunManager


@pytest.mark.asyncio
async def test_chat_run_manager_rejects_exact_duplicate_within_window():
    manager = ChatRunManager(duplicate_window_seconds=10.0)

    claimed, active, fingerprint = await manager.claim_prompt(1, "hello   world")
    assert claimed is True
    assert active is False
    assert fingerprint

    claimed_again, active_again, fingerprint_again = await manager.claim_prompt(1, "hello world")
    assert claimed_again is False
    assert active_again is True
    assert fingerprint_again == fingerprint

    await manager.mark_prompt_finished(1, "hello world")

    claimed_recent, active_recent, fingerprint_recent = await manager.claim_prompt(1, "hello world")
    assert claimed_recent is False
    assert active_recent is False
    assert fingerprint_recent == fingerprint


@pytest.mark.asyncio
async def test_chat_run_manager_allows_new_prompt_after_window_expires():
    manager = ChatRunManager(duplicate_window_seconds=0.01)
    await manager.claim_prompt(1, "hello world")
    await manager.mark_prompt_finished(1, "hello world")
    await asyncio.sleep(0.02)

    claimed, active, _ = await manager.claim_prompt(1, "hello world")
    assert claimed is True
    assert active is False


@pytest.mark.asyncio
async def test_chat_run_manager_rejects_duplicate_client_send_id_within_window():
    manager = ChatRunManager(duplicate_window_seconds=10.0)

    claimed, active = await manager.claim_client_send_id(1, "send-123")
    assert claimed is True
    assert active is False

    claimed_again, active_again = await manager.claim_client_send_id(1, "send-123")
    assert claimed_again is False
    assert active_again is True

    await manager.mark_client_send_id_finished(1, "send-123")

    claimed_recent, active_recent = await manager.claim_client_send_id(1, "send-123")
    assert claimed_recent is False
    assert active_recent is False


@pytest.mark.asyncio
async def test_chat_run_manager_only_stops_matching_active_run():
    manager = ChatRunManager()
    blocker = asyncio.Event()

    async def _wait_forever():
        await blocker.wait()

    task = asyncio.create_task(_wait_forever())
    await manager.register(7, task, run_id="chat_run_current")

    assert await manager.request_stop_run(7, "chat_run_stale") is False
    assert task.cancelled() is False
    assert await manager.request_stop_run(7, "chat_run_current") is True
    with pytest.raises(asyncio.CancelledError):
        await task
