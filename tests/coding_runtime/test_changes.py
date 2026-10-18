from __future__ import annotations

import asyncio
from importlib import import_module
import json
import os

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect

from app.coding_runtime import changes
from app.coding_runtime.contracts import CodingRuntimeCapabilities, CodingRuntimeEvent
from app.coding_runtime.repository import RepositoryError
from app.coding_runtime.service import CodingServiceError
from app.db.models import CodingChange, ModelProfile
from app.db.session import Base
from test_service import FakeRuntime, harness, finished, wait_for_status
from test_checkouts import settled
from test_repository import repository
from test_api import api_harness, headers


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', '.git/config', 'sub/.GIT/config', 'a//b', 'a/./b', 'a\nb'])
def test_change_paths_fail_closed(tmp_path, path):
    with pytest.raises((RepositoryError, OSError)):
        changes.apply(tmp_path, path, None, 'new')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'parent_symlink', 'large', 'binary'])
def test_change_rejects_unsafe_targets(tmp_path, kind):
    root = tmp_path / 'repo'
    root.mkdir()
    target = root / 'file'
    private = tmp_path / 'private'
    private.write_text('SECRET')
    path = 'file'
    if kind == 'symlink': target.symlink_to(private)
    if kind == 'hardlink': os.link(private, target)
    if kind == 'fifo': os.mkfifo(target)
    if kind == 'parent_symlink':
        (root / 'sub').symlink_to(tmp_path, target_is_directory=True)
        path = 'sub/private'
    if kind == 'large': target.write_text('a' * (changes.FILE_BYTES + 1))
    if kind == 'binary': target.write_bytes(b'\xff\0')
    with pytest.raises((RepositoryError, OSError, UnicodeError)):
        changes.apply(root, path, 'SECRET', 'new')
    assert private.read_text() == 'SECRET'


def test_host_change_is_exact_atomic_and_refuses_stale_snapshot(tmp_path):
    target = tmp_path / 'file'
    target.write_text('before\r\n')
    target.chmod(0o640)
    changes.apply(tmp_path, 'file', 'before\r\n', 'after\r\n')
    assert target.read_bytes() == b'after\r\n'
    assert target.stat().st_mode & 0o777 == 0o640
    with pytest.raises(RepositoryError):
        changes.apply(tmp_path, 'file', 'before\r\n', 'overwrite')
    assert target.read_bytes() == b'after\r\n'
    changes.apply(tmp_path, 'new', None, '')
    assert (tmp_path / 'new').exists()
    assert '\\ No newline at end of file' in changes.patch('file', 'old', 'new')
    assert list(tmp_path.glob('.fruitcake-change-*')) == []


