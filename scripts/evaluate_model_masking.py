"""工程回归：助手编写合成集、HTTP payload 截获、工具恢复及本机开销。

这些开发样例没有独立人工复核，不是冻结盲测，不能代表生产识别质量。
规模化公开集评测见 evaluate_masking_public.py。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import platform
import os
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2
from core.model_masking import MaskingSession, MaskingTransport, _TextBuffer, mask_request, prepare_request

DATASET = Path(__file__).resolve().parents[1] / "evaluation/datasets/model_masking.json"


def stats(values):
    ordered = sorted(values)
    return {"p50_ms": round(statistics.median(ordered), 4), "p95_ms": round(ordered[int(.95 * (len(ordered) - 1))], 4), "p99_ms": round(ordered[int(.99 * (len(ordered) - 1))], 4)}


def contains(value, secret):
    if isinstance(value, dict):
        return any(contains(x, secret) for x in value.values())
    if isinstance(value, list):
        return any(contains(x, secret) for x in value)
    if isinstance(value, str):
        if secret in value:
            return True
        try:
            parsed = json.loads(value)
        except ValueError:
            return False
        return contains(parsed, secret) if isinstance(parsed, (dict, list)) else False
    return False


async def evaluate(repetitions, mode="off"):
    os.environ["IT_COPILOT_MASK_MODEL"] = mode
    fixtures = json.loads(DATASET.read_text())
    outcomes, clean = [], []
    # 每条都是独立出站请求，不共享已知值，以免前面的样本帮助后面的识别。
    for case in fixtures["sensitive"]:
        captured = []
        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx2.Response(200, json={"choices": []})
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            await client.post("http://fixture/v1/chat/completions", json={"model": "fixture", "messages": [{"role": "user", "content": case["text"]}]})
        outcomes.append({"kind": case["kind"], "protected": not contains(captured[0], case["value"]), "expected_protected": case.get("expected_protected", True)})
    for text in fixtures["benign"]:
        session = MaskingSession()
        body = {"messages": [{"role": "user", "content": text}]}
        await prepare_request(body, session)
        masked = mask_request(body, session)
        clean.append(masked["messages"][0]["content"] == text)
    restoration, splits = 0, 0
    for case in fixtures["sensitive"]:
        if mode == "off" and not case.get("expected_protected", True):
            continue
        session = MaskingSession()
        body = {"messages": [{"role": "user", "content": case["text"]}]}
        await prepare_request(body, session)
        mask_request(body, session)
        token = session.reverse.get(case["value"])
        if token is None:
            continue
        restored = json.loads(session.restore(json.dumps({"value": token}), tool=True))
        restoration += restored["value"] == case["value"]
        for cut in range(len(token) + 1):
            buffer = _TextBuffer(session, tool=True)
            value = buffer.feed(token[:cut]) + buffer.feed(token[cut:])
            assert json.loads('"' + value + '"') == case["value"]
            splits += 1
    base_text = "VPN 691 失败；请检查设备 DEV-1001 的证书版本。" * 80
    payload = {"model": "fixture", "messages": [{"role": "user", "content": base_text + case["text"]} for case in fixtures["sensitive"][:8]]}
    # 配对基线：同一 JSON payload 的复制/编码；处理组加上新建映射与脱敏。无网络/模型推理。
    baseline, masked = [], []
    for i in range(repetitions + 20):
        started = time.perf_counter()
        json.dumps(json.loads(json.dumps(payload)))
        plain_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        session = MaskingSession()
        await prepare_request(payload, session)
        json.dumps(mask_request(payload, session))
        mask_ms = (time.perf_counter() - started) * 1000
        if i >= 20:
            baseline.append(plain_ms)
            masked.append(mask_ms)
    supported = sum(o["expected_protected"] for o in outcomes)
    expected_restoration = supported if mode == "off" else len(outcomes)
    report = {
        "mode": mode,
        "scope": "offline synthetic fixtures; MockTransport intercepts real HTTP payload; on uses real local AstrLink; no cloud generation or production efficacy claim",
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "dataset": str(DATASET),
        "sensitive": {"protected": sum(o["protected"] for o in outcomes), "total": len(outcomes), "supported_total": supported,
            "supported_protected": sum(o["protected"] for o in outcomes if o["expected_protected"]), "cases": outcomes},
        "benign": {"unchanged": sum(clean), "total": len(clean), "false_positive_indices": [i for i, good in enumerate(clean) if not good]},
        "restoration": {"correct": restoration, "total": expected_restoration, "stream_split_positions_passed": splits},
        "performance": {"model_cache": "warm identical text" if mode != "off" else "not applicable", "payload_utf8_bytes": len(json.dumps(payload, ensure_ascii=False).encode()), "repetitions": repetitions, "warmup": 20,
            "baseline_serialization": stats(baseline), "with_masking": stats(masked), "paired_added_latency": stats([b - a for a, b in zip(baseline, masked)])},
    }
    report["passed"] = all(o["protected"] for o in outcomes if o["expected_protected"]) and all(clean) and restoration == expected_restoration
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="evaluation/reports/model_masking/metrics.json")
    parser.add_argument("--repetitions", type=int, default=300)
    parser.add_argument("--mode", choices=["off", "on", "auto"], default="on")
    parser.add_argument("--compare", action="store_true", help="同一进程顺序运行规则及真实本地模型，不改变样本")
    args = parser.parse_args()
    if args.repetitions < 20:
        parser.error("--repetitions must be at least 20")
    if args.compare:
        from core.local_pii import REVISION, FILES, detector, settings
        start = time.perf_counter()
        model = detector()
        load_ms = (time.perf_counter() - start) * 1000
        report = {"rules": asyncio.run(evaluate(args.repetitions, "off")),
                  "astrlink": asyncio.run(evaluate(args.repetitions, "on"))}
        report["model"] = {"revision": REVISION, "sha256": {k: v[1] for k, v in FILES.items()},
            "provider": model.session.get_providers(), "threshold": settings()[1], "load_ms": round(load_ms, 3)}
        # 与重复请求缓存命中分开：每次清空缓存，测量相同长输入首次识别的开销。
        base = "VPN 691 失败；请检查设备 DEV-1001 的证书版本。" * 80
        texts = [base + c["text"] for c in json.loads(DATASET.read_text())["sensitive"][:8]]
        misses = []
        for _ in range(30):
            started = time.perf_counter()
            for text in texts:
                model.detect(text, deadline=time.monotonic() + 30, use_cache=False)
            misses.append((time.perf_counter() - started) * 1000)
        report["model"]["uncached_detection_30_repeats"] = stats(misses)
        report["passed"] = report["rules"]["passed"] and report["astrlink"]["passed"]
    else:
        report = asyncio.run(evaluate(args.repetitions, args.mode))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"sensitive", "environment"}}, ensure_ascii=False, indent=2))
    if "sensitive" in report:
        print(json.dumps({"protected": report["sensitive"]["protected"], "total": report["sensitive"]["total"]}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
