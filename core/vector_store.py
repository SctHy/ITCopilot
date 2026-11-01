"""向量存储：与应用数据库同库（PostgreSQL + pgvector；本地与测试用 SQLite），表结构见 workflow/db.py。

接口是 upsert / get / query / delete / count 几个集合操作，知识库与记忆模块不关心后端：
  - PostgreSQL：相似度在库内计算，``embedding <=> :q`` 为余弦距离；
  - SQLite：按过滤条件取出候选向量，用 numpy 计算余弦距离。
演示规模（千级片段）两边都是精确检索；数据量上来后在 PostgreSQL 上加 HNSW 索引。

过滤条件只支持字符串等值、``{"$in": [...]}`` 和 ``{"$and": [...]}``，在 SQL 里按 JSON 字段求值。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
from sqlalchemy import Float, and_, bindparam, delete, func, select, update
from sqlalchemy.dialects import postgresql, sqlite

from core.embedding import Embedder
from workflow.db import Embedding, make_engine, migrate, vector_documents as T

logger = logging.getLogger(__name__)

REEMBED_BATCH = 256


def _conditions(where: Optional[Dict[str, Any]]) -> list:
    if not where:
        return []
    if set(where) == {"$and"}:
        return [c for part in where["$and"] for c in _conditions(part)]
    out = []
    for key, value in where.items():
        field = T.c.meta[key].as_string()
        if isinstance(value, dict) and set(value) == {"$in"} and all(isinstance(v, str) for v in value["$in"]):
            out.append(field.in_(list(value["$in"])))
        elif isinstance(value, str):
            out.append(field == value)
        else:
            raise TypeError(f"不支持的过滤条件：{key}={value!r}")
    return out


class VectorStore:
    """一个数据库连接上的若干集合；构造时执行迁移，保证表存在。"""

    def __init__(self, url: str, embedder: Optional[Embedder] = None):
        migrate(url)
        self.engine = make_engine(url)
        self.embedder = embedder or Embedder()

    def collection(self, name: str) -> "VectorCollection":
        collection = VectorCollection(self, name)
        collection.reembed_stale()
        return collection

    def dispose(self) -> None:
        self.engine.dispose()


class VectorCollection:
    def __init__(self, store: VectorStore, name: str):
        self.name = name
        self._engine = store.engine
        self._embedder = store.embedder
        self._postgres = self._engine.dialect.name == "postgresql"

    @property
    def model(self) -> str:
        return self._embedder.model_name

    def count(self) -> int:
        with self._engine.connect() as db:
            return db.scalar(select(func.count()).select_from(T).where(T.c.collection == self.name)) or 0

    def upsert(self, ids: Sequence[str], documents: Sequence[str], metadatas: Optional[Sequence[Optional[Dict[str, Any]]]] = None) -> None:
        if not ids:
            return
        if len(set(ids)) != len(ids):
            raise ValueError("同一批写入的 id 不能重复")
        metadatas = metadatas or [None] * len(ids)
        vectors = self._embedder.embed_documents([str(doc) for doc in documents])
        now = time.time()
        rows = [{"collection": self.name, "id": i, "document": str(doc), "meta": dict(meta or {}), "embedding": vec, "model": self.model, "updated_at": now}
                for i, doc, meta, vec in zip(ids, documents, metadatas, vectors)]
        insert = (postgresql if self._postgres else sqlite).insert(T)
        statement = insert.on_conflict_do_update(
            index_elements=[T.c.collection, T.c.id],
            set_={column: insert.excluded[column] for column in ("document", "meta", "embedding", "model", "updated_at")},
        )
        with self._engine.begin() as db:
            db.execute(statement, rows)

    add = upsert

    def delete(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if ids:
            with self._engine.begin() as db:
                db.execute(delete(T).where(T.c.collection == self.name, T.c.id.in_(ids)))

    def get(self, ids: Optional[Sequence[str]] = None, where: Optional[Dict[str, Any]] = None,
            limit: Optional[int] = None, offset: Optional[int] = None) -> Dict[str, List[Any]]:
        statement = select(T.c.id, T.c.document, T.c.meta).where(T.c.collection == self.name, *_conditions(where)).order_by(T.c.id)
        if ids is not None:
            statement = statement.where(T.c.id.in_(list(ids)))
        if limit is not None:
            statement = statement.limit(limit)
        if offset:
            statement = statement.offset(offset)
        with self._engine.connect() as db:
            rows = db.execute(statement).all()
        return {"ids": [r.id for r in rows], "documents": [r.document for r in rows], "metadatas": [r.meta for r in rows]}

    def query(self, query_texts: Sequence[str], n_results: int = 10, where: Optional[Dict[str, Any]] = None) -> Dict[str, List[List[Any]]]:
        """每条查询返回最近的 n_results 条；distances 为余弦距离（0 最相似）。"""
        out: Dict[str, List[List[Any]]] = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        conditions = [T.c.collection == self.name, T.c.model == self.model, *_conditions(where)]
        for text in query_texts:
            vector = self._embedder.embed_query(str(text))
            hits = self._query_postgres(vector, n_results, conditions) if self._postgres else self._query_sqlite(vector, n_results, conditions)
            for key, values in zip(("ids", "documents", "metadatas", "distances"), zip(*hits) if hits else ([], [], [], [])):
                out[key].append(list(values))
        return out

    def _query_postgres(self, vector: np.ndarray, n: int, conditions: list) -> list:
        distance = T.c.embedding.op("<=>", return_type=Float)(bindparam("q", vector, type_=Embedding()))
        statement = select(T.c.id, T.c.document, T.c.meta, distance.label("distance")).where(and_(*conditions)).order_by(distance).limit(n)
        with self._engine.connect() as db:
            return [(r.id, r.document, r.meta, float(r.distance)) for r in db.execute(statement)]

    def _query_sqlite(self, vector: np.ndarray, n: int, conditions: list) -> list:
        with self._engine.connect() as db:
            rows = db.execute(select(T.c.id, T.c.document, T.c.meta, T.c.embedding).where(and_(*conditions))).all()
        if not rows:
            return []
        # 写入时已归一化，余弦相似度即点积。
        distances = 1.0 - np.stack([r.embedding for r in rows]) @ vector
        order = np.argsort(distances, kind="stable")[:n]
        return [(rows[i].id, rows[i].document, rows[i].meta, float(distances[i])) for i in order]

    def reembed_stale(self) -> int:
        """把其他模型生成的向量按当前模型重算；换模型后第一次打开集合时执行。"""
        stale = select(T.c.id, T.c.document).where(T.c.collection == self.name, T.c.model != self.model).limit(REEMBED_BATCH)
        total = 0
        while True:
            with self._engine.connect() as db:
                rows = db.execute(stale).all()
            if not rows:
                break
            vectors = self._embedder.embed_documents([r.document for r in rows])
            with self._engine.begin() as db:
                for row, vec in zip(rows, vectors):
                    db.execute(update(T).where(T.c.collection == self.name, T.c.id == row.id).values(embedding=vec, model=self.model))
            total += len(rows)
        if total:
            logger.info("集合 %s 已按 %s 重新向量化 %d 条", self.name, self.model, total)
        return total
