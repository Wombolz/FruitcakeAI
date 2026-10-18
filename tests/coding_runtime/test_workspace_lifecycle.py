import asyncio
from pathlib import Path
from importlib import import_module

import pytest
from sqlalchemy import create_engine, inspect
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models import ModelProfile, CodingSession, CodingRun, User
from app.db.session import Base
from app.coding_runtime.service import CodingServiceError
from test_api import api_harness, headers
from test_service import harness, finished, wait_for_status
from test_changes import enable
from test_repository import git


@pytest.mark.asyncio
async def test_reuse_checkout_keeps_files_and_serializes_sessions(tmp_path):
    async with harness(tmp_path) as (service, _, factory, grant, session):
        _, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('unfinished work\n')
        other = await service.create_session(user_id=2, workspace_id=grant.id, model_profile_id=1, checkout_id=checkout.id)
        assert not other.edit_mode and not other.command_mode
        assert await service._execution_root(user_id=2, session_id=other.id) == root
        factory.mode = 'block'
        await service.set_edit_mode(user_id=2, session_id=session.id, enabled=False)
        run = await service.submit(user_id=2, session_id=session.id, message='Inspect')
        await wait_for_status(service, run.id, 'running')
        with pytest.raises(CodingServiceError, match='Another run'):
            await service.submit(user_id=2, session_id=other.id, message='Inspect')
        await service.stop(user_id=2, run_id=run.id)
        await finished(service, run)
        await service.delete_session(user_id=2, session_id=session.id)
        assert (root / 'file.txt').read_text() == 'unfinished work\n'
        assert (await service.get_checkout(user_id=2, session_id=other.id)).id == checkout.id
        assert (await service.list_checkouts(user_id=2))[0].id == checkout.id


@pytest.mark.asyncio
async def test_model_switch_retires_runtime_and_records_each_runs_model(tmp_path):
    async with harness(tmp_path) as (service, sessions, factory, _, session):
        async with sessions() as db, db.begin():
            db.add(ModelProfile(id=2, model_id='ollama_chat/reviewer', display_name='Reviewer', provider_family='ollama', is_local=True))
        first = await service.submit(user_id=2, session_id=session.id, message='Read')
        await finished(service, first)
        old = factory.instances[0]
        changed = await service.change_model(user_id=2, session_id=session.id, model_profile_id=2)
        assert old.closed and changed.model_profile_id == 2
        assert not changed.edit_mode and not changed.command_mode
        second = await service.submit(user_id=2, session_id=session.id, message='Review')
        result, _ = await finished(service, second)
        assert result.model_profile_id == 2 and result.model_id == 'ollama_chat/reviewer'
        assert (await service.get_run(user_id=2, run_id=first.id)).model_profile_id == 1


@pytest.mark.asyncio
async def test_shared_checkout_handoff_retires_stale_peer_memory(tmp_path):
    async with harness(tmp_path) as (service, _, factory, workspace, session):
        _, checkout = await enable(service, session, tmp_path)
        await service.set_edit_mode(user_id=2, session_id=session.id, enabled=False)
        other = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1, checkout_id=checkout.id)
        first = await service.submit(user_id=2, session_id=session.id, message='Read original state')
        await finished(service, first)
        first_runtime = factory.instances[-1]
        second = await service.submit(user_id=2, session_id=other.id, message='Inspect same files')
        await finished(service, second)
        assert first_runtime.closed
        second_runtime = factory.instances[-1]
        resumed = await service.submit(user_id=2, session_id=session.id, message='Read files again')
        result, _ = await finished(service, resumed)
        assert second_runtime.closed and result.context_generation > first.context_generation


@pytest.mark.asyncio
async def test_archive_restore_and_delete_do_not_delete_checkout(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, _, _, _, session, _):
        _, checkout = await enable(service, session, tmp_path)
        assert (await client.post(f'/coding/sessions/{session.id}/archive', json={'archived': True})).status_code == 200
        assert (await client.get('/coding/sessions')).json() == []
        assert (await client.get('/coding/sessions?archived=true')).json()[0]['id'] == session.id
        restored = await client.post(f'/coding/sessions/{session.id}/archive', json={'archived': False})
        assert restored.json()['status'] == 'idle'
        assert (await client.delete(f'/coding/sessions/{session.id}', headers=headers(3))).status_code == 404
        assert (await client.delete(f'/coding/sessions/{session.id}')).status_code == 204
        assert (await client.get(f'/coding/sessions/{session.id}')).status_code == 404
        assert Path(checkout.canonical_path).is_dir()
        assert (await client.get('/coding/checkouts')).json()[0]['id'] == checkout.id


