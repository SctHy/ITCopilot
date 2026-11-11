"""Bounded event-driven Agent workers; waiting for approval holds no worker slot."""
import asyncio
import logging

from core.concurrency import LeaseLostError, run_leased
from core.protocols import SERVICE

logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(self, store, handle, *, concurrency=2, poll_s=1, lease_s=120, timeout_s=120):
        self.store, self.handle = store, handle
        self.concurrency, self.poll_s = concurrency, poll_s
        self.lease_s, self.timeout_s = lease_s, timeout_s
        self.lifecycle = SERVICE.start()
        self._workers = []
        self._wake = asyncio.Event()

    def start(self):
        if self.lifecycle.state == "running":
            return
        self.lifecycle.send("start")
        self._workers = [asyncio.create_task(self._loop(), name=f"service-task-{i}") for i in range(self.concurrency)]

    def kick(self):
        self._wake.set()

    async def stop(self):
        if self.lifecycle.state == "stopped":
            return
        self.lifecycle.send("stop")
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self.lifecycle.send("stopped")

    async def _loop(self):
        while self.lifecycle.state == "running":
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.exception("shared task worker iteration failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_s)
            except TimeoutError:
                pass

    async def run_once(self):
        claim = await asyncio.to_thread(self.store.claim, self.lease_s)
        if not claim:
            return False

        async def operation():
            try:
                context = await asyncio.to_thread(self.store.context, claim)
                context[0]['_claim'] = dict(claim)  # transient internal context, never a public task field
                result = await asyncio.wait_for(self.handle(*context), self.timeout_s)
                await asyncio.to_thread(self.store.finish, claim, result)
            except LeaseLostError:
                raise
            except Exception as exc:
                logger.exception("shared task run failed: %s", claim["id"])
                await asyncio.to_thread(self.store.fail, claim, type(exc).__name__)

        async def renew():
            return await asyncio.to_thread(self.store.renew, claim, self.lease_s)

        try:
            await run_leased(operation, renew, self.lease_s / 3)
        except LeaseLostError:
            logger.info("shared task claim lost: %s", claim["id"])
        return True
