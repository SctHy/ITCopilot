"""Rank recalled episodes by relevance, importance and recency within a token budget."""
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def rank_memory_rows(results: Dict[str, Any], max_tokens: int, entities: Optional[Dict[str, Any]] = None) -> List[str]:
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []
    distances = results.get("distances") or []
    docs = docs[0] if docs and isinstance(docs[0], list) else docs
    metas = metas[0] if metas and isinstance(metas[0], list) else metas
    distances = distances[0] if distances and isinstance(distances[0], list) else distances
    # 余弦距离门槛：jina-v2-base-zh 上同主题问答对 0.19–0.62、不同主题 ≥0.62（9 条查询的小样本校准）。
    # 不设门槛时，老用户每轮都会带上 3 条与当前问题无关的旧对话，而且落在不命中缓存的本轮消息里。
    max_distance = float(os.getenv("SERVICEDESK_AGENT_MEMORY_MAX_DISTANCE") or 0.6)
    rows = []
    for idx, doc in enumerate(docs):
        if not isinstance(doc, str) or not doc.strip():
            continue
        distance = float(distances[idx]) if idx < len(distances) and distances[idx] is not None else None
        if distance is not None and distance > max_distance:
            continue
        meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
        requested_keys = {str(key) for key, value in (entities or {}).items() if value}
        indexed_keys = {key for key in str(meta.get("entity_keys", "")).split(",") if key}
        if requested_keys and indexed_keys and not requested_keys.intersection(indexed_keys):
            continue
        try:
            importance = float(meta.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        try:
            seen = int(meta.get("seen", 0))
            importance *= 1 + 0.1 * min(seen, 5)
            updated = datetime.fromisoformat(max(str(meta.get("ts", "")), str(meta.get("last_seen", ""))).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds() / 86400)
            importance *= 0.5 ** (age_days / 30.0)
        except (TypeError, ValueError):
            pass
        # 相关度为主、重要度与新近度为辅；原先只按重要度排序，相似度顺序被整个丢掉。
        relevance = 1.0 - distance if distance is not None else 0.5
        rows.append((relevance * (0.5 + importance), doc))
    rows.sort(key=lambda item: item[0], reverse=True)
    return budget_texts([doc for _, doc in rows], max_tokens)


def budget_texts(values: List[str], max_tokens: int) -> List[str]:
    budget = max(128, int(max_tokens))
    used = 0
    output = []
    for value in values:
        cost = max(1, len(value) // 2)
        if output and used + cost > budget:
            break
        output.append(value)
        used += cost
    return output
