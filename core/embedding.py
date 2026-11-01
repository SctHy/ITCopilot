"""文本向量：fastembed（ONNX，无 torch）本地加载中文模型，默认 BAAI/bge-small-zh-v1.5。

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

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
# bge-zh 对短查询建议加检索指令；文档侧不加。
QUERY_INSTRUCTIONS = {"BAAI/bge-small-zh-v1.5": "为这个句子生成表示以用于检索相关文章："}

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
        return self._embed(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([QUERY_INSTRUCTIONS.get(self.model_name, "") + text])[0]
