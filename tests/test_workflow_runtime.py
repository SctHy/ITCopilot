import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import uvicorn
from sqlalchemy import true
from sqlalchemy.dialects import postgresql

from servicedesk_agent.api.routes.ops import ready
from servicedesk_agent.cmd.api import DrainingServer
from servicedesk_agent.cmd.worker import management_app, serve
from servicedesk_agent.workflow.actions import ActionTransactions
from servicedesk_agent.workflow.notifier import ActionNotifier
from servicedesk_agent.workflow.store import LeaseLost, WorkflowStore
from servicedesk_agent.workflow.worker import ExecutionWorker, build_registry
from tests.support.application import application
from tests.test_workflow import BOB, create


def test_postgres_claim_and_notification_sql():
    sql = str(ActionTransactions.lock_next(true(), (), None).compile(dialect=postgresql.dialect()))
    assert "ORDER BY actions.not_before" in sql and "LIMIT" in sql and "FOR UPDATE SKIP LOCKED" in sql
    db = Mock()
    db.dialect.name = "postgresql"
    ActionNotifier("postgresql://").publish(db, "action")
    stmt, params = db.execute.call_args.args
    assert str(stmt) == "SELECT pg_notify('servicedesk_agent_action_ready', :payload)"
    assert params == {"payload": "action"}


@pytest.mark.asyncio
async def test_local_notify_on_commit_only_and_poll_fallback(tmp_path):
    store = WorkflowStore(f"sqlite:///{tmp_path}/events.sqlite")
    notifier = store.notifier
    notifier.start()
    try:
        with pytest.raises(ValueError), store.transaction() as db:
            notifier.publish(db, "rolled-back")
            raise ValueError()
        await asyncio.wait_for(notifier.wait(.01), .5)
        assert not notifier.event.is_set()
        pending = create(store)
        await store.run(store.approvals.decide, "a", "approved", BOB, pending["payload_hash"])
        await asyncio.wait_for(notifier.event.wait(), .5)
        [claimed] = await store.actions.claim("a", 60)
        await store.actions.release_lease(claimed, uncertain=False)
        [reclaimed] = await store.actions.claim("b", 60)
        assert reclaimed["id"] == claimed["id"] and reclaimed["prev_status"] == "queued"
        with pytest.raises(LeaseLost):
            await store.actions.release_lease(claimed)
    finally:
        await notifier.close()
        await store.aclose()


@pytest.mark.asyncio
async def test_api_signal_marks_detached_process_unready():
    async with application(embed_worker=False) as (http, state):
        assert state.worker is None and state.retention._task is None
        assert (await http.get("/readyz")).status_code == 200
        server = DrainingServer(uvicorn.Config(SimpleNamespace(state=SimpleNamespace(container=state))))
        server.handle_exit(signal.SIGTERM, None)
        assert server.should_exit and (await ready(state)).status_code == 503
        assert (await http.get("/livez")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [True, False])
async def test_worker_signal_drains_or_releases(db_url, monkeypatch, finish):
    store = WorkflowStore(db_url)
    registry = build_registry(store)
    worker = ExecutionWorker(store, registry)
    pending = create(store)
    store.decide("a", "approved", BOB, pending["payload_hash"])
    started, resume = asyncio.Event(), asyncio.Event()
    apply = registry.local.apply

    async def delayed(*args):
        started.set()
        await resume.wait()
        return await apply(*args)

    monkeypatch.setattr(registry.local, "apply", delayed)
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", handlers.__setitem__)
    monkeypatch.setattr(loop, "remove_signal_handler", handlers.pop)
    monkeypatch.delenv("SERVICEDESK_AGENT_WORKER_METRICS_ADDR", raising=False)
    monkeypatch.setenv("SERVICEDESK_AGENT_SHUTDOWN_GRACE_S", "1" if finish else ".02")
    service = asyncio.create_task(serve(worker, store))
    await asyncio.wait_for(started.wait(), 3)
    handlers[signal.SIGTERM]()
    probe = next(r.endpoint for r in management_app(worker).routes if r.path == "/readyz")
    assert (await probe()).status_code == 503
    if finish:
        resume.set()
    await asyncio.wait_for(service, 3)
    if finish:
        assert store.get("a")["execution"] == "succeeded"
    else:
        [action] = await store.actions.claim("replacement", 60)
        assert action["prev_status"] == "unknown"
    await store.aclose()


