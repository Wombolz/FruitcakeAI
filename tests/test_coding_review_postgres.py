"""Opt-in transactional checks against a disposable DB, never the operator DB."""
import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.coding_runtime.review_admission import SupervisorFence, claim, release
from app.coding_runtime.service import CodingServiceError
from app.db.models import CodingCheckoutReservation
from tests.test_database_roles import postgres_container


@pytest.mark.skipif(os.environ.get("FRUITCAKE_TEST_DOCKER") != "1", reason="opt-in disposable PostgreSQL integration")
def test_real_postgres_reservation_race_and_supervisor_fence(tmp_path):
    with postgres_container(tmp_path) as url:
        async def check():
            engine = create_async_engine(url.set(drivername="postgresql+asyncpg"))
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            first, second = SupervisorFence(sessions), SupervisorFence(sessions)
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("CREATE TABLE coding_checkouts(id varchar(64) PRIMARY KEY)"))
                    await connection.execute(text("INSERT INTO coding_checkouts VALUES ('checkout')"))
                    await connection.run_sync(CodingCheckoutReservation.__table__.create)
                await first.acquire()
                await first.check()
                with pytest.raises(CodingServiceError) as exc:
                    await second.acquire()
                assert exc.value.code == "coding_owner_busy"
                start = asyncio.Event()
                prepared = [asyncio.Event(), asyncio.Event()]
                async def attempt(index, kind):
                    async with sessions() as db:
                        prepared[index].set()
                        await start.wait()
                        try:
                            async with db.begin():
                                token = await claim(db, "checkout", kind, str(index), "epoch")
                            return kind, token
                        except CodingServiceError as error:
                            assert error.code == "checkout_busy"
                            return None
                tasks = [asyncio.create_task(attempt(0, "builder")), asyncio.create_task(attempt(1, "capture"))]
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in prepared)), 3)
                start.set()
                winners = [winner for winner in await asyncio.wait_for(asyncio.gather(*tasks), 5) if winner]
                assert len(winners) == 1
                async with sessions() as db, db.begin():
                    winner = await db.get(CodingCheckoutReservation, "checkout")
                    await release(db, winner.holder_kind, winner.holder_id, "old-epoch", token=winner.token)
                async with sessions() as db:
                    assert await db.get(CodingCheckoutReservation, "checkout") is not None
                # Invalidating the dedicated connection must fail closed, not
                # transparently acquire ownership on a replacement connection.
                await first.connection.invalidate()
                with pytest.raises(CodingServiceError) as exc:
                    await first.check()
                assert exc.value.code == "coding_owner_lost"
                await first.close()
                await second.acquire()
                await second.check()
            finally:
                await first.close()
                await second.close()
                await engine.dispose()
        asyncio.run(check())
