"""Capture-only contracts: no model calls, immutable evidence, shared admission."""
import asyncio
from datetime import timedelta
from importlib import import_module
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.coding_runtime.review_admission import claim, release
from app.coding_runtime.review_capture import CaptureBuilder, CaptureLimits
from app.coding_runtime.review_service import now
from app.coding_runtime.repository import RepositoryError
from app.coding_runtime.service import CodingServiceError, CodingSessionService
from app.db.models import CodingCheckoutReservation, CodingReviewCapture, CodingWorkspace
from app.db.session import Base
from test_service import harness, finished, wait_for_status
from test_api import api_harness, headers
from test_changes import enable
from test_repository import git, repository


async def captured(service, session, **overrides):
    result = await service.create_review_capture(user_id=2, session_id=session.id,
        **{"scope": "checkout_changes", "paths": ["file.txt"], "client_request_id": str(uuid4()), **overrides})
    task = service._capture_jobs.get(result["id"])
    if task:
        await asyncio.wait_for(asyncio.shield(task), 5)
    return await service.get_review_capture(user_id=2, capture_id=result["id"])


@pytest.mark.asyncio
async def test_scopes_preserve_committed_staged_unstaged_untracked_and_deleted(tmp_path):
    root = repository(tmp_path / "repo")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "file.txt").write_text("committed\n")
    git(root, "commit", "-am", "Change")
    builder = CaptureBuilder()
    assert (await builder.options(root, baseline, "uncommitted"))["paths"] == []
    assert (await builder.options(root, baseline, "checkout_changes"))["paths"][0]["path"] == "file.txt"
    (root / "file.txt").write_text("staged\n")
    git(root, "add", "file.txt")
    (root / "file.txt").write_bytes(b"complete\r\n")
    (root / "new.txt").write_text("new without newline")
    manifest, blobs = await builder.capture(root, baseline, "checkout_changes", ["file.txt", "new.txt"], {})
    file = manifest["files"][0]
    assert blobs[file["before"]["sha256"]] == b"original\n"
    assert blobs[file["after"]["sha256"]] == b"complete\r\n"
    manifest, blobs = await builder.capture(root, baseline, "uncommitted", ["file.txt"], {})
    assert blobs[manifest["files"][0]["before"]["sha256"]] == b"committed\n"
    (root / "file.txt").unlink()
    manifest, blobs = await builder.capture(root, baseline, "checkout_changes", ["file.txt"], {})
    item = manifest["files"][0]
    assert item["after"] is None
    assert b"+++ /dev/null" in blobs[item["patch"]["sha256"]]


@pytest.mark.asyncio
async def test_empty_files_no_newline_and_executable_mode_are_retained_exactly(tmp_path):
    root = repository(tmp_path / "repo")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "empty").write_text("")
    (root / "empty").chmod(0o755)
    (root / "file.txt").chmod(0o755)
    manifest, blobs = await CaptureBuilder().capture(root, baseline, "checkout_changes", ["empty", "file.txt"], {})
    empty, changed_mode = manifest["files"]
    assert blobs[empty["after"]["sha256"]] == b""
    assert b"new file mode 100755" in blobs[empty["patch"]["sha256"]]
    assert b"old mode 100644\nnew mode 100755" in blobs[changed_mode["patch"]["sha256"]]
    (root / "file.txt").write_text("without newline")
    git(root, "commit", "-am", "No newline")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "file.txt").unlink()
    manifest, blobs = await CaptureBuilder().capture(root, baseline, "checkout_changes", ["file.txt"], {})
    assert b"-without newline\n\\ No newline at end of file\n" in blobs[manifest["files"][0]["patch"]["sha256"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "binary", "oversized", "secret", "hidden", "ignored", "historic_binary"])