@pytest.mark.parametrize('character', ['x', '\u00e9', '\U0001f600', '\x01'])
def test_host_preserves_complete_large_files_and_enforces_utf8_byte_boundary(tmp_path, character):
    before = character * (changes.FILE_BYTES // len(character.encode('utf-8')))
    after = 'Y' + before[1:]
    target = tmp_path / 'file'
    target.write_bytes(before.encode('utf-8'))
    changes.apply(tmp_path, 'file', before, after)
    assert target.read_bytes() == after.encode('utf-8')
    for path, old, new in [('file', after, before + 'x'), ('new', None, before + 'x')]:
        with pytest.raises(RepositoryError, match='Do not truncate'):
            changes.apply(tmp_path, path, old, new)
    assert target.read_bytes() == after.encode('utf-8')
    assert not (tmp_path / 'new').exists()


def test_host_snapshot_handles_short_reads_without_truncating(tmp_path, monkeypatch):
    before = 'original\n' + 'preserve\n' * 1000
    (tmp_path / 'file').write_text(before)
    original = os.read
    monkeypatch.setattr(os, 'read', lambda descriptor, count: original(descriptor, min(count, 71)))
    after = before.replace('original', 'approved')
    changes.apply(tmp_path, 'file', before, after)
    assert (tmp_path / 'file').read_text() == after


@pytest.mark.parametrize('tool', ['fruitcake_edit', 'fruitcake_write'])
@pytest.mark.parametrize('oversized', [False, True])
def test_proposal_extension_preserves_large_snapshots_and_matches_host_budgets(tmp_path, tool, oversized):
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for proposal extension tests')
    before = 'original\n' + '\u00e9' * ((changes.FILE_BYTES - 10) // 2) + '\n'
    if oversized:
        before += 'x' * 10
    target = tmp_path / 'file'
    target.write_bytes(before.encode('utf-8'))
    script = '''
      import { registerEditTools, FILE_BYTES, PROPOSAL_BYTES } from './app/coding_runtime/edit_tools.mjs';
      const tools = new Map();
      registerEditTools({registerTool(tool) {tools.set(tool.name, tool);}}, () => process.argv[1]);
      const name = process.argv[2];
      let proposal = null;
      const ctx = {cwd: process.argv[1], ui: {async confirm(title, message) {proposal = JSON.parse(message); return false;}}};
      const input = name === 'fruitcake_edit' ? {path: 'file', oldText: 'original', newText: 'approved'}
        : {path: 'file', content: 'complete replacement\\n'};
      try {await tools.get(name).execute('op', input, null, null, ctx);}
      catch (error) {process.stdout.write(JSON.stringify({proposal, error: error.message, fileBytes: FILE_BYTES, proposalBytes: PROPOSAL_BYTES}));}
    '''
    result = subprocess.run([node, '--input-type=module', '-e', script, str(tmp_path), tool],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload['fileBytes'] == changes.FILE_BYTES
    assert payload['proposalBytes'] == changes.PROPOSAL_BYTES
    assert target.read_bytes() == before.encode('utf-8')
    if oversized:
        assert payload['proposal'] is None and 'do not truncate' in payload['error']
    else:
        assert payload['proposal']['before'] == before
        assert payload['proposal']['after'] == (before.replace('original', 'approved') if tool == 'fruitcake_edit' else 'complete replacement\n')


@pytest.mark.asyncio
@pytest.mark.parametrize('character', ['\x01', '\u00e9'])
async def test_large_approval_api_and_stream_preserve_complete_diff_with_separate_budget(tmp_path, character):
    from pathlib import Path
    from app.coding_runtime.output import payload_size
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        before = 'original\n' + character * ((changes.FILE_BYTES - 10) // len(character.encode('utf-8'))) + '\n'
        after = before.replace('original', 'approved')
        service._factory = lambda: EditRuntime(before=before, after=after)
        source, checkout = await enable(service, session, tmp_path)
        target = Path(checkout.canonical_path) / 'file.txt'
        target.write_bytes(before.encode('utf-8'))
        run = await service.submit(user_id=2, session_id=session.id, message='Change one word; preserve everything else')
        await wait_for_status(service, run.id, 'waiting_approval')
        approvals = (await client.get(f'/coding/runs/{run.id}/approvals')).json()
        approval = approvals[0]
        expected_diff = changes.patch('file.txt', before, after)
        assert approval['change']['diff'] == expected_diff
        events = [event async for event in service.events(user_id=2, run_id=run.id)]
        event = next(event for event in events if event.kind == 'approval.requested')
        assert event.payload['change']['diff'] == expected_diff
        assert service.limits.max_payload_bytes < payload_size(event.payload) <= changes.APPROVAL_BYTES
        assert target.read_bytes() == before.encode('utf-8')
        response = await client.post(f"/coding/approvals/{approval['id']}/resolve", json={
            'approved': True, 'change_hash': approval['change']['after_hash'],
        })
        assert response.status_code == 204
        result, _ = await finished(service, run)
        assert result.status == 'completed'
        assert target.read_bytes() == after.encode('utf-8')
        assert (source / 'file.txt').read_text() == 'original\n'
        record = (await client.get(f'/coding/runs/{run.id}/changes')).json()[0]
        assert record['diff'] == expected_diff and record['status'] == 'applied'
        streamed = (await client.get(f'/coding/runs/{run.id}/events?follow=false')).text
        envelopes = [json.loads(line[6:]) for line in streamed.splitlines() if line.startswith('data: ')]
        streamed_approval = next(event for event in envelopes if event['kind'] == 'approval.requested')
        assert streamed_approval['payload']['change']['diff'] == expected_diff
        assert len(json.dumps(streamed_approval, ensure_ascii=False).encode()) < changes.APPROVAL_BYTES + 16 * 1024


class EditRuntime(FakeRuntime):
    def __init__(self, *, path='file.txt', before='original\n', after='approved\n', timeout=10000):
        super().__init__()
        self.path, self.before, self.after, self.timeout = path, before, after, timeout

    def capabilities(self):
        return CodingRuntimeCapabilities(confirmations=True, approved_edits=True)

    async def run(self, prompt):
        yield CodingRuntimeEvent('interaction.requested', {
            'id': 'private-runtime-request', 'method': 'confirm', 'title': 'Approve file change?',
            'message': json.dumps({'protocol': changes.CHANGE_PROTOCOL, 'tool': 'fruitcake_write',
                                  'path': self.path, 'before': self.before, 'after': self.after}),
            'timeout': self.timeout,
        })
        await self.confirmation.wait()
        yield CodingRuntimeEvent('operation.completed', {'operation_id': 'edit', 'tool': 'fruitcake_write', 'is_error': not self.approved})
        yield CodingRuntimeEvent('run.completed', {'aborted': self.aborted})


async def enable(service, session, tmp_path):
    source = repository(tmp_path / 'repo')
    await service.create_checkout(user_id=2, session_id=session.id)
    checkout = await settled(service, session.id)
    await service.set_edit_mode(user_id=2, session_id=session.id, enabled=True)
    return source, checkout


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['allow', 'deny', 'stale', 'stop', 'expire', 'policy'])
async def test_host_owns_approval_mutation_and_durable_evidence(tmp_path, decision):
    from pathlib import Path
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        runtime = EditRuntime(timeout=30 if decision == 'expire' else 10000)
        service._factory = lambda: runtime
        source, checkout = await enable(service, session, tmp_path)
        target = Path(checkout.canonical_path) / 'file.txt'
        run = await service.submit(user_id=2, session_id=session.id, message='Make an approved change')
        await wait_for_status(service, run.id, 'waiting_approval')
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        assert target.read_text() == 'original\n'
        assert '+approved' in approval.change['diff']
        with pytest.raises(CodingServiceError):
            await service.resolve_approval(user_id=3, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
        if decision == 'stale': target.write_text('user edit\n')
        if decision == 'policy':
            async with sessions() as db, db.begin():
                (await db.get(ModelProfile, 1)).blocked_tools_json = '["write"]'
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
            await service.stop(user_id=2, run_id=run.id)
        elif decision == 'expire':
            await asyncio.sleep(0.06)
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
            await service.stop(user_id=2, run_id=run.id)
        elif decision == 'stop':
            await service.stop(user_id=2, run_id=run.id)
            with pytest.raises(CodingServiceError):
                await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
        else:
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=decision != 'deny', change_hash=approval.change["after_hash"])
        await finished(service, run)
        saved = await service.list_changes(user_id=2, run_id=run.id)
        assert len(saved) == 1
        assert saved[0].status == {'allow': 'applied', 'deny': 'denied', 'stale': 'failed'}.get(decision, 'cancelled')
        assert target.read_text() == {'allow': 'approved\n', 'stale': 'user edit\n'}.get(decision, 'original\n')
        assert (source / 'file.txt').read_text() == 'original\n'
        if decision == 'allow':
            target.write_text('later local edit\n')
            assert (await service.list_changes(user_id=2, run_id=run.id))[0].diff == saved[0].diff
        await service.shutdown()
        await service.startup()
        assert (await service.list_changes(user_id=2, run_id=run.id))[0].status == saved[0].status


@pytest.mark.asyncio
async def test_edit_api_requires_checkout_and_scopes_saved_changes(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        path = f'/coding/sessions/{session.id}/edit-mode'
        assert (await client.post(path, json={'enabled': True})).status_code == 409
        runtime = EditRuntime()
        service._factory = lambda: runtime
        await enable(service, session, tmp_path)
        assert (await client.post(path, json={'enabled': True})).json()['edit_mode']
        assert (await client.post(path, json={'enabled': True, 'force': True})).status_code == 422
        run = await service.submit(user_id=2, session_id=session.id, message='Change')
        await wait_for_status(service, run.id, 'waiting_approval')
        assert (await client.post(path, json={'enabled': False})).status_code == 409
        approvals = (await client.get(f'/coding/runs/{run.id}/approvals')).json()
        assert approvals[0]['change']['diff'].endswith('+approved\n')
        resolve = f"/coding/approvals/{approvals[0]['id']}/resolve"
        assert (await client.post(resolve, json={'approved': True})).status_code == 409
        assert (await client.post(resolve, json={'approved': True, 'change_hash': '0' * 64})).status_code == 409
        assert (await client.get(f'/coding/runs/{run.id}/changes')).json()[0]['status'] == 'proposed'
        response = await client.post(resolve, json={'approved': True, 'change_hash': approvals[0]['change']['after_hash']})
        assert response.status_code == 204
        await finished(service, run)
        changes_path = f'/coding/runs/{run.id}/changes'
        assert (await client.get(changes_path)).json()[0]['status'] == 'applied'
        for user in (1, 3):
            assert (await client.get(changes_path, headers=headers(user))).status_code == 404
        assert (await client.post(path, json={'enabled': False})).json()['edit_mode'] is False


def test_change_migration_matches_orm_and_preserves_checkout_data(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "migration.sqlite"}')
    try:
        with engine.begin() as connection:
            for table in ('users', 'user_projects', 'model_profiles'):
                connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            with Operations.context(MigrationContext.configure(connection)):
                for name in ('052_coding_sessions', '053_coding_checkouts', '054_coding_changes', '055_coding_command_mode'):
                    import_module('app.db.migrations.versions.' + name).upgrade()
                for name in ('coding_sessions', 'coding_changes'):
                    expected = set(Base.metadata.tables[name].columns.keys())
                    if name == 'coding_sessions':
                        expected -= {'archived_at', 'deleted_at'}  # Added by migration 056.
                    assert {c['name'] for c in inspect(connection).get_columns(name)} == expected
                migration = import_module('app.db.migrations.versions.054_coding_changes')
                import_module('app.db.migrations.versions.055_coding_command_mode').downgrade()
                migration.downgrade()
                assert 'coding_changes' not in inspect(connection).get_table_names()
                assert 'coding_checkouts' in inspect(connection).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_restart_marks_inflight_change_uncertain_without_reapplying(tmp_path):
    from pathlib import Path
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        service._factory = lambda: EditRuntime()
        source, checkout = await enable(service, session, tmp_path)
        run = await service.submit(user_id=2, session_id=session.id, message='Change')
        await wait_for_status(service, run.id, 'waiting_approval')
        record = (await service.list_changes(user_id=2, run_id=run.id))[0]
        async with sessions() as db, db.begin():
            (await db.get(CodingChange, record.id)).status = 'applying'
        await service.shutdown()
        await service.startup()
        saved = (await service.list_changes(user_id=2, run_id=run.id))[0]
        assert saved.status == 'uncertain' and saved.error_code == 'backend_restart'
        assert (Path(checkout.canonical_path) / 'file.txt').read_text() == 'original\n'
        assert (source / 'file.txt').read_text() == 'original\n'


@pytest.mark.asyncio
async def test_lost_runtime_confirmation_does_not_erase_applied_evidence(tmp_path):
    from pathlib import Path
    async with harness(tmp_path) as (service, sessions, factory, grant, session):
        runtime = EditRuntime()
        async def disconnected(*args):
            raise OSError('disconnected')
        runtime.confirm = disconnected
        service._factory = lambda: runtime
        _, checkout = await enable(service, session, tmp_path)
        run = await service.submit(user_id=2, session_id=session.id, message='Change')
        await wait_for_status(service, run.id, 'waiting_approval')
        approval = (await service.list_approvals(user_id=2, run_id=run.id))[0]
        with pytest.raises(CodingServiceError) as error:
            await service.resolve_approval(user_id=2, approval_id=approval.id, approved=True, change_hash=approval.change["after_hash"])
        assert error.value.code == 'approval_delivery_failed'
        await finished(service, run)
        assert (Path(checkout.canonical_path) / 'file.txt').read_text() == 'approved\n'
        assert (await service.list_changes(user_id=2, run_id=run.id))[0].status == 'applied'


def test_change_failure_after_replace_is_uncertain(tmp_path, monkeypatch):
    import stat
    original = os.fsync
    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError('simulated verification failure')
        original(descriptor)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    with pytest.raises(RepositoryError) as error:
        changes.apply(tmp_path, 'file', None, 'written')
    assert error.value.code == 'change_uncertain'
    assert (tmp_path / 'file').read_text() == 'written'


def test_new_file_cleanup_failure_is_not_reported_as_unapplied(tmp_path, monkeypatch):
    original = os.unlink
    def fail_cleanup(path, **kwargs):
        if str(path).startswith('.fruitcake-change-'):
            raise PermissionError('simulated cleanup failure')
        return original(path, **kwargs)
    monkeypatch.setattr(os, 'unlink', fail_cleanup)
    with pytest.raises(RepositoryError) as error:
        changes.apply(tmp_path, 'file', None, 'written')
    assert error.value.code == 'change_uncertain'
    assert (tmp_path / 'file').read_text() == 'written'


@pytest.mark.parametrize('case', ['relative', 'absolute', 'outside', 'sibling_prefix', 'traversal', 'git_metadata', 'root'])
def test_proposal_path_normalization_keeps_host_contract_relative(tmp_path, case):
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for proposal extension tests')
    root = tmp_path / 'checkout'
    paths = {'relative': 'js/AgentMode.js', 'absolute': str(root / 'js/AgentMode.js'),
             'outside': str(tmp_path / 'source/AgentMode.js'), 'sibling_prefix': str(tmp_path / 'checkout-other/file'),
             'traversal': str(root / 'sub/../file'), 'git_metadata': str(root / '.git/config'), 'root': str(root)}
    script = '''
      import { changePath } from './app/coding_runtime/edit_tools.mjs';
      try { process.stdout.write(JSON.stringify({path: changePath(process.argv[1], process.argv[2])})); }
      catch (error) { process.stdout.write(JSON.stringify({error: error.message})); }
    '''
    result = subprocess.run([node, '--input-type=module', '-e', script, str(root), paths[case]],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    if case in {'relative', 'absolute'}:
        assert payload == {'path': 'js/AgentMode.js'}
    else:
        assert 'error' in payload
