"""文本向量：fastembed（ONNX，无 torch）本地加载中英双语模型，默认 jinaai/jina-embeddings-v2-base-zh。

bge-small-zh-v1.5 在英文语料上向量召回比 BM25 还差（WixQA Expert R@5 0.29），jina-zh 在中文 rag-v2 上
更好、在英文 WixQA 上与英文模型 bge-small-en 持平（0.57），见 docs/evaluation-public.md。

英文的 all-MiniLM-L6-v2 在中文语料上向量召回很差（rag-v2：Recall@3 0.525），换模型只改
IT_COPILOT_EMBEDDING_MODEL；core/vector_store.py 会用新模型重新向量化旧数据。
"""
from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from typing import List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-base-zh"
# 各模型官方说明里的检索前缀：bge-zh 只给短查询加指令；e5、embeddinggemma 查询和文档两侧都要加。
_QWEN3_QUERY = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
QUERY_INSTRUCTIONS = {
    "BAAI/bge-small-zh-v1.5": "为这个句子生成表示以用于检索相关文章：",
    "intfloat/multilingual-e5-large": "query: ",
    "google/embeddinggemma-300m": "task: search result | query: ",
    "Qwen/Qwen3-Embedding-0.6B": _QWEN3_QUERY,
    "Qwen/Qwen3-Embedding-0.6B-Q": _QWEN3_QUERY,
}
DOCUMENT_PREFIXES = {
    "intfloat/multilingual-e5-large": "passage: ",
    "google/embeddinggemma-300m": "title: none | text: ",
}

_load_lock = threading.Lock()


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


class Embedder:
    """同一模型在进程内只加载一次；输出 L2 归一化向量，余弦相似度即点积。"""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or embedding_model_name()

    def _embed(self, texts: Sequence[str]) -> List[np.ndarray]:
        if not texts:
            return []
        # 首次加载要串行，避免并发下载同一模型；加载后的 ONNX 会话可被多线程共享。
        with _load_lock:
            model = _load_model(self.model_name)
        vectors = []
        for vector in model.embed(list(texts)):
            vector = np.asarray(vector, dtype=np.float32)
            vectors.append(vector / (np.linalg.norm(vector) or 1.0))
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> List[np.ndarray]:
        prefix = DOCUMENT_PREFIXES.get(self.model_name, "")
        return self._embed([prefix + text for text in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([QUERY_INSTRUCTIONS.get(self.model_name, "") + text])[0]