@pytest.mark.asyncio
async def test_selected_commit_preview_stale_guard_and_source_fast_forward(tmp_path):
    async with harness(tmp_path) as (service, sessions, _, _, session):
        async with sessions() as db, db.begin():
            (await db.get(User, 2)).role = 'admin'
        source, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('approved\n')
        (root / 'other.txt').write_text('leave uncommitted\n')
        preview = await service.delivery(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Update device')
        assert 'approved' in preview['diff']
        assert git(root, 'diff', '--cached') == b''
        (root / 'file.txt').write_text('changed after review\n')
        with pytest.raises(CodingServiceError, match='fresh preview'):
            await service.delivery(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Update device', review_hash=preview['review_hash'])
        (root / 'file.txt').write_text('approved\n')
        commit = await service.delivery(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Update device', review_hash=preview['review_hash'])
        assert git(root, 'show', 'HEAD:file.txt') == b'approved\n'
        assert git(root, 'status', '--porcelain') == b'?? other.txt\n'
        assert (source / 'file.txt').read_text() == 'original\n'
        assert await service._execution_root(user_id=2, session_id=session.id) == root
        apply = await service.delivery(user_id=2, session_id=session.id, action='apply')
        assert 'approved' in apply['diff']
        await service.delivery(user_id=2, session_id=session.id, action='apply', review_hash=apply['review_hash'])
        assert (source / 'file.txt').read_text() == 'approved\n'
        assert git(source, 'rev-parse', 'HEAD').decode().strip() == commit['head']
        assert not (source / 'other.txt').exists()


@pytest.mark.asyncio
async def test_source_delivery_refuses_dirty_or_divergent_original(tmp_path):
    async with harness(tmp_path) as (service, sessions, _, _, session):
        async with sessions() as db, db.begin():
            (await db.get(User, 2)).role = 'admin'
        source, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('new\n')
        body = dict(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Change')
        plan = await service.delivery(**body)
        await service.delivery(**body, review_hash=plan['review_hash'])
        (source / 'personal.txt').write_text('KEEP')
        with pytest.raises(CodingServiceError, match='Nothing will be overwritten'):
            await service.delivery(user_id=2, session_id=session.id, action='apply')
        git(source, 'add', 'personal.txt')
        git(source, 'commit', '-qm', 'Independent source change')
        with pytest.raises(CodingServiceError, match='diverged'):
            await service.delivery(user_id=2, session_id=session.id, action='apply')
        assert (source / 'personal.txt').read_text() == 'KEEP'
        assert (source / 'file.txt').read_text() == 'original\n'


def test_lifecycle_migration_matches_metadata_and_backfills_existing_checkout(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "migration.sqlite"}')
    with engine.begin() as conn:
        for table in ('users', 'user_projects'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE model_profiles (id INTEGER PRIMARY KEY, model_id TEXT)')
        with Operations.context(MigrationContext.configure(conn)):
            for module in ('052_coding_sessions','053_coding_checkouts','054_coding_changes','055_coding_command_mode'):
                import_module('app.db.migrations.versions.' + module).upgrade()
            conn.exec_driver_sql("INSERT INTO coding_workspaces (id,user_id,canonical_path,approved_by_user_id,is_active) VALUES ('w',1,'/fixture',1,1)")
            conn.exec_driver_sql("INSERT INTO coding_sessions (id,user_id,workspace_id,title,persona,runtime_kind,status,context_generation) VALUES ('s',1,'w','Test','fixture','pi','idle',0)")
            conn.exec_driver_sql("INSERT INTO coding_checkouts (id,session_id,user_id,canonical_path,baseline_commit,status) VALUES ('c','s',1,'/checkout','abc','ready')")
            migration = import_module('app.db.migrations.versions.056_coding_workspace_lifecycle')
            migration.upgrade()
            assert conn.exec_driver_sql('SELECT session_id,checkout_id FROM coding_checkout_attachments').first() == ('s','c')
            assert conn.exec_driver_sql('SELECT workspace_id,head_commit FROM coding_checkouts').first() == ('w','abc')
            for table in ('coding_sessions','coding_checkouts','coding_checkout_attachments','coding_runs'):
                assert {c['name'] for c in inspect(conn).get_columns(table)} == set(Base.metadata.tables[table].columns.keys())
            migration.downgrade()
    engine.dispose()


@pytest.mark.asyncio
async def test_lifecycle_api_owner_model_and_attachment_checks(tmp_path):
    async with api_harness(tmp_path) as (client, _, service, _, _, workspace, session, _):
        _, checkout = await enable(service, session, tmp_path)
        assert (await client.get('/coding/checkouts', headers=headers(3))).json() == []
        body = {'workspace_id': workspace.id, 'model_profile_id': 1, 'checkout_id': checkout.id}
        assert (await client.post('/coding/sessions', headers=headers(3), json=body)).status_code == 403
        joined = await client.post('/coding/sessions', json=body)
        assert joined.status_code == 201
        joined_id = joined.json()['id']
        attached = (await client.get(f'/coding/sessions/{joined_id}/checkout')).json()
        assert attached['id'] == checkout.id and attached['session_id'] == joined_id
        assert (await client.post(f'/coding/sessions/{joined_id}/model', json={'model_profile_id': True})).status_code == 422
        assert (await client.post(f'/coding/sessions/{joined_id}/model', json={'model_profile_id': 1})).status_code == 200
        assert (await client.post(f'/coding/sessions/{joined_id}/delivery', json={'action': 'apply'})).status_code == 403


@pytest.mark.asyncio
async def test_delivery_failure_after_git_mutation_retains_work_and_blocks_replay(tmp_path, monkeypatch):
    from app.coding_runtime import git_delivery
    async with harness(tmp_path) as (service, _, _, _, session):
        source, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('committed work\n')
        args = dict(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Keep work')
        plan = await service.delivery(**args)
        original = git_delivery.create_commit
        async def disconnected(*args):
            await original(*args)
            raise OSError('simulated loss after commit')
        monkeypatch.setattr(git_delivery, 'create_commit', disconnected)
        with pytest.raises(CodingServiceError):
            await service.delivery(**args, review_hash=plan['review_hash'])
        state = await service.get_checkout(user_id=2, session_id=session.id)
        assert state.status == 'failed' and state.error_code == 'delivery_uncertain'
        assert git(root, 'show', 'HEAD:file.txt') == b'committed work\n'
        assert (source / 'file.txt').read_text() == 'original\n'
        with pytest.raises(CodingServiceError):
            await service.delivery(**args, review_hash=plan['review_hash'])


@pytest.mark.asyncio
async def test_delivery_refuses_staged_files_and_keeps_operator_index(tmp_path):
    async with harness(tmp_path) as (service, _, _, _, session):
        _, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('operator staged\n')
        git(root, 'add', 'file.txt')
        before = git(root, 'diff', '--cached')
        with pytest.raises(CodingServiceError, match='staged changes'):
            await service.delivery(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='No overwrite')
        assert git(root, 'diff', '--cached') == before


@pytest.mark.asyncio
async def test_git_review_blocks_new_source_runs_until_reservation_finishes(tmp_path, monkeypatch):
    from app.coding_runtime import git_delivery
    async with harness(tmp_path) as (service, _, _, workspace, session):
        _, checkout = await enable(service, session, tmp_path)
        root = Path(checkout.canonical_path)
        (root / 'file.txt').write_text('reviewed\n')
        other = await service.create_session(user_id=2, workspace_id=workspace.id, model_profile_id=1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = git_delivery.commit_plan
        async def blocking(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        monkeypatch.setattr(git_delivery, 'commit_plan', blocking)
        preview = asyncio.create_task(service.delivery(user_id=2, session_id=session.id, action='commit', files=['file.txt'], message='Review'))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with pytest.raises(CodingServiceError, match='delivery is in progress'):
                await service.submit(user_id=2, session_id=other.id, message='Read the source')
            with pytest.raises(CodingServiceError):
                await service.close_session(user_id=2, session_id=session.id)
            with pytest.raises(CodingServiceError):
                await service.change_model(user_id=2, session_id=session.id, model_profile_id=1)
        finally:
            release.set()
            await preview
