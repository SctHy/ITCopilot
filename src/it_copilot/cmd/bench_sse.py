"""Loopback HTTP clients against the deployed SSE routes with a deterministic producer."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import shutil
import socket
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from .bench import percentiles
from it_copilot.infra.events import InMemoryEventBus, RedisEventBus
from it_copilot.infra.identity import Principal


@asynccontextmanager
async def event_bus(args, root):
    if args.in_memory:
        yield InMemoryEventBus(max_streams=args.clients), "memory"
        return
    from redis.asyncio import Redis

    proc = None
    if args.redis_url:
        client = Redis.from_url(args.redis_url, decode_responses=True)
    else:
        binary = shutil.which("redis-server")
        if not binary:
            raise RuntimeError("redis-server missing: pass --redis-url or --in-memory")
        path = str(root / "r.sock")
        proc = subprocess.Popen(
            [
                binary,
                "--port",
                "0",
                "--unixsocket",
                path,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(root),
            ],
            stdout=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=path, decode_responses=True)
    try:
        if proc:
            async with asyncio.timeout(5):
                while not (root / "r.sock").exists():
                    if proc.poll() is not None:
                        raise RuntimeError("redis-server startup failed")
                    await asyncio.sleep(0.01)
        await client.ping()
        yield RedisEventBus(client, prefix="bench:" + uuid.uuid4().hex + ":"), "redis"
    finally:
        await client.aclose()
        if proc:
            proc.terminate()
            proc.wait(timeout=5)


async def sse(args, root):
    import httpx2 as httpx
    import uvicorn
    from fastapi import FastAPI
    from it_copilot.api.container import get_container
    from it_copilot.api.routes import chat

    count = args.events_per_stream
    if not 2 <= count <= 2000:
        raise ValueError("events-per-stream must be between 2 and 2000")
    executions = Counter()
    principal = Principal("bench:sse", "sse", "engineering")

    async def produce(state, payload, owner, rid, emit):
        executions[rid] += 1
        for seq in range(1, count):
            await emit("completed" if seq == count - 1 else "content_delta", {"seq": seq})
            await asyncio.sleep(0.001)

    async def client_run(http, i):
        payload = {"message": "bench", "client_request_id": str(i)}
        endpoint = "/chat/stream"

        async def post():
            return await http.send(http.build_request("POST", endpoint, json=payload), stream=True)

        first, replay = await asyncio.gather(post(), post())
        first.raise_for_status()
        replay.raise_for_status()
        await replay.aclose()
        conflict = await http.post(endpoint, json={**payload, "message": "changed"})
        assert conflict.status_code == 409
        ids, seqs = [], []

        async def read(response, stop):
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    ids.append(line[4:])
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    seqs.append(data.get("seq", 0))
                    if len(seqs) == stop:
                        break

        await read(first, count // 2)
        await first.aclose()
        start = time.monotonic()
        rid = chat.request_id(principal, str(i))
        async with http.stream(
            "GET", f"{endpoint}/{rid}", headers={"Last-Event-ID": ids[-1]}
        ) as resumed:
            resumed.raise_for_status()
            await read(resumed, None)
        assert seqs == list(range(count)) and len(set(ids)) == count
        return len(ids), (time.monotonic() - start) * 1000

    async with event_bus(args, root) as (bus, backend):
        state = SimpleNamespace(
            events=bus, orchestrator=True, rate_limiter=None, background_tasks=set()
        )
        app = FastAPI()
        app.include_router(chat.router)
        app.dependency_overrides[get_container] = lambda: state
        app.dependency_overrides[chat.current_principal] = lambda: principal
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        with socket.socket() as sock, patch.object(chat, "_run_streamed", produce):
            sock.bind(("127.0.0.1", 0))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(60):
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("HTTP server failed to start")
                        await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                        trust_env=False,
                        limits=httpx.Limits(max_connections=3 * args.clients),
                    ) as http:
                        rows = await asyncio.gather(
                            *(client_run(http, i) for i in range(args.clients))
                        )
            finally:
                server.should_exit = True
                await task
    assert list(executions.values()) == [1] * args.clients
    return dict(
        backend=backend,
        frames=sum(n for n, _ in rows),
        complete_pct=100,
        lost=0,
        duplicates=0,
        executions=sum(executions.values()),
        matching_requests=2 * args.clients,
        conflicts=args.clients,
        conflict_pct=100,
        resume_ms=percentiles([ms for _, ms in rows], 50, 99),
    )
