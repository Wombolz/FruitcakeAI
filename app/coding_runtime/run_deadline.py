"""An execution budget that excludes time spent waiting for human decisions."""
import asyncio


class RunDeadline:
    def __init__(self, seconds):
        self.remaining = seconds
        self.pending = set()
        self.timer = None

    async def __aenter__(self):
        self.timer = asyncio.timeout(self.remaining)
        await self.timer.__aenter__()
        return self

    async def __aexit__(self, *args):
        timer, self.timer = self.timer, None
        return await timer.__aexit__(*args)

    def pause(self, approval_id):
        if self.timer is not None and not self.pending:
            self.remaining = self.timer.when() - asyncio.get_running_loop().time()
            # An already exhausted budget must not become an unlimited wait.
            if self.remaining <= 0:
                raise TimeoutError
            self.timer.reschedule(None)
        self.pending.add(approval_id)

    def resolve(self, approval_id):
        if approval_id not in self.pending:
            return
        self.pending.remove(approval_id)
        if self.timer is not None and not self.pending and not self.timer.expired():
            self.timer.reschedule(asyncio.get_running_loop().time() + self.remaining)
