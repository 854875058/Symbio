"""本地降级 embedding —— 无外部 API Key 时的开箱即用向量化。

两级策略：
1. sentence-transformers（若已安装）：真实语义向量，跨表述的同义匹配可用；
   模型权重按模型名做进程级单例缓存，避免多实例重复加载（一次 3~6s、数百 MB）。
2. 字符 n-gram 哈希向量：零依赖、确定性、L2 归一化。对完全相同或轻微改写的
   文本能匹配，语义泛化能力弱于真实 embedding，但保证功能始终可用。

``SemanticCacheEngine`` 和 ``ProjectMemoryManager`` 共用本模块，避免各写一份降级逻辑。
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
from typing import Any

from symbio.utils.logger import get_logger

logger = get_logger("utils.embedding")

ST_MODEL_NAME = "all-MiniLM-L6-v2"
ST_EMBEDDING_DIM = 384
HASH_EMBEDDING_DIM = 256

_ST_MODEL_CACHE: dict[str, Any] = {}


def load_st_model_singleton(model_name: str = ST_MODEL_NAME):
    """加载并缓存 sentence-transformers 模型（进程级单例，按模型名区分）。

    首次调用真正 load；后续同名直接命中缓存。加载失败抛异常，由调用方降级处理。
    """
    cached = _ST_MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    _ST_MODEL_CACHE[model_name] = model
    return model


def sentence_transformers_available() -> bool:
    """探测 sentence-transformers 是否可用（只查 import 规格，不加载权重）。"""
    return importlib.util.find_spec("sentence_transformers") is not None


def hash_embedding(text: str, dim: int = HASH_EMBEDDING_DIM) -> list[float]:
    """字符 n-gram 哈希向量（零依赖、确定性、L2 归一化）。"""
    vec = [0.0] * dim
    cleaned = (text or "").lower().strip()
    if not cleaned:
        return vec

    tokens: list[str] = []
    for n in (1, 2, 3):
        for i in range(len(cleaned) - n + 1):
            tokens.append(cleaned[i : i + n])

    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[idx] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class LocalEmbedder:
    """惰性加载的本地 embedding 器。

    构造时只探测依赖可用性（不加载模型），首次 :meth:`embed` 才真正 load，
    因此构造开销可忽略。sentence-transformers 加载失败会永久降级到哈希向量。
    """

    def __init__(self, *, hash_dim: int = HASH_EMBEDDING_DIM, prefer_st: bool = True):
        self._hash_dim = hash_dim
        # None = 待加载；False = 不可用（走哈希）；其他 = 已加载的模型
        self._model: Any = None if (prefer_st and sentence_transformers_available()) else False
        self._dim = ST_EMBEDDING_DIM if self._model is None else hash_dim

    @property
    def dim(self) -> int:
        """当前生效的向量维度（建表前可用，无需加载模型）。"""
        return self._dim

    @property
    def backend(self) -> str:
        return "hash" if self._model is False else "sentence-transformers"

    def embed(self, text: str) -> list[float]:
        """生成向量，优先真实语义 embedding，失败则降级为哈希向量。"""
        if self._model is not False:
            try:
                if self._model is None:
                    self._model = load_st_model_singleton()
                    self._dim = int(self._model.get_sentence_embedding_dimension())
                vec = self._model.encode(text, normalize_embeddings=True)
                return [float(x) for x in vec]
            except Exception as e:
                logger.warning(f"sentence-transformers 不可用，降级为哈希向量: {e}")
                self._model = False
                self._dim = self._hash_dim

        return hash_embedding(text, self._dim)
