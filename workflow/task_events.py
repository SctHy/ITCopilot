"""Independent source polling. Slow/failed sources never occupy an Agent worker slot."""
import asyncio
import logging

from workflow.task_ports import TaskEventSource

logger = logging.getLogger(__name__)


class TaskEventPump:
    def __init__(self, sources: list[TaskEventSource], *, poll_s=1):
        self.sources, self.poll_s = sources, poll_s
        self._tasks = []
        self._wake = asyncio.Event()

    def start(self):
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._loop(source), name="task-event-source") for source in self.sources]

    def kick(self):
        self._wake.set()

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def poll(self, source):
        work = asyncio.create_task(asyncio.to_thread(source.poll))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            # Drain the database operation before the composition root closes its engine.
            await asyncio.gather(work, return_exceptions=True)
            raise
        except Exception:
            logger.exception("task event source failed: %s", type(source).__name__)

    async def run_once(self):
        await asyncio.gather(*(self.poll(source) for source in self.sources))

    async def _loop(self, source):
        while True:
            self._wake.clear()
            await self.poll(source)
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_s)
            except TimeoutError:
                pass
