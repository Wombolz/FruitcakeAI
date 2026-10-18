from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.coding_runtime.checkouts import CheckoutManager
from app.coding_runtime.repository import RepositoryError
from app.coding_runtime.service import CodingServiceError
from app.db.models import CodingCheckout
from test_repository import git, repository
from test_service import harness, finished, wait_for_status
from test_api import api_harness, headers


async def settled(service, session_id):
    async with asyncio.timeout(20):
        while True:
            row = await service.get_checkout(user_id=2, session_id=session_id)
            if row.status not in {"creating", "removing"}:
                return row
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_worktree_baseline_excludes_dirty_and_ignored_source_files(tmp_path):
    source = repository(tmp_path / "repo")
    (source / "file.txt").write_text("USER DIRTY CHANGE\n")
    (source / "untracked").write_text("private user draft")
    (source / ".env").write_text("SECRET")
    (source / ".git/info/exclude").write_text(".env\n")
    index = source / ".git/index"
    before = index.read_bytes(), index.stat().st_mtime_ns
    manager = CheckoutManager(tmp_path / "managed")
    target = manager.path(2, str(uuid4()))
    baseline = await manager.create(source, target, "fixture")
    assert baseline == git(source, "rev-parse", "HEAD").decode().strip()
    assert (target / "file.txt").read_text() == "original\n"
    assert not (target / "untracked").exists()
    assert not (target / ".env").exists()
    assert (source / "file.txt").read_text() == "USER DIRTY CHANGE\n"
    assert before == (index.read_bytes(), index.stat().st_mtime_ns)
    assert git(target, "status", "--porcelain") == b""
    assert b"locked fixture" in git(source, "worktree", "list", "--porcelain")
    await manager.remove(source, target, baseline, "fixture")
    assert not target.exists()
    assert source.exists()
    assert (source / "untracked").read_text() == "private user draft"


@pytest.mark.asyncio
async def test_worktree_checkout_does_not_run_hooks_or_filters(tmp_path):
    source = repository(tmp_path / "repo")
    (source / ".gitattributes").write_text("*.txt filter=custom\n")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-qm", "Attributes")
    marker = tmp_path / "executed"
    command = f"touch '{marker}'"
    for key in ("filter.custom.clean", "filter.custom.smudge", "filter.custom.process", "core.fsmonitor"):
        git(source, "config", key, command)
    git(source, "config", "filter.custom.required", "true")
    hook = source / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\n{command}\n")
    hook.chmod(0o700)
    manager = CheckoutManager(tmp_path / "managed")
    target = manager.path(2, str(uuid4()))
    baseline = await manager.create(source, target, "fixture")
    assert not marker.exists()
    assert (target / "file.txt").read_text() == "original\n"
    await manager.remove(source, target, baseline, "fixture")
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tracked", "untracked", "ignored"])
async def test_removal_never_forces_away_user_files(tmp_path, kind):
    source = repository(tmp_path / "repo")
    manager = CheckoutManager(tmp_path / "managed")
    target = manager.path(2, str(uuid4()))
    baseline = await manager.create(source, target, "fixture")
    if kind == "tracked":
        changed = target / "file.txt"
    elif kind == "ignored":
        (source / ".git/info/exclude").write_text("private.tmp\n")
        changed = target / "private.tmp"
    else:
        changed = target / "draft.tmp"
    changed.write_text("KEEP THIS")
    with pytest.raises(RepositoryError) as error:
        await manager.remove(source, target, baseline, "fixture")
    assert error.value.code == "checkout_dirty"
    assert changed.read_text() == "KEEP THIS"
    assert b"locked fixture" in git(source, "worktree", "list", "--porcelain")


@pytest.mark.asyncio
async def test_wrong_baseline_or_replaced_path_fails_closed(tmp_path):
    source = repository(tmp_path / "repo")
    manager = CheckoutManager(tmp_path / "managed")
    target = manager.path(2, str(uuid4()))
    baseline = await manager.create(source, target, "fixture")
    with pytest.raises(RepositoryError):
        await manager.validate(source, target, "0" * 40)
    with pytest.raises(RepositoryError):
        await manager.create(source, target, "fixture")
    target.rename(target.with_name(target.name + "-moved"))
    target.symlink_to(source, target_is_directory=True)
    with pytest.raises(RepositoryError):
        await manager.remove(source, target, baseline, "fixture")
    assert (source / "file.txt").read_text() == "original\n"


