"""When does the user see progress? Live execution events vs. the old replay-after-completion SSE.

Runs the real application behind a real uvicorn server on a loopback TCP port (httpx2's
in-process ASGITransport buffers whole responses and cannot observe streaming), offline
(fakeredis, temporary stores), with a model
substitute that sleeps a fixed time per call, so the result isolates the streaming
architecture from model variance. The old SSE emitted its first non-"started" event only
after the whole turn finished, so its first-event latency equals completion latency.

    python scripts/benchmark_stream.py --runs 20 --model-latency 0.8
    -> evaluation/reports/stream_latency_v3.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart  # noqa: E402

from api.main import app  # noqa: E402
from evaluation.application import application, auth  # noqa: E402
from evaluation.fake_model import scripted_model  # noqa: E402
from evaluation.reporting import percentile  # noqa: E402

REPORT = _ROOT / "evaluation/reports/stream_latency_v3.json"


def slow_model(latency_s: float):
    async def reply(messages, info):
        await asyncio.sleep(latency_s)
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": 0.9, "entities": {"error_code": ["VPN-691"]}})])
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("search_knowledge_base", {"query": "VPN-691"})])
        return ModelResponse(parts=[TextPart("VPN-691 表示认证失败：确认账号未锁定，检查系统时间和客户端版本。")])
    return scripted_model(reply)


async def measure(runs: int, latency: float, concurrency: int = 1, repeats: int = 1):
    rows = []
    async with application(model=slow_model(latency)) as (_asgi, _state):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        http = httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=auth(), timeout=60)
        slots = asyncio.Semaphore(concurrency)
        async def turn(i):
            started = time.monotonic()
            seen = {}
            result = None
            # Unique text per run: the intent recognizer caches identical messages.
            payload = {"message": f"VPN 报错 VPN-691 怎么办（工位 {i}）", "conv_id": f"bench-{i}", "client_request_id": f"bench-{i}"}
            async with http.stream("POST", "/chat/stream", json=payload) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        seen.setdefault(line[7:], round(time.monotonic() - started, 3))
                    elif line.startswith("data: ") and "completed" in seen:
                        result = json.loads(line[6:])
            first_progress = min(v for k, v in seen.items() if k != "status")
            return {"first_progress_s": first_progress, "first_tool_s": seen.get("tool_started"),
                    "first_token_s": seen.get("answer_delta"), "completed_s": seen["completed"],
                    "stage_ms": result["stage_ms"], "event_times": seen}
        async def bounded_turn(i):
            async with slots:
                return await turn(i)
        for repeat in range(repeats):
            rows.extend(await asyncio.gather(*(bounded_turn(repeat * runs + i) for i in range(runs))))
        await http.aclose()
        server.should_exit = True
        await serving
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=pathlib.Path, default=REPORT)
    parser.add_argument("--model-latency", type=float, default=0.8, help="seconds per simulated model call (3 calls per turn)")
    args = parser.parse_args()
    if min(args.runs, args.concurrency, args.repeats) < 1 or args.model_latency < 0:
        parser.error("runs, concurrency and repeats must be positive; model latency must be non-negative")
    rows = asyncio.run(measure(args.runs, args.model_latency, args.concurrency, args.repeats))
    summary = {}
    for key in ("first_progress_s", "first_tool_s", "first_token_s", "completed_s"):
        values = [r[key] for r in rows if r[key] is not None]
        summary[key] = {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
                        "p99": percentile(values, 0.99), "n": len(values),
                        "mean": round(statistics.mean(values), 3) if values else None}
    stage_p99 = {stage: percentile([row["stage_ms"][stage] for row in rows if stage in row["stage_ms"]], .99)
                 for stage in sorted({stage for row in rows for stage in row["stage_ms"]})}
    report = {
        "schema_version": 1, "suite": "stream_latency_v3", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": f"real uvicorn over loopback TCP; simulated model latency {args.model_latency}s x 3 calls/turn; fakeredis; not production latency",
        "runs": len(rows), "concurrency": args.concurrency, "repeats": args.repeats,
        "summary": summary, "stage_p99_ms": stage_p99,
        "baseline_note": "Previous SSE replayed events after the turn completed: its first progress event ~= completed_s.",
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"runs": args.runs, "summary": summary, "first_run": rows[0]["event_times"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
