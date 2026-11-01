"""Chroma 集合共用的文本向量函数：fastembed（ONNX，无 torch）加载中文模型，默认 BAAI/bge-small-zh-v1.5。

Chroma 自带的 all-MiniLM-L6-v2 只在英文语料上训练，中文查询的向量召回很差（rag-v2：recall@3 0.525），
换模型只改 IT_COPILOT_EMBEDDING_MODEL。集合名带模型后缀，不同模型的向量不会混在同一个集合里；
新集合为空时从旧集合复制文本与元数据并用新模型重新向量化。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from functools import lru_cache
from typing import Any, Dict, List, Optional

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings, Space
from chromadb.utils.embedding_functions import register_embedding_function

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
# bge-zh 对短查询建议加检索指令；文档侧不加。
QUERY_INSTRUCTIONS = {"BAAI/bge-small-zh-v1.5": "为这个句子生成表示以用于检索相关文章："}
COPY_BATCH = 256


def embedding_model_name() -> str:
    return os.getenv("IT_COPILOT_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def _cache_dir() -> str:
    # fastembed 默认缓存在系统临时目录，重启后可能被清理；固定到用户缓存目录（镜像内由 FASTEMBED_CACHE_PATH 指定）。
    return os.getenv("FASTEMBED_CACHE_PATH") or os.path.join(os.path.expanduser("~"), ".cache", "fastembed")


@lru_cache(maxsize=4)
def _load_model(model_name: str):
    from fastembed import TextEmbedding
    logger.info("加载 embedding 模型 %s", model_name)
    return TextEmbedding(model_name, cache_dir=_cache_dir())


@register_embedding_function
class FastEmbedFunction(EmbeddingFunction[Documents]):
    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or embedding_model_name()
        self._lock = threading.Lock()

    def _embed(self, texts: List[str]) -> Embeddings:
        # ONNX 会话可被多线程共享，但首次加载要串行，避免并发下载同一模型。
        with self._lock:
            model = _load_model(self.model_name)
        return [vector for vector in model.embed(texts)]

    def __call__(self, input: Documents) -> Embeddings:
        return self._embed(list(input))

    def embed_query(self, input: Documents) -> Embeddings:
        prefix = QUERY_INSTRUCTIONS.get(self.model_name, "")
        return self._embed([prefix + text for text in input])

    @staticmethod
    def name() -> str:
        return "it_copilot_fastembed"

    def default_space(self) -> Space:
        return "cosine"

    def supported_spaces(self) -> List[Space]:
        return ["cosine"]

    def get_config(self) -> Dict[str, Any]:
        return {"model_name": self.model_name}

    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "FastEmbedFunction":
        return FastEmbedFunction(config.get("model_name"))


def collection_name(base: str, model_name: Optional[str] = None) -> str:
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", (model_name or embedding_model_name()).split("/")[-1]).strip("-.").lower()
    return f"{base}__{slug}"


def open_collection(client, base: str, metadata: Optional[Dict[str, Any]] = None):
    """按当前模型打开集合；新集合为空且存在旧集合时，迁移旧集合的文本与元数据（旧集合保留不删）。"""
    function = FastEmbedFunction()
    collection = client.get_or_create_collection(collection_name(base, function.model_name), embedding_function=function, metadata=metadata)
    if collection.count() == 0:
        legacy = next((c for c in client.list_collections() if c.name == base), None)
        if legacy is not None and legacy.count():
            _copy(client.get_collection(base), collection)
    return collection


def _copy(source, target) -> None:
    total, offset = source.count(), 0
    while offset < total:
        rows = source.get(include=["documents", "metadatas"], limit=COPY_BATCH, offset=offset)
        if not rows["ids"]:
            break
        documents = [doc or "" for doc in rows["documents"]]
        target.upsert(ids=rows["ids"], documents=documents, metadatas=rows["metadatas"])
        offset += len(rows["ids"])
    logger.info("集合 %s 已按新 embedding 模型重建到 %s（%d 条）", source.name, target.name, offset)