@pytest.mark.asyncio
async def test_checkout_api_is_idempotent_scoped_and_routes_runtime_and_changes(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        source = repository(tmp_path / "repo")
        path = f"/coding/sessions/{session.id}/checkout"
        assert (await client.get(path)).json() is None
        assert (await client.post(path, json={"path": str(source), "ref": "other"})).status_code == 422
        for user_id in (1, 3):
            assert (await client.post(path, headers=headers(user_id))).status_code == 404
        response = await client.post(path)
        assert response.status_code == 202
        assert response.json()["status"] == "creating"
        assert (await client.post(path)).json()["id"] == response.json()["id"]
        row = await settled(service, session.id)
        assert row.status == "ready", row.error_code
        assert (await client.get(path)).json()["baseline_commit"] == row.baseline_commit
        target = Path(row.canonical_path)
        (target / "file.txt").write_text("changed only in checkout\n")
        status = await client.get(f"/coding/sessions/{session.id}/repository")
        assert status.json()["files"][0]["path"] == "file.txt"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect isolated files")
        result, _ = await finished(service, run)
        assert result.status == "completed"
        assert factory.instances[0].starts[0].workspace == target
        assert factory.instances[0].capabilities().read_only
        assert (source / "file.txt").read_text() == "original\n"
        assert (await client.post(path + "/remove")).status_code == 409
        await service.close_session(user_id=2, session_id=session.id)
        assert (await client.post(path + "/remove", json={"force": True})).status_code == 422
        assert (await client.post(path + "/remove")).status_code == 202
        retained = await settled(service, session.id)
        assert retained.status == "ready" and retained.error_code == "checkout_dirty"
        assert target.exists()
        (target / "file.txt").write_text("original\n")
        assert (await client.post(path + "/remove")).status_code == 202
        assert (await settled(service, session.id)).status == "removed"
        assert (await client.post(path + "/remove")).json()["status"] == "removed"
        assert not target.exists()


@pytest.mark.asyncio
async def test_source_session_history_transitions_explicitly_without_reusing_source_runtime(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect source")
        first, _ = await finished(service, run)
        old_runtime = factory.instances[0]
        (tmp_path / "repo/file.txt").write_text("UNCOMMITTED SOURCE CONTENT\n")
        attached = await service.create_checkout(user_id=2, session_id=session.id)
        assert old_runtime.closed == 1
        assert session.id not in service._slots
        row = await settled(service, session.id)
        assert row.status == "ready" and attached.id == row.id
        assert (Path(row.canonical_path) / "file.txt").read_text() == "original\n"
        assert (tmp_path / "repo/file.txt").read_text() == "UNCOMMITTED SOURCE CONTENT\n"
        current = await service.get_session(user_id=2, session_id=session.id)
        assert not current.edit_mode and not current.command_mode
        following = await service.submit(user_id=2, session_id=session.id, message="Continue in the isolated checkout")
        second, _ = await finished(service, following)
        assert second.context_generation == first.context_generation + 1
        assert factory.instances[1].starts[0].workspace == Path(row.canonical_path)
        assert ("user", "Inspect source") in factory.instances[1].prompts[0].history
        history = await service.list_runs(user_id=2, session_id=session.id)
        assert {item.id for item in history} == {run.id, following.id}
        assert (await service.get_run(user_id=2, run_id=run.id)).response_text == first.response_text


@pytest.mark.asyncio
async def test_attached_checkout_never_falls_back_to_source(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        other = await service.create_session(user_id=2, workspace_id=grant.id, model_profile_id=1)
        await service.create_checkout(user_id=2, session_id=other.id)
        row = await settled(service, other.id)
        Path(row.canonical_path).rename(Path(row.canonical_path + "-moved"))
        failed = await service.submit(user_id=2, session_id=other.id, message="Never use source as fallback")
        result, _ = await finished(service, failed)
        assert result.status == "failed" and result.error_code == "checkout_invalid"
        assert not factory.instances[-1].starts


@pytest.mark.asyncio
async def test_checkout_api_allows_explicit_transition_after_source_history(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        repository(tmp_path / "repo")
        old = await service.submit(user_id=2, session_id=session.id, message="Inspect source first")
        await finished(service, old)
        response = await client.post(f"/coding/sessions/{session.id}/checkout")
        assert response.status_code == 202
        ready = await settled(service, session.id)
        assert ready.status == "ready"
        assert (await client.get(f"/coding/sessions/{session.id}/runs")).json()[0]["id"] == old.id
        updated = (await client.post(f"/coding/sessions/{session.id}/edit-mode", json={"enabled": True})).json()
        assert updated["id"] == session.id and updated["edit_mode"]


@pytest.mark.asyncio
async def test_failed_source_runtime_teardown_does_not_attach_checkout(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        old = await service.submit(user_id=2, session_id=session.id, message="Inspect source")
        await finished(service, old)
        runtime = factory.instances[0]
        original = runtime.close
        async def fail():
            raise OSError("fixture teardown failure")
        runtime.close = fail
        try:
            with pytest.raises(CodingServiceError) as failure:
                await service.create_checkout(user_id=2, session_id=session.id)
            assert failure.value.code == "cleanup_failed"
            assert await service.get_checkout(user_id=2, session_id=session.id) is None
        finally:
            runtime.close = original


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "closed", "interrupted"])
async def test_checkout_transition_requires_idle_or_interrupted_source_session(tmp_path, state):
    from app.db.models import CodingSession
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        if state == "active":
            factory.mode = "blocked"
            run = await service.submit(user_id=2, session_id=session.id, message="Keep source run active")
            await wait_for_status(service, run.id, "running")
        elif state == "closed":
            await service.close_session(user_id=2, session_id=session.id)
        else:
            async with sessions() as db, db.begin():
                (await db.get(CodingSession, session.id)).status = "interrupted"
        if state == "interrupted":
            await service.create_checkout(user_id=2, session_id=session.id)
            assert (await settled(service, session.id)).status == "ready"
        else:
            with pytest.raises(CodingServiceError) as failure:
                await service.create_checkout(user_id=2, session_id=session.id)
            assert failure.value.code == "checkout_conflict"
            assert await service.get_checkout(user_id=2, session_id=session.id) is None
        if state == "active":
            await service.stop(user_id=2, run_id=run.id)


@pytest.mark.asyncio
async def test_restart_recovers_checkout_state_without_deleting_data(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        await service.create_checkout(user_id=2, session_id=session.id)
        row = await settled(service, session.id)
        await service.shutdown()
        async with sessions() as db, db.begin():
            (await db.get(CodingCheckout, row.id)).status = "creating"
        await service.startup()
        recovered = await service.get_checkout(user_id=2, session_id=session.id)
        assert recovered.status == "failed" and recovered.error_code == "backend_restart"
        assert Path(recovered.canonical_path).is_dir()
        with pytest.raises(CodingServiceError):
            await service.submit(user_id=2, session_id=session.id, message="Do not fall back")
        await service.close_session(user_id=2, session_id=session.id)
        await service.remove_checkout(user_id=2, session_id=session.id)
        assert (await settled(service, session.id)).status == "removed"


@pytest.mark.asyncio
async def test_non_git_failure_retains_identity_and_can_remove_absent_checkout(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        first = await service.create_checkout(user_id=2, session_id=session.id)
        row = await settled(service, session.id)
        assert row.status == "failed" and row.error_code == "not_repository"
        assert (await service.create_checkout(user_id=2, session_id=session.id)).id == first.id
        assert not factory.instances
        # The grant becomes a repository before cleanup; absent, unregistered
        # paths can be marked removed without any filesystem deletion.
        repository(tmp_path / "repo")
        await service.close_session(user_id=2, session_id=session.id)
        await service.remove_checkout(user_id=2, session_id=session.id)
        assert (await settled(service, session.id)).status == "removed"


@pytest.mark.asyncio
async def test_checkout_creation_does_not_block_another_sessions_stop(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        factory.mode = "blocked"
        run = await service.submit(user_id=2, session_id=session.id, message="Inspect")
        await wait_for_status(service, run.id, "running")
        other = await service.create_session(user_id=2, workspace_id=grant.id, model_profile_id=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._checkouts.create

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        service._checkouts.create = delayed
        await service.create_checkout(user_id=2, session_id=other.id)
        await asyncio.wait_for(entered.wait(), 4)
        assert await asyncio.wait_for(service.stop(user_id=2, run_id=run.id), 2)
        release.set()
        assert (await settled(service, other.id)).status == "ready"


@pytest.mark.asyncio
async def test_revoking_grant_cancels_checkout_creation_without_fallback(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        source = repository(tmp_path / "repo")
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        service._checkouts.create = blocked
        await service.create_checkout(user_id=2, session_id=session.id)
        await asyncio.wait_for(entered.wait(), 4)
        await service.revoke_workspace(operator_id=1, workspace_id=grant.id)
        row = await settled(service, session.id)
        assert row.status == "failed" and row.error_code == "checkout_interrupted"
        assert (source / "file.txt").read_text() == "original\n"
        assert not factory.instances
        with pytest.raises(CodingServiceError):
            await service.submit(user_id=2, session_id=session.id, message="Do not fall back")


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("FRUITCAKE_TEST_PI"), reason="Set FRUITCAKE_TEST_PI for installed-Pi checks")
async def test_installed_pi_reads_only_its_checkout_and_still_cannot_write(tmp_path):
    from app.coding_runtime.pi_rpc import PiRpcClient
    from test_pi_live import live_config, model_server, run_with_events

    source = repository(tmp_path / "repo")
    (source / "private.txt").write_text("OUTSIDE_CHECKOUT_SECRET")
    manager = CheckoutManager(tmp_path / "managed")
    target = manager.path(2, str(uuid4()))
    await manager.create(source, target, "fixture")
    with model_server([
        {"tool": "read", "arguments": {"path": "file.txt"}},
        {"tool": "write", "arguments": {"path": "new.txt", "content": "unauthorized"}},
        {"tool": "read", "arguments": {"path": str(source / "private.txt")}},
        {"text": "Done"},
    ]) as (endpoint, requests):
        async with PiRpcClient(live_config(target, endpoint)) as client:
            _, events = await run_with_events(client)
        assert any(event.kind == "operation.completed" and event.payload.get("is_error") for event in events)
        assert "OUTSIDE_CHECKOUT_SECRET" not in str(requests)
        assert not (target / "new.txt").exists()
        assert (target / "file.txt").read_text() == "original\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_immediate_cancellation_finalizes_unstarted_reservations(tmp_path, shutdown):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        repository(tmp_path / "repo")
        row = await service.create_checkout(user_id=2, session_id=session.id)
        if shutdown:
            await service.shutdown()
        else:
            await service.revoke_workspace(operator_id=1, workspace_id=grant.id)
        current = await service.get_checkout(user_id=2, session_id=session.id)
        assert current.status == "failed"
        assert current.error_code == ("backend_shutdown" if shutdown else "checkout_interrupted")
        assert not Path(row.canonical_path).exists()


@pytest.mark.asyncio
async def test_retained_checkout_limit_blocks_unbounded_disk_reservations(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        for _ in range(8):
            reserved = await service.create_session(user_id=2, workspace_id=grant.id, model_profile_id=1)
            async with sessions() as db, db.begin():
                item = CodingCheckout(id=str(uuid4()), session_id=reserved.id, user_id=2,
                                      canonical_path=str(tmp_path / str(uuid4())), status="failed")
                db.add(item)
        with pytest.raises(CodingServiceError) as error:
            await service.create_checkout(user_id=2, session_id=session.id)
        assert error.value.code == "capacity_reached"
