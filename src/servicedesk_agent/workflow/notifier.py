"""Transactional PostgreSQL outbox wakeups with bounded polling during outages."""
import asyncio
import logging
from contextlib import contextmanager
from threading import Lock
from weakref import WeakSet

from servicedesk_agent.infra.monitor import metrics

import psycopg
from sqlalchemy import text
from sqlalchemy.engine import make_url

CHANNEL = "servicedesk_agent_action_ready"
logger = logging.getLogger(__name__)


class ActionNotifier:
    _local = WeakSet()
    _lock = Lock()

    def __init__(self, url):
        self.url = make_url(url)
        self.event = asyncio.Event()
        self.source = None
        self.task = self.loop = None

    @contextmanager
    def transaction(self, engine):
        with engine.begin() as db:
            info = db.info
            try:
                yield db
            finally:
                wake = info.pop("workflow_action_ready", False)
        if wake:
            with self._lock:
                for listener in self._local:
                    if listener.url == self.url:
                        listener.loop.call_soon_threadsafe(listener.signal, 'kick')

    def publish(self, db, payload):
        if db.dialect.name == "postgresql":
            db.execute(text("SELECT pg_notify('servicedesk_agent_action_ready', :payload)"), {"payload": payload})
        else:
            db.info["workflow_action_ready"] = True

    def start(self):
        self.loop = asyncio.get_running_loop()
        if self.url.get_backend_name() == "postgresql":
            self.task = self.loop.create_task(self._listen())
        else:
            with self._lock:
                self._local.add(self)

    def signal(self, source):
        if source not in {"notify", "kick"}:
            raise ValueError("unrecognized wakeup source")
        self.source = source
        self.event.set()

    async def _listen(self):
        dsn = self.url.set(drivername="postgresql").render_as_string(hide_password=False)
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.signal("kick")
                    async for _ in conn.notifies():
                        self.signal("notify")
            except psycopg.Error:
                logger.warning("action listener disconnected", exc_info=True)
                await asyncio.sleep(1)

    async def wait(self, timeout):
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except TimeoutError:
            metrics.WORKFLOW_WAKEUPS.labels("poll").inc()
        else:
            if self.source is None:
                return
            metrics.WORKFLOW_WAKEUPS.labels(self.source).inc()

    async def close(self):
        with self._lock:
            self._local.discard(self)
            self.loop = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.source = None
        self.event.set()
