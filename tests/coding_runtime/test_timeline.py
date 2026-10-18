import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.coding_runtime.contracts import CodingRuntimeEvent
from app.db.models import CodingApproval
from test_api import api_harness
from test_service import FakeRuntime, finished, wait_for_status


class ParallelApprovals(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.decisions = asyncio.Queue()

    async def run(self, prompt):
        for index in range(7):
            yield CodingRuntimeEvent('operation.started', {'operation_id': f'op-{index}', 'tool': 'read'})
            yield CodingRuntimeEvent('interaction.requested', {
                'id': f'private-request-{index}', 'method': 'confirm', 'title': f'Read file {index}?',
                'message': json.dumps({'operation_id': f'op-{index}', 'tool': 'read', 'path': f'file-{index}.txt'}),
                'timeout': 10000,
            })
        for _ in range(7):
            index = await self.decisions.get()
            if index is None:
                break
            yield CodingRuntimeEvent('operation.completed', {'operation_id': f'op-{index}', 'tool': 'read', 'is_error': False})
        yield CodingRuntimeEvent('run.completed', {'aborted': self.aborted})

    async def confirm(self, request_id, approved):
        self.decisions.put_nowait(int(request_id.rsplit('-', 1)[1]))

    async def abort(self):
        self.aborted = True
        self.decisions.put_nowait(None)


async def seven_approvals(service, run_id):
    async with asyncio.timeout(3):
        while True:
            records = await service.list_approvals(user_id=2, run_id=run_id)
            if len(records) == 7:
                return records
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_seven_approvals_keep_event_order_and_survive_other_operations(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        service._factory = ParallelApprovals
        run = await service.submit(user_id=2, session_id=session.id, message='Inspect seven files')
        records = await seven_approvals(service, run.id)
        # Equal/reversed timestamps cannot determine approval order; event
        # sequence is authoritative, including for older stored records.
        async with sessions() as db, db.begin():
            for index, record in enumerate(records):
                (await db.get(CodingApproval, record.id)).created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=index)
        path = f'/coding/runs/{run.id}/approvals'
        data = (await client.get(path)).json()
        assert [row['title'] for row in data] == [f'Read file {index}?' for index in range(7)]
        assert [row['request_sequence'] for row in data] == sorted(row['request_sequence'] for row in data)
        await service._record(run.id, 'operation.completed', {'operation_id': 'unrelated', 'tool': 'ls', 'is_error': False})
        assert all(row.status == 'pending' for row in await service.list_approvals(user_id=2, run_id=run.id))
        assert (await service.get_run(user_id=2, run_id=run.id)).status == 'waiting_approval'
        for record in records[:3]:
            await service.resolve_approval(user_id=2, approval_id=record.id, approved=True)
        async with asyncio.timeout(3):
            while sum(event.kind == 'operation.completed' for event in [event async for event in service.events(user_id=2, run_id=run.id)]) < 4:
                await asyncio.sleep(0.01)
        remaining = (await client.get(path)).json()
        assert [row['status'] for row in remaining] == ['approved'] * 3 + ['pending'] * 4
        assert (await service.get_run(user_id=2, run_id=run.id)).status == 'waiting_approval'
        for record in records[3:]:
            await service.resolve_approval(user_id=2, approval_id=record.id, approved=True)
        result, events = await finished(service, run)
        assert result.status == 'completed'
        assert len((await client.get(path)).json()) == 7
        requested = [event for event in events if event.kind == 'approval.requested']
        assert all('expires_at' in event.payload for event in requested)
        assert 'private-request-' not in json.dumps([event.payload for event in events])


@pytest.mark.asyncio
async def test_expired_operation_settles_only_its_own_approval(tmp_path):
    async with api_harness(tmp_path) as (client, app, service, sessions, factory, grant, session, _):
        service._factory = ParallelApprovals
        run = await service.submit(user_id=2, session_id=session.id, message='Inspect files')
        records = await seven_approvals(service, run.id)
        async with sessions() as db, db.begin():
            (await db.get(CodingApproval, records[0].id)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await service._record(run.id, 'operation.completed', {'operation_id': 'op-0', 'tool': 'read', 'is_error': True})
        saved = await service.list_approvals(user_id=2, run_id=run.id)
        assert [row.status for row in saved] == ['expired'] + ['pending'] * 6
        assert (await service.get_run(user_id=2, run_id=run.id)).status == 'waiting_approval'
        await service.stop(user_id=2, run_id=run.id)
        await finished(service, run)
        saved = await service.list_approvals(user_id=2, run_id=run.id)
        assert [row.status for row in saved] == ['expired'] + ['cancelled'] * 6
        events = [event async for event in service.events(user_id=2, run_id=run.id)]
        resolved = [event.payload for event in events if event.kind == 'approval.resolved']
        assert len(resolved) == 7
        assert resolved[0] == {'approval_id': records[0].id, 'status': 'expired'}
