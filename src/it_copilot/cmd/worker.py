import asyncio
from it_copilot.infra.logging import configure_logging
import os
import signal
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from it_copilot.infra.retention import RetentionPolicy, RetentionSweeper
from it_copilot.workflow.worker import ExecutionWorker, configured_store, database_url


def management_app(worker):
    app = FastAPI(openapi_url=None)

    @app.get("/livez")
    async def live():
        return Response(status_code=200)

    @app.get("/readyz")
    async def ready():
        return Response(status_code=200 if worker.lifecycle.state == "running" and not worker.draining else 503)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def serve(worker, store):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def drain():
        worker.draining = True
        stop.set()

    async with AsyncExitStack() as resources:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, drain)
            resources.callback(loop.remove_signal_handler, sig)
        address = os.getenv("IT_COPILOT_WORKER_METRICS_ADDR")
        if address:
            host, port = address.rsplit(":", 1)
            server = uvicorn.Server(uvicorn.Config(management_app(worker), log_config=None, host=host, port=int(port), lifespan="off"))
            server.capture_signals = nullcontext
            task = asyncio.create_task(server.serve())

            async def close_server():
                server.should_exit = True
                await task

            resources.push_async_callback(close_server)
        retention = RetentionSweeper(store.engine, RetentionPolicy.from_env(), database=store.database,
                                     interval_s=float(os.getenv("IT_COPILOT_RETENTION_INTERVAL_S", "3600")))
        if os.getenv("IT_COPILOT_RETENTION_ENABLED", "true").lower() == "true":
            retention.start()
            resources.push_async_callback(retention.stop)
        worker.start()
        resources.push_async_callback(worker.stop)
        await stop.wait()


async def run_worker() -> None:
    configure_logging("worker")
    from it_copilot.infra import tenant_context

    tenant_context.enter(tenant_context.SYSTEM)  # the standalone worker spans tenants; run_once scopes each action
    store, registry = configured_store(database_url())

    async with AsyncExitStack() as resources:
        from it_copilot.infra.monitor.telemetry import setup_tracing, shutdown_tracing
        resources.callback(shutdown_tracing, setup_tracing())
        resources.push_async_callback(store.aclose)
        for adapter in registry.all():
            if hasattr(adapter, "close"):
                resources.push_async_callback(adapter.close)
        worker = ExecutionWorker(store, registry, poll_s=float(os.getenv("IT_COPILOT_WORKER_POLL_S", "5")))
        await serve(worker, store)


def main():
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
