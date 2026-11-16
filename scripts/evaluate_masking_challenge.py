"""Chinese helpdesk diagnostics, reported separately from the external benchmark."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2

from core.local_pii import detector
from core.model_masking import MaskingTransport, masking_scope
from evaluation.masking_benchmark import emitted_spans, score
from scripts.evaluate_masking_public import PREFIX, aggregate


async def main(output):
    path = ROOT / "evaluation/datasets/model_masking_challenge_zh.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    source = json.loads(path.read_text())
    rows = []
    for annotated in source["sensitive"] + source["benign"]:
        text, gold, previous = PREFIX, [], 0
        for m in re.finditer(r"⟦([A-Z]+):([^⟧]+)⟧", annotated):
            text += annotated[previous:m.start()]
            gold.append((len(text), len(text) + len(m[2]), m[1]))
            text += m[2]
            previous = m.end()
        rows.append((text + annotated[previous:], gold))
    assert len(rows) == len(set(text for text, _ in rows)), "Duplicate diagnostic text"
    report = {"dataset": str(path), "sha256": digest, "description": source["description"],
              "documents": len(rows), "model_threshold": .8, "modes": {}}
    captured = []

    def upstream(request):
        masked = json.loads(request.content)["messages"][0]["content"]
        captured.append(masked)
        return httpx2.Response(200, json={"choices": [{"message": {"tool_calls": [{
            "function": {"name": "inspect", "arguments": json.dumps({"text": masked})}}]}}]})

    model = detector()
    os.environ.update(IT_COPILOT_MASK_MODEL_THRESHOLD="0.8", IT_COPILOT_MASK_MODEL_TIMEOUT_S="10", IT_COPILOT_MASK_WORDS="[]")
    async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
        for mode in ("off", "on"):
            os.environ["IT_COPILOT_MASK_MODEL"] = mode
            records = []
            for i, (text, gold) in enumerate(rows):
                model.cache.clear()
                captured.clear()
                started = time.perf_counter()
                with masking_scope("challenge", str(i) + mode) as session:
                    response = await client.post("http://fixture/v1/chat/completions", json={"messages": [{"role": "user", "content": text}]})
                    predicted = emitted_spans(text, captured[0], session.values)
                    restored = json.loads(response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["text"] == text
                records.append({"id": i, "text": text, "gold_spans": gold, "predicted_spans": predicted,
                                "restoration_ok": restored, "blocked": False,
                                "latency_ms": (time.perf_counter() - started) * 1000, **score(text, gold, predicted)})
            report["modes"][mode] = {"overall": aggregate(records), "documents": records}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({m: r["overall"] for m, r in report["modes"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "evaluation/reports/model_masking/challenge_zh.json"))
    asyncio.run(main(parser.parse_args().output))
