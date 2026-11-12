"""意图识别离线评测：准确率、宏 F1 与分维度统计。

端到端应用评测在 evaluation/application.py 与 scripts/evaluate_application.py；多轮模拟用户评测在
evaluation/simulated.py。两者都按业务状态判定通过/失败。
"""
import asyncio
import json
import logging
import math
import os
import pathlib
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


from agents.intent.recognizer import IntentRecognizer

logger = logging.getLogger(__name__)

# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass

class IntentTestCase:
    message:          str
    expected_intent:  str
    context:          Optional[Dict[str, Any]] = None
    # v3 起的难例字段：标注确有歧义时的可接受意图、难度标签、留出划分、期望紧急度。
    id:               str = ""
    acceptable_intents: tuple = ()
    tags:             tuple = ()
    split:            str = ""
    expected_urgency: Optional[str] = None


# ── 意图识别评测 ──────────────────────────────────────────────────────────────

def _group_accuracy(details: List[Dict[str, Any]], keys) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[bool]] = {}
    for item in details:
        for key in keys(item):
            groups.setdefault(key, []).append(item["correct"])
    return {key: {"n": len(v), "correct": sum(v), "accuracy": round(sum(v) / len(v), 4)} for key, v in sorted(groups.items())}


def _urgency_accuracy(details: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    labeled = [item for item in details if item["expected_urgency"]]
    if not labeled:
        return None
    hit = sum(item["urgency"] == item["expected_urgency"] for item in labeled)
    return {"n": len(labeled), "correct": hit, "accuracy": round(hit / len(labeled), 4)}


def _latency_summary(values: List[float]) -> Dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {"p50": round(statistics.median(ordered), 4), "p95": round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 4), "max": round(ordered[-1], 4)}