async def test_selected_unsafe_content_never_publishes(tmp_path, kind):
    root = repository(tmp_path / "repo")
    target = root / "file.txt"
    path = "file.txt"
    if kind == "historic_binary":
        target.write_bytes(b"\0private")
        git(root, "commit", "-am", "Binary baseline")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    target.unlink()
    if kind == "symlink": target.symlink_to(tmp_path / "outside")
    elif kind == "hardlink":
        outside = tmp_path / "outside"
        outside.write_text("PRIVATE")
        os.link(outside, target)
    elif kind == "fifo": os.mkfifo(target)
    elif kind == "binary": target.write_bytes(b"\xff\0")
    elif kind == "oversized": target.write_bytes(b"x" * (256 * 1024 + 1))
    else: target.write_text("updated\n")
    if kind in {"secret", "hidden"}:
        path = "secret.txt" if kind == "secret" else ".env"
        (root / path).write_text("PRIVATE")
    if kind == "ignored": (root / ".gitignore").write_text("file.txt\n")
    with pytest.raises(RepositoryError):
        await CaptureBuilder().capture(root, baseline, "checkout_changes", [path], {})


@pytest.mark.asyncio
async def test_capture_bounds_and_explicit_context_omissions(tmp_path):
    root = repository(tmp_path / "repo")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "file.txt").write_text("new\n")
    (root / "context.py").write_text("support\n")
    git(root, "add", "context.py")
    (root / ".env").write_text("PRIVATE")
    manifest, blobs = await CaptureBuilder(CaptureLimits(context_files=0)).capture(root, baseline, "checkout_changes", ["file.txt"], {})
    assert manifest["limited"]
    assert manifest["omission_counts"] == {"denied_path": 1, "context_limit": 1}
    assert not any(b"PRIVATE" in blob for blob in blobs.values())
    for limits in (CaptureLimits(content_bytes=1), CaptureLimits(patch_bytes=1), CaptureLimits(manifest_bytes=1)):
        with pytest.raises(RepositoryError, match="budget|limit"):
            await CaptureBuilder(limits).capture(root, baseline, "checkout_changes", ["file.txt"], {})


@pytest.mark.asyncio
async def test_double_observation_rejects_changes_during_capture(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "file.txt").write_text("one\n")
    builder = CaptureBuilder()
    original = builder.scan
    async def scan(*args):
        result = await original(*args)
        (root / "file.txt").write_text("two\n")
        return result
    monkeypatch.setattr(builder, "scan", scan)
    with pytest.raises(RepositoryError, match="changed while"):
        await builder.capture(root, baseline, "checkout_changes", ["file.txt"], {})


@pytest.mark.asyncio
async def test_unselected_untracked_files_are_not_read_as_supporting_context(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "file.txt").write_text("updated\n")
    (root / "personal.py").write_text("UNSELECTED_PRIVATE_TEXT")
    builder = CaptureBuilder()
    original = builder.read_live
    def read(root, path):
        assert path != "personal.py", "Unselected untracked file must not be opened"
        return original(root, path)
    monkeypatch.setattr(builder, "read_live", read)
    manifest, blobs = await builder.capture(root, baseline, "checkout_changes", ["file.txt"], {})
    assert manifest["omission_counts"] == {"unselected_untracked": 1}
    assert not any(b"UNSELECTED_PRIVATE_TEXT" in data for data in blobs.values())


