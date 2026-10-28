"""When does the user see progress? Live execution events vs. the old replay-after-completion SSE.

Runs the real application behind a real uvicorn server on a loopback TCP port (httpx's
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

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from evaluation.application import application, auth  # noqa: E402

REPORT = _ROOT / "evaluation/reports/stream_latency_v3.json"


def slow_model(latency_s: float):
    async def reply(messages, info):
        await asyncio.sleep(latency_s)
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": 0.9, "entities": {"error_code": ["VPN-691"]}})])
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("lookup_error_code", {"error_code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("VPN-691 表示认证失败：确认账号未锁定，检查系统时间和客户端版本。")])
    return FunctionModel(reply)


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 3)


async def measure(runs: int, latency: float):
    rows = []
    async with application(model=slow_model(latency)) as (_asgi, api):
        server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=auth(), timeout=60)
        for i in range(runs):
            started = time.monotonic()
            seen = {}
            # Unique text per run: the intent recognizer caches identical messages.
            payload = {"message": f"VPN 报错 VPN-691 怎么办（工位 {i}）", "conv_id": f"bench-{i}", "client_request_id": f"bench-{i}"}
            async with http.stream("POST", "/chat/stream", json=payload) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        seen.setdefault(line[7:], round(time.monotonic() - started, 3))
            first_progress = min(v for k, v in seen.items() if k != "status")
            rows.append({"first_progress_s": first_progress, "first_tool_s": seen.get("tool_started"),
                         "completed_s": seen["completed"], "event_times": seen})
        await http.aclose()
        server.should_exit = True
        await serving
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--model-latency", type=float, default=0.8, help="seconds per simulated model call (3 calls per turn)")
    args = parser.parse_args()
    rows = asyncio.run(measure(args.runs, args.model_latency))
    summary = {}
    for key in ("first_progress_s", "first_tool_s", "completed_s"):
        values = [r[key] for r in rows]
        summary[key] = {"p50": pct(values, 0.5), "p95": pct(values, 0.95), "mean": round(statistics.mean(values), 3)}
    report = {
        "schema_version": 1, "suite": "stream_latency_v3", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": f"real uvicorn over loopback TCP; simulated model latency {args.model_latency}s x 3 calls/turn; fakeredis; not production latency",
        "runs": args.runs, "summary": summary,
        "baseline_note": "Previous SSE replayed events after the turn completed: its first progress event ~= completed_s.",
        "rows": rows,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"runs": args.runs, "summary": summary, "first_run": rows[0]["event_times"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