class IntentEvaluator:
    """评测意图识别的准确率和 F1。"""

    def __init__(self, recognizer: IntentRecognizer):
        self._recognizer = recognizer

    async def evaluate(self, cases: List[IntentTestCase]) -> Dict[str, Any]:
        self._recognizer.reset_metrics()
        semaphore = asyncio.Semaphore(max(1, int(os.getenv("EVAL_INTENT_CONCURRENCY", "4"))))
        async def evaluate_case(case):
            async with semaphore:
                started = time.perf_counter()
                result = await self._recognizer.recognize(
                    case.message,
                    history=(case.context or {}).get("history"),
                    previous_intent_group=(case.context or {}).get("previous_intent_group"),
                )
                return case, result, time.perf_counter() - started
        evaluated = await asyncio.gather(*(evaluate_case(case) for case in cases))
        predictions, ground_truth = [], []
        case_details: List[Dict[str, Any]] = []

        for case, result, latency in evaluated:
            predicted = result.intent.value
            # 预测落在可接受意图内算对；F1 也把它记作命中，严格口径另算 strict_accuracy。
            accepted = predicted == case.expected_intent or predicted in case.acceptable_intents
            predictions.append(predicted)
            ground_truth.append(predicted if accepted else case.expected_intent)
            case_details.append({
                "id": case.id,
                "split": case.split,
                "tags": list(case.tags),
                "message": case.message,
                "expected": case.expected_intent,
                "acceptable": list(case.acceptable_intents),
                "predicted": predicted,
                "correct": accepted,
                "strict_correct": predicted == case.expected_intent,
                "confidence": result.confidence,
                "expected_urgency": case.expected_urgency,
                "urgency": getattr(result.urgency, "value", result.urgency),
                "latency_s": round(latency, 4),
                "missing_slots": list(result.missing_slots),
                "entities": {k: v for k, v in result.entities.items() if v},
                "reasoning": result.reasoning,
                "llm_used": result.llm_used,
                "recognition_source": result.recognition_source,
            })

        # 纯 Python 计算指标
        correct = sum(p == g for p, g in zip(predictions, ground_truth))
        accuracy = correct / len(predictions) if predictions else 0.0

        # 每类 F1
        labels = sorted(set(ground_truth + predictions))
        per_class: Dict[str, Dict[str, float]] = {}
        for label in labels:
            tp = sum(p == label and g == label for p, g in zip(predictions, ground_truth))
            fp = sum(p == label and g != label for p, g in zip(predictions, ground_truth))
            fn = sum(p != label and g == label for p, g in zip(predictions, ground_truth))
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec  = tp / (tp + fn) if (tp + fn) else 0.0
            f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            per_class[label] = {"precision": prec, "recall": rec, "f1": f1}

        macro_f1 = statistics.mean(v["f1"] for v in per_class.values()) if per_class else 0.0
        llm_cases = [item for item in case_details if item["llm_used"]]
        fallback_cases = [item for item in case_details if not item["llm_used"]]
        llm_correct = sum(item["correct"] for item in llm_cases)
        fallback_correct = sum(item["correct"] for item in fallback_cases)

        metrics = self._recognizer.metrics()
        return {
            "accuracy":   round(accuracy, 4),
            "macro_f1":   round(macro_f1, 4),
            "per_class":  per_class,
            "total":      len(cases),
            "correct":    correct,
            "cases":      case_details,
            "llm_calls": metrics.get("llm_calls", 0),
            "llm_success": metrics.get("llm_success", 0),
            "fallback_calls": metrics.get("fallback_calls", 0),
            "fallback_disabled": metrics.get("fallback_disabled", 0),
            "fallback_errors": metrics.get("fallback_errors", 0),
            "last_error": metrics.get("last_error"),
            "llm_only_accuracy": round(llm_correct / len(llm_cases), 4) if llm_cases else None,
            "llm_only_total": len(llm_cases),
            "fallback_accuracy": round(fallback_correct / len(fallback_cases), 4) if fallback_cases else None,
            "fallback_total": len(fallback_cases),
            "strict_accuracy": round(sum(item["strict_correct"] for item in case_details) / len(case_details), 4) if case_details else 0.0,
            "by_tag": _group_accuracy(case_details, lambda item: item["tags"]),
            "by_split": _group_accuracy(case_details, lambda item: [item["split"]] if item["split"] else []),
            "urgency_accuracy": _urgency_accuracy(case_details),
            "latency_s": _latency_summary([item["latency_s"] for item in case_details]),
            **{k: v for k, v in metrics.items() if k.startswith("jev_")},
        }


# ── 内置测试用例（开箱即用）──────────────────────────────────────────────────

DEFAULT_INTENT_CASES: List[IntentTestCase] = [
    IntentTestCase("VPN 连不上", "vpn_issue"),
    IntentTestCase("帮我重置密码", "password_reset"),
    IntentTestCase("申请 GitLab 权限", "permission_request"),
    IntentTestCase("Wi-Fi 一直断开", "wifi_issue"),
    IntentTestCase("创建 IT 工单", "ticket_create"),
    IntentTestCase("你好", "greeting"),
    IntentTestCase("我要转人工", "human_handoff"),
]


def load_intent_cases(path: Optional[str] = None) -> List[IntentTestCase]:
    """Load the versioned offline intent suite; keep a code fallback for deployments."""
    candidate = pathlib.Path(path) if path else pathlib.Path(__file__).parent / "datasets" / "intent_v3.json"
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        return [IntentTestCase(
            item["message"], item["expected_intent"], item.get("context"),
            id=item.get("id", ""),
            acceptable_intents=tuple(item.get("acceptable_intents", ())),
            tags=tuple(item.get("tags", ())),
            split=item.get("split", ""),
            expected_urgency=item.get("expected_urgency"),
        ) for item in payload["cases"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("无法读取意图评测集 %s：%s；使用内置最小集", candidate, type(exc).__name__)
        return list(DEFAULT_INTENT_CASES)
