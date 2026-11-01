"""一次性把旧版 Chroma 数据迁入应用数据库的向量表（core/vector_store.py），按当前 embedding 模型重新向量化。

迁移 knowledge_base / episodic / user_profile 三个集合；同名带模型后缀的集合（如 knowledge_base__bge-small-zh-v1.5）
优先于不带后缀的旧集合。chromadb 已不是项目依赖，运行前临时安装：

    uv pip install chromadb==1.5.9
    python scripts/migrate_chroma.py data/chroma                      # 写入 IT_COPILOT_DATABASE_URL 或本地 SQLite
    python scripts/migrate_chroma.py data/chroma --database-url postgresql+psycopg://...

重复执行是幂等的（按 id upsert）；Chroma 目录只读，不会被修改。
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.vector_store import VectorStore  # noqa: E402
from workflow.worker import database_url  # noqa: E402

COLLECTIONS = ("knowledge_base", "episodic", "user_profile")
BATCH = 256


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("chroma_path")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    try:
        import chromadb
    except ImportError:
        sys.exit("需要先安装 chromadb：uv pip install chromadb==1.5.9")

    client = chromadb.PersistentClient(path=args.chroma_path, settings=chromadb.Settings(anonymized_telemetry=False))
    names = {c.name for c in client.list_collections()}
    store = VectorStore(args.database_url or database_url())
    for base in COLLECTIONS:
        source = next((n for n in sorted(names, reverse=True) if n.startswith(base + "__")), base if base in names else None)
        if source is None:
            print(f"{base}: 无旧数据")
            continue
        chroma, target, offset = client.get_collection(source), store.collection(base), 0
        while True:
            rows = chroma.get(include=["documents", "metadatas"], limit=BATCH, offset=offset)
            if not rows["ids"]:
                break
            target.upsert(rows["ids"], [doc or "" for doc in rows["documents"]], rows["metadatas"])
            offset += len(rows["ids"])
        print(f"{base}: 从 {source} 迁移 {offset} 条，目标共 {target.count()} 条")
    store.dispose()


if __name__ == "__main__":
    main()
