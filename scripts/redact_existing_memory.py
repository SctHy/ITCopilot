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
from core.db import read, vector_documents
from sqlalchemy import select
from memory.conversation_memory import MemoryManager


def redact_existing_memory(store: VectorStore) -> dict:
    changed = {}
    for name in ("episodic", "user_profile"):
        collection = store.collection(name)
        rows = collection.get()
        with read(store.engine) as db:
            timestamps = dict(db.execute(select(vector_documents.c.id, vector_documents.c.updated_at)
                .where(vector_documents.c.collection == name)).all())
        count = 0
        for doc_id, document, meta in zip(rows["ids"], rows["documents"], rows["metadatas"]):
            safe_meta = redact(meta)
            safe_document = redact_text(document)
            if name == "user_profile":
                profile = MemoryManager._upgrade_profile(json.loads(safe_document), updated_at=timestamps[doc_id])
                safe_document = json.dumps(profile, ensure_ascii=False)
            elif meta.get("memory_type") == "episodic_turn":
                safe_meta.setdefault("seen", 1)
                safe_meta.setdefault("last_seen", meta["ts"])
                safe_meta.setdefault("source_request_id", meta["request_id"])
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