def test_standalone_processes_serve_probes_and_exit_on_sigterm(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from contextlib import ExitStack

    def unused_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    monkeypatch.setenv("SERVICEDESK_AGENT_BUILD_SHA", "test")
    monkeypatch.setenv("SERVICEDESK_AGENT_DATABASE_URL", f"sqlite:///{tmp_path}/app.sqlite")
    monkeypatch.setenv("SERVICEDESK_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SERVICEDESK_AGENT_REQUIRE_REDIS", "false")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setenv("SERVICEDESK_AGENT_EMBED_WORKER", "false")
    monkeypatch.setenv("SERVICEDESK_AGENT_SEED_DEMO_DOCS", "false")
    monkeypatch.setenv("SERVICEDESK_AGENT_GITEA_REPOS", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    with ExitStack() as resources:
        processes = {}
        for name in ("api", "worker"):
            port = unused_port()
            env = dict(os.environ, SERVICEDESK_AGENT_PORT=str(port), SERVICEDESK_AGENT_WORKER_METRICS_ADDR=f"127.0.0.1:{port}")
            log = resources.enter_context((tmp_path / f"{name}.log").open("w+"))
            proc = subprocess.Popen([sys.executable, "-m", f"servicedesk_agent.cmd.{name}"], env=env, stdout=log, stderr=log)
            resources.callback(proc.wait, 5)
            resources.callback(proc.kill)
            processes[name] = proc
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=.5) as resp:
                        assert resp.status == 200
                    break
                except OSError:
                    time.sleep(.05)
            else:
                log.seek(0)
                pytest.fail(f"{name} failed to start: {log.read()[-2000:]}")
        for proc in processes.values():
            proc.send_signal(signal.SIGTERM)
        assert processes["worker"].wait(5) == 0
        assert processes["api"].wait(5) in (0, -signal.SIGTERM)


@pytest.mark.asyncio
async def test_sigterm_finishes_inflight_sse_before_lifespan_closes():
    import socket
    from contextlib import asynccontextmanager, nullcontext

    import httpx2
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from servicedesk_agent.api.container import AppContainer

    finish = asyncio.Event()
    closed = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        yield
        closed.set()

    app = FastAPI(lifespan=lifespan)
    app.state.container = AppContainer()

    @app.get("/")
    async def stream():
        async def body():
            yield "data: started\n\n"
            await finish.wait()
            assert not closed.is_set()
            yield "data: completed\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")

    server = DrainingServer(uvicorn.Config(app, timeout_graceful_shutdown=2))
    server.capture_signals = nullcontext
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
                async with httpx2.AsyncClient() as http, http.stream("GET", f"http://127.0.0.1:{sock.getsockname()[1]}/") as response:
                    chunks = response.aiter_lines()
                    assert await anext(chunks) == "data: started"
                    server.handle_exit(signal.SIGTERM, None)
                    assert app.state.container.draining
                    finish.set()
                    assert "data: completed" in [line async for line in chunks]
        finally:
            finish.set()
            server.should_exit = True
            await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_grant_release_wakes_other_local_worker_after_commit(tmp_path):
    from tests.test_workflow import ALICE

    store = WorkflowStore(f"sqlite:///{tmp_path}/grant.sqlite")
    worker = ExecutionWorker(store, build_registry(store))
    listener = ActionNotifier(store.url)
    listener.start()
    try:
        draft = create(store)
        await store.run(store.approvals.decide, "a", "approved", BOB, draft["payload_hash"])
        await worker.run_until_idle()
        assert store.directory.employee("1001", "default")["access_ledger"][0]["status"] == "active"
        listener.event.clear()
        await store.run(store.grants.release, "a", ALICE)
        await asyncio.wait_for(listener.event.wait(), 1)
        [revoke] = await store.actions.claim("expiry", 30)
        assert revoke["kind"] == "revoke" and revoke["approval_id"] == "a"
    finally:
        await listener.close()
        await store.aclose()


@pytest.mark.asyncio
async def test_listen_protocol_uses_dedicated_autocommit_connection(monkeypatch):
    import psycopg

    incoming, closed = asyncio.Event(), asyncio.Event()
    statements = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed.set()

        async def execute(self, query):
            statements.append(query)

        async def notifies(self):
            while True:
                await incoming.wait()
                incoming.clear()
                yield object()

    async def connect(dsn, *, autocommit):
        assert dsn == "postgresql://user@localhost/db" and autocommit
        return Connection()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    listener = ActionNotifier("postgresql+psycopg://user@localhost/db")
    listener.start()
    try:
        await asyncio.wait_for(listener.event.wait(), 1)
        assert statements == ["LISTEN servicedesk_agent_action_ready"]
        listener.event.clear()
        incoming.set()
        await asyncio.wait_for(listener.event.wait(), 1)
    finally:
        await listener.close()
    assert closed.is_set()