@pytest.mark.asyncio
async def test_historical_ignore_rules_and_malicious_git_helpers_are_not_bypassed(tmp_path):
    root = repository(tmp_path / "repo")
    (root / ".gitignore").write_text("private.txt\nsub/*.txt\n")
    (root / "private.txt").write_text("HISTORICAL_PRIVATE")
    (root / "sub").mkdir()
    (root / "sub/.gitignore").write_text("!allowed.txt\n")
    (root / "sub/allowed.txt").write_text("ordinary")
    git(root, "add", "-f", ".gitignore", "private.txt", "sub/.gitignore", "sub/allowed.txt")
    git(root, "commit", "-qm", "Historical policy")
    baseline = git(root, "rev-parse", "HEAD").decode().strip()
    (root / ".gitignore").unlink()
    (root / "private.txt").write_text("updated")
    (root / "file.txt").write_text("new")
    marker = tmp_path / "must-not-execute"
    git(root, "config", "diff.external", f"touch {marker}")
    git(root, "config", "filter.unsafe.smudge", f"touch {marker}")
    (root / ".gitattributes").write_text("*.txt filter=unsafe\n")
    builder = CaptureBuilder()
    options = await builder.options(root, baseline, "checkout_changes")
    assert not next(item for item in options["paths"] if item["path"] == "private.txt")["eligible"]
    with pytest.raises(RepositoryError, match="privacy policy"):
        await builder.capture(root, baseline, "checkout_changes", ["private.txt"], {})
    manifest, blobs = await builder.capture(root, baseline, "checkout_changes", ["file.txt"], {})
    assert not any(b"HISTORICAL_PRIVATE" in blob for blob in blobs.values())
    assert "sub/allowed.txt" in [item["path"] for item in manifest["context"]]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_retained_evidence_is_private_owner_scoped_stale_and_never_live_fallback(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, factory, _, session):
        _, checkout = await enable(service, session, tmp_path)
        target = Path(checkout.canonical_path) / "file.txt"
        target.write_text("captured\n")
        capture = await captured(service, session)
        assert capture["status"] == "ready", capture
        assert capture["applicability"] == "current"
        assert not capture["ai_reviews"] and not factory.instances
        diff = await service.review_capture_diff(user_id=2, capture_id=capture["id"], path="file.txt")
        assert "+captured" in diff["patch"]
        target.write_text("live later\n")
        stale = await service.get_review_capture(user_id=2, capture_id=capture["id"])
        assert stale["applicability"] == "stale"
        assert await service.review_capture_diff(user_id=2, capture_id=capture["id"], path="file.txt") == diff
        with pytest.raises(CodingServiceError, match="not found"):
            await service.get_review_capture(user_id=3, capture_id=capture["id"])
        directory = service._capture_storage.directory(2, capture["id"])
        assert directory.stat().st_mode & 0o077 == 0
        assert all(file.stat().st_mode & 0o077 == 0 for file in directory.iterdir())
        after = capture["files"][0]["after"]
        (directory / after["sha256"]).write_text("tampered")
        assert (await service.get_review_capture(user_id=2, capture_id=capture["id"]))["content_status"] == "unavailable"
        with pytest.raises(CodingServiceError) as exc:
            await service.review_capture_diff(user_id=2, capture_id=capture["id"], path="file.txt")
        assert exc.value.code == "capture_corrupt"
        assert "live later" not in str(exc.value)


