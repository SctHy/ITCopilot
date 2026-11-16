"""一次性清理长期记忆；文档变更经 VectorCollection.upsert 重新生成向量。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.redaction import redact, redact_text
from core.vector_store import VectorStore
from memory.conversation_memory import MemoryManager


def redact_existing_memory(store: VectorStore) -> dict:
    changed = {}
    for name in ("episodic", "user_profile"):
        collection = store.collection(name)
        rows = collection.get()
        count = 0
        for doc_id, document, meta in zip(rows["ids"], rows["documents"], rows["metadatas"]):
            safe_meta = redact(meta)
            safe_document = redact_text(document)
            if name == "user_profile":
                profile = MemoryManager._read_profile(json.loads(safe_document))
                safe_document = json.dumps(profile, ensure_ascii=False)
            if document != safe_document or meta != safe_meta:
                collection.upsert([doc_id], [safe_document], [safe_meta])
                count += 1
        changed[name] = count
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("IT_COPILOT_DATABASE_URL", "sqlite:///data/it_copilot.sqlite3"))
    args = parser.parse_args()
    store = VectorStore(args.database_url)
    try:
        print(json.dumps(redact_existing_memory(store), ensure_ascii=False))
    finally:
        store.dispose()


if __name__ == "__main__":
    main()
