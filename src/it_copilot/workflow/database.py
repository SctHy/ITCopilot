"""Run the same Core transaction functions on synchronous and asynchronous drivers."""
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.util import greenlet_spawn


class WorkflowDatabase:
    def __init__(self, engine):
        self.sync = engine
        self._async_context = ContextVar("workflow_async_driver", default=False)
        url = engine.url
        sqlite = url.get_backend_name() == "sqlite"
        url = url.set(drivername="sqlite+aiosqlite" if sqlite else "postgresql+psycopg")
        options = {"poolclass": NullPool, "connect_args": {"timeout": 15}} if sqlite else {"pool_pre_ping": True}
        self.async_engine = create_async_engine(url, **options)
        if sqlite:
            @event.listens_for(self.async_engine.sync_engine, "begin")
            def begin(conn):
                conn.exec_driver_sql("BEGIN" if conn.get_execution_options().get("read_only") else "BEGIN IMMEDIATE")

    @property
    def engine(self):
        return self.async_engine.sync_engine if self._async_context.get() else self.sync

    def begin(self):
        return self.engine.begin()

    def connect(self):
        return self.engine.connect()

    async def run(self, operation, *args, **kwargs):
        # Repository operations own their transaction boundaries, sometimes several per call.
        # SQLAlchemy's greenlet adapter yields at each async-driver I/O without using a thread.
        token = self._async_context.set(True)
        try:
            return await greenlet_spawn(operation, *args, **kwargs)
        finally:
            self._async_context.reset(token)