@pytest.mark.asyncio
async def test_capture_reservation_blocks_builder_delivery_removal_and_replays_once(tmp_path, monkeypatch):
    async with harness(tmp_path, review=True) as (service, sessions, factory, _, session):
        _, checkout = await enable(service, session, tmp_path)
        (Path(checkout.canonical_path) / "file.txt").write_text("changed\n")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = service._capture_builder.capture
        async def hold(*args):
            entered.set()
            await proceed.wait()
            return await original(*args)
        monkeypatch.setattr(service._capture_builder, "capture", hold)
        request = dict(user_id=2, session_id=session.id, scope="checkout_changes", paths=["file.txt"], client_request_id="same")
        first = await service.create_review_capture(**request)
        await asyncio.wait_for(entered.wait(), 3)
        assert (await service.create_review_capture(**request))["id"] == first["id"]
        with pytest.raises(CodingServiceError) as exc:
            await service.create_review_capture(**{**request, "paths": ["another"]})
        assert exc.value.code == "request_conflict"
        for operation in (
            lambda: service.submit(user_id=2, session_id=session.id, message="Builder must wait"),
            lambda: service.delivery(user_id=2, session_id=session.id, action="commit", files=["file.txt"], message="No"),
            lambda: service.create_review_capture(**{**request, "client_request_id": "another"}),
        ):
            with pytest.raises(CodingServiceError) as exc:
                await operation()
            assert exc.value.code == "checkout_busy"
        assert not factory.instances
        await service.close_session(user_id=2, session_id=session.id)
        with pytest.raises(CodingServiceError) as exc:
            await service.remove_checkout(user_id=2, session_id=session.id)
        assert exc.value.code == "checkout_busy"
        proceed.set()
        await asyncio.wait_for(service._capture_jobs[first["id"]], 5)
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_delivery_admission_blocks_capture_until_git_work_finishes(tmp_path, monkeypatch):
    async with harness(tmp_path, review=True) as (service, sessions, _, _, session):
        _, checkout = await enable(service, session, tmp_path)
        (Path(checkout.canonical_path) / "file.txt").write_text("new\n")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = service._checkouts.validate_ready
        async def hold(*args, **kwargs):
            entered.set()
            await proceed.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(service._checkouts, "validate_ready", hold)
        delivery = asyncio.create_task(service.delivery(user_id=2, session_id=session.id, action="commit", files=["file.txt"], message="Preview"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(CodingServiceError) as exc:
                await captured(service, session)
            assert exc.value.code == "checkout_busy"
        finally:
            proceed.set()
            await asyncio.wait_for(delivery, 5)
        assert (await captured(service, session))["status"] == "ready"


@pytest.mark.asyncio
async def test_late_delivery_completion_does_not_reset_a_new_owners_reservation(tmp_path, monkeypatch):
    async with harness(tmp_path, review=True) as (service, sessions, _, _, session):
        _, checkout = await enable(service, session, tmp_path)
        (Path(checkout.canonical_path) / "file.txt").write_text("new\n")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = service._checkouts.validate_ready
        async def hold(*args, **kwargs):
            entered.set()
            await proceed.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(service._checkouts, "validate_ready", hold)
        delivery = asyncio.create_task(service.delivery(user_id=2, session_id=session.id, action="commit", files=["file.txt"], message="Preview"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            async with sessions() as db, db.begin():
                lease = await db.get(CodingCheckoutReservation, checkout.id)
                lease.service_epoch, lease.token = "new-owner", str(uuid4())
        finally:
            proceed.set()
            await asyncio.wait_for(delivery, 5)
        async with sessions() as db:
            lease = await db.get(CodingCheckoutReservation, checkout.id)
            assert lease.service_epoch == "new-owner"
            from app.db.models import CodingCheckout
            assert (await db.get(CodingCheckout, checkout.id)).status == "committing"


@pytest.mark.asyncio
async def test_capture_quotas_and_grant_revocation_fail_closed(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, factory, workspace, session):
        _, checkout = await enable(service, session, tmp_path)
        (Path(checkout.canonical_path) / "file.txt").write_text("new\n")
        ready = await captured(service, session)
        async with sessions() as db, db.begin():
            for i in range(9):
                db.add(CodingReviewCapture(id=str(uuid4()), user_id=2, session_id=session.id, checkout_id=checkout.id,
                    workspace_id=workspace.id, client_request_id=str(i), request_hash="a"*64,
                    scope="checkout_changes", status="failed", expires_at=now()+timedelta(days=30)))
        with pytest.raises(CodingServiceError) as exc:
            await captured(service, session)
        assert exc.value.code == "capture_limit"
        async with sessions() as db, db.begin():
            (await db.get(CodingWorkspace, workspace.id)).is_active = False
        for operation in (
            lambda: service.get_review_capture(user_id=2, capture_id=ready["id"]),
            lambda: service.review_capture_diff(user_id=2, capture_id=ready["id"], path="file.txt"),
            lambda: service.review_options(user_id=2, session_id=session.id),
        ):
            with pytest.raises(CodingServiceError) as exc:
                await operation()
            assert exc.value.code == "workspace_denied"
        assert not factory.instances


@pytest.mark.asyncio
async def test_builder_holds_checkout_against_capture_and_releases_after_stop(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, factory, _, session):
        _, checkout = await enable(service, session, tmp_path)
        factory.mode = "blocked"
        await service.set_edit_mode(user_id=2, session_id=session.id, enabled=False)
        run = await service.submit(user_id=2, session_id=session.id, message="Hold checkout")
        await wait_for_status(service, run.id, "running")
        with pytest.raises(CodingServiceError) as exc:
            await captured(service, session)
        assert exc.value.code == "checkout_busy"
        await service.stop(user_id=2, run_id=run.id)
        await finished(service, run)
        async with sessions() as db:
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_fenced_token_and_second_supervisor_cannot_reclaim_work(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, factory, _, session):
        _, checkout = await enable(service, session, tmp_path)
        async with sessions() as db, db.begin():
            token = await claim(db, checkout.id, "capture", "holder", service._capture_epoch)
        replacement = CodingSessionService(sessions, factory, runtime_kind="fixture")
        with pytest.raises(CodingServiceError) as exc:
            await replacement.startup()
        assert exc.value.code == "coding_owner_busy"
        async with sessions() as db, db.begin():
            await release(db, "capture", "holder", "old-epoch", token=token)
            await release(db, "capture", "holder", service._capture_epoch, token="wrong-token")
            assert await db.get(CodingCheckoutReservation, checkout.id) is not None
            with pytest.raises(CodingServiceError):
                await claim(db, checkout.id, "builder", "other", service._capture_epoch)
            await release(db, "capture", "holder", service._capture_epoch, token=token)
        await service.shutdown()
        async with sessions() as db, db.begin():
            db.add(CodingReviewCapture(id="stale", user_id=2, session_id=session.id, checkout_id=checkout.id,
                workspace_id=session.workspace_id, client_request_id="stale", request_hash="a"*64,
                scope="checkout_changes", status="capturing", expires_at=now()+timedelta(days=30)))
            await claim(db, checkout.id, "capture", "stale", "dead-epoch")
        replacement = CodingSessionService(sessions, factory, runtime_kind="fixture", review_capture_root=tmp_path / "captures")
        try:
            await replacement.startup()
            async with sessions() as db:
                assert (await db.get(CodingReviewCapture, "stale")).status == "interrupted"
                assert await db.get(CodingCheckoutReservation, checkout.id) is None
        finally:
            await replacement.shutdown()


@pytest.mark.asyncio
async def test_ownership_lost_during_startup_prevents_recovery(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, factory, _, session):
        _, checkout = await enable(service, session, tmp_path)
        await service.shutdown()
        async with sessions() as db, db.begin():
            await claim(db, checkout.id, "capture", "old-capture", "old-epoch")
        replacement = CodingSessionService(sessions, factory, runtime_kind="fixture", review_capture_root=tmp_path / "captures")
        async def disconnect_during_cleanup():
            await replacement._capture_fence.close()
        replacement._commands = SimpleNamespace(recover=disconnect_during_cleanup)
        with pytest.raises(CodingServiceError) as exc:
            await replacement.startup()
        assert exc.value.code == "coding_owner_lost"
        async with sessions() as db:
            assert (await db.get(CodingCheckoutReservation, checkout.id)).holder_id == "old-capture"


@pytest.mark.asyncio
async def test_shutdown_before_capture_first_instruction_and_expiry(tmp_path):
    async with harness(tmp_path, review=True) as (service, sessions, _, _, session):
        _, checkout = await enable(service, session, tmp_path)
        target = Path(checkout.canonical_path) / "file.txt"
        target.write_text("new\n")
        ready = await captured(service, session)
        async with sessions() as db, db.begin():
            (await db.get(CodingReviewCapture, ready["id"])).expires_at = now() - timedelta(seconds=1)
        assert (await service.get_review_capture(user_id=2, capture_id=ready["id"]))["content_status"] == "expired"
        await service.expire_captures()
        assert not service._capture_storage.directory(2, ready["id"]).exists()
        pending = await service.create_review_capture(user_id=2, session_id=session.id, scope="checkout_changes", paths=["file.txt"], client_request_id="cancel")
        service._capture_jobs[pending["id"]].cancel()
        await service.shutdown()
        async with sessions() as db:
            assert (await db.get(CodingReviewCapture, pending["id"])).status == "interrupted"
            assert await db.get(CodingCheckoutReservation, checkout.id) is None


@pytest.mark.asyncio
async def test_disabled_api_is_honest_and_does_not_need_capture_tables(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, _, session, _):
        async with sessions.kw["bind"].begin() as connection:
            await connection.run_sync(lambda conn: CodingReviewCapture.__table__.drop(conn))
            await connection.run_sync(lambda conn: CodingCheckoutReservation.__table__.drop(conn))
        assert not (await client.get("/coding/status")).json()["review_captures"]
        result = await client.get(f"/coding/sessions/{session.id}/review-options")
        assert result.status_code == 200 and result.json() == {"captures_enabled": False, "ai_reviews": False, "reasoning_efforts": []}
        result = await client.post(f"/coding/sessions/{session.id}/review-captures", json={"paths": ["file.txt"], "client_request_id": "off"})
        assert result.status_code == 409
        assert not factory.instances


@pytest.mark.asyncio
async def test_capture_api_validates_inputs_and_owner_without_ai_calls(tmp_path):
    async with api_harness(tmp_path, review=True) as (client, app, service, sessions, factory, _, session, _):
        _, checkout = await enable(service, session, tmp_path)
        (Path(checkout.canonical_path) / "file.txt").write_text("api\n")
        url = f"/coding/sessions/{session.id}/review-captures"
        body = {"paths": ["file.txt"], "client_request_id": "api"}
        for invalid in ({**body, "scope": "whole_repo"}, {**body, "paths": []}, {**body, "paths": [42]}, {**body, "run_reviewer": True}):
            assert (await client.post(url, json=invalid)).status_code == 422
        assert (await client.post(url, json=body, headers=headers(3))).status_code == 404
        response = await client.post(url, json=body)
        assert response.status_code == 202, response.text
        capture_id = response.json()["id"]
        await asyncio.wait_for(service._capture_jobs[capture_id], 5)
        detail = await client.get(f"/coding/review-captures/{capture_id}")
        assert detail.status_code == 200 and detail.json()["content_status"] == "available"
        assert str(tmp_path) not in detail.text
        assert (await client.get(f"/coding/review-captures/{capture_id}", headers=headers(3))).status_code == 404
        diff = await client.get(f"/coding/review-captures/{capture_id}/diff", params={"path": "file.txt"})
        assert diff.status_code == 200 and "+api" in diff.json()["patch"]
        assert not factory.instances


def test_capture_migration_round_trip_and_retention_guard(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.sqlite'}")
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in {"coding_checkout_reservations", "coding_review_captures"}])
    migration = import_module("app.db.migrations.versions.058_coding_review_captures")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            inspector = inspect(connection)
            for name in ("coding_checkout_reservations", "coding_review_captures"):
                expected = Base.metadata.tables[name]
                assert set(expected.columns.keys()) == {item["name"] for item in inspector.get_columns(name)}
                assert {index.name for index in expected.indexes} == {item["name"] for item in inspector.get_indexes(name)}
            migration.downgrade()
            migration.upgrade()
            connection.execute(CodingCheckoutReservation.__table__.insert().values(checkout_id="x", holder_kind="capture", holder_id="x", token="x", service_epoch="x"))
            with pytest.raises(RuntimeError, match="operator inspection"):
                migration.downgrade()
    engine.dispose()
