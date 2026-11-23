"""Kill only the child worker at a synchronized external-write boundary."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.request import Request, urlopen

from .bench import MemoryTarget, approve, desired, database
from servicedesk_agent.workflow.adapters import AdapterRegistry
from servicedesk_agent.workflow.store import WorkflowStore
from servicedesk_agent.workflow.worker import ExecutionWorker


class HttpTarget(MemoryTarget):
    def __init__(self, url, gate):
        self.url = url
        self.gate = gate

    def request(self, method, path, data=None):
        req = Request(self.url + path, method=method, data=data)
        with urlopen(req, timeout=20) as response:
            return json.load(response)

    async def current_level(self, employee_id, system):
        return await asyncio.to_thread(self.request, "GET", "/permission")

    async def observe(self, action_id, action):
        level = await asyncio.to_thread(self.request, "GET", "/observe")
        return level == desired(action)

    async def apply(self, action_id, action):
        if self.gate == "before":
            await asyncio.to_thread(self.request, "GET", "/gate")
        role = desired(action)
        await asyncio.to_thread(
            self.request, "PUT" if role else "DELETE", "/permission", json.dumps(role).encode()
        )
        return {}


@contextmanager
def endpoint(scene):
    reached, release = Event(), Event()
    log = []
    levels = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(json.dumps(levels[-1] if levels else None).encode())
            except BrokenPipeError, ConnectionResetError:
                pass

        def do_GET(self):
            log.append(self.path)
            if self.path == "/gate":
                reached.set()
                release.wait(20)
            self.reply()

        def do_PUT(self):
            levels.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            log.append("write")
            if scene == "after":
                reached.set()
                release.wait(20)
            self.reply()

        do_DELETE = do_PUT

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", reached, release, log, levels
        finally:
            release.set()
            server.shutdown()
            thread.join()


async def child(db, address, gate):
    store = WorkflowStore(db, run_migrations=False)
    try:
        worker = ExecutionWorker(store, AdapterRegistry(HttpTarget(address, gate)), lease_s=1)
        await worker.run_once(limit=1)
    finally:
        await store.aclose()


@contextmanager
def process(db, address, gate):
    with subprocess.Popen([sys.executable, "-m", __spec__.name, db, address, gate]) as proc:
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


async def fault(args, root: Path):
    results = {}
    for scene in ("before", "after", "control"):
        scene_root = root / scene
        scene_root.mkdir()
        with database(scene_root, args.database_url) as db:
            store = WorkflowStore(db)
            try:
                approve(store, "grant", 0)
                with endpoint(scene) as (address, reached, release, log, levels):
                    start = time.monotonic()
                    with process(db, address, scene) as proc:
                        if scene != "control":
                            assert reached.wait(20), "worker missed injection barrier"
                            proc.kill()
                            assert proc.wait(timeout=5) == -9
                        else:
                            assert proc.wait(timeout=20) == 0
                    injected = time.monotonic()
                    cut = len(log)
                    release.set()
                    if scene != "control":
                        time.sleep(1.05)
                        with process(db, address, "recover") as proc:
                            assert proc.wait(timeout=20) == 0
                        assert log[cut] == "/observe", log
                    row = store.get("grant")["results"][0]
                    assert row["status"] == "succeeded" and levels == ["readonly"], (row, levels)
                    hit = bool(row.get("reconciled") or (row.get("result") or {}).get("reconciled"))
                    assert hit == (scene == "after"), row
                    results[scene] = dict(
                        injection_ms=round((injected - start) * 1000, 2),
                        recovery_ms=round((time.monotonic() - injected) * 1000, 2)
                        if scene != "control"
                        else None,
                        recovery_observe_hits=int(hit),
                        duplicate_writes=len(levels) - 1,
                    )
            finally:
                await store.aclose()
    return dict(database="postgresql" if args.database_url else "sqlite", rounds=len(results), scenes=results, convergence_pct=100)


if __name__ == "__main__":
    asyncio.run(child(*sys.argv[1:]))
