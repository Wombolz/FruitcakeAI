"""Single-supervisor ownership and transactional checkout reservations."""
import fcntl
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.db.models import CodingCheckoutReservation


def failure(code, message):
    from app.coding_runtime.service import CodingServiceError
    return CodingServiceError(code, message)


class SupervisorFence:
    """Hold a DB-session advisory lock (or file-backed SQLite test equivalent)."""
    def __init__(self, sessions):
        self.engine = sessions.kw["bind"]
        self.connection = None
        self.descriptor = None

    async def acquire(self):
        if self.engine.dialect.name == "postgresql":
            connection = await self.engine.connect()
            try:
                if not await connection.scalar(text("SELECT pg_try_advisory_lock(719364, 58001)")):
                    raise failure("coding_owner_busy", "Another coding supervisor owns this database")
                await connection.commit()
                self.connection = connection
            except BaseException:
                await connection.invalidate()
                await connection.close()
                raise
        elif self.engine.dialect.name == "sqlite" and self.engine.url.database not in (None, ":memory:"):
            path = Path(self.engine.url.database).with_suffix(".coding-owner.lock")
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.descriptor = descriptor
            except BlockingIOError:
                os.close(descriptor)
                raise failure("coding_owner_busy", "Another coding supervisor owns this database") from None
        else:
            raise failure("unsupported", "Capture admission requires PostgreSQL or file-backed SQLite")

    async def close(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            try:
                if not connection.invalidated:
                    await connection.execute(text("SELECT pg_advisory_unlock(719364, 58001)"))
                    await connection.commit()
            finally:
                # Never return a potentially still-locked connection to the pool.
                await connection.invalidate()
                await connection.close()
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    async def check(self):
        if self.engine.dialect.name == "postgresql":
            if self.connection is None or self.connection.invalidated:
                raise failure("coding_owner_lost", "Coding supervisor ownership was lost; restart safely")
            try:
                owned = await self.connection.scalar(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                    AND pid=pg_backend_pid() AND classid=719364 AND objid=58001 AND objsubid=2)
                """))
                await self.connection.commit()
            except Exception:
                raise failure("coding_owner_lost", "Coding supervisor ownership could not be confirmed") from None
            if not owned:
                raise failure("coding_owner_lost", "Coding supervisor ownership was lost; restart safely")
        elif self.descriptor is None:
            raise failure("coding_owner_lost", "Coding supervisor ownership was lost; restart safely")


async def claim(db, checkout_id, kind, holder_id, epoch):
    token = str(uuid4())
    try:
        async with db.begin_nested():
            db.add(CodingCheckoutReservation(checkout_id=checkout_id, holder_kind=kind,
                holder_id=holder_id, token=token, service_epoch=epoch))
            await db.flush()
    except IntegrityError:
        raise failure("checkout_busy", "Another run or capture is using this checkout") from None
    return token


async def release(db, kind, holder_id, epoch, *, token=None):
    query = delete(CodingCheckoutReservation).where(
        CodingCheckoutReservation.holder_kind == kind,
        CodingCheckoutReservation.holder_id == holder_id,
        CodingCheckoutReservation.service_epoch == epoch,
    )
    if token is not None:
        query = query.where(CodingCheckoutReservation.token == token)
    await db.execute(query)
