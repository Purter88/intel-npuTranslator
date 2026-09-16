"""译文 LRU 缓存（SPEC.md · CLI 管道契约）。

**key 的构成是硬约束**：`(文本, 目标语言, 源语言)`，**不含设备**。
否则同一段文本在 NPU 与 CPU 上会命中不同条目、给出不同译文，重试/降级时结果就不确定了。

同理，术语表（glossary）一旦启用也必须进 key，否则不同术语表之间会串味。
v1 CLI 不暴露 `--glossary`（SPEC.md · CLI 管道契约），字段预留给 `service.py`。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import NamedTuple

from . import config as cfg

__all__ = ["CacheKey", "LRUCache", "TranslationCache"]


class CacheKey(NamedTuple):
    text: str
    target: str
    source: str = "auto"
    glossary: str = ""


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


class LRUCache:
    """线程安全的 LRU。

    用 `OrderedDict` + `move_to_end` 手写而不是 `functools.lru_cache`：
    后者没有"运行时可清空 / 可取统计 / 可跨实例共享"的能力，而 CLI 的
    `--verbose` 要报命中率。
    """

    def __init__(self, maxsize: int | None = None) -> None:
        self.maxsize = max(0, cfg.LRU_CACHE_SIZE if maxsize is None else maxsize)
        self._data: OrderedDict[CacheKey, str] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = CacheStats()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key: CacheKey) -> str | None:
        with self._lock:
            if key not in self._data:
                self.stats.misses += 1
                return None
            self._data.move_to_end(key)
            self.stats.hits += 1
            return self._data[key]

    def put(self, key: CacheKey, value: str) -> None:
        if self.maxsize == 0:
            return
        with self._lock:
            if key in self._data:
                self._data[key] = value
                self._data.move_to_end(key)
                return
            self._data[key] = value
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
                self.stats.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "hits": self.stats.hits,
                "misses": self.stats.misses,
                "evictions": self.stats.evictions,
                "hit_rate": round(self.stats.hit_rate, 3),
            }


class TranslationCache:
    """按 (text, target, source) 缓存译文的薄封装。"""

    def __init__(self, maxsize: int | None = None, enabled: bool = True) -> None:
        self.enabled = enabled
        self._cache = LRUCache(maxsize)

    def __len__(self) -> int:
        return len(self._cache)

    @staticmethod
    def make_key(text: str, target: str, source: str = "auto", glossary: str = "") -> CacheKey:
        return CacheKey(text=text, target=target, source=source, glossary=glossary)

    def get(self, text: str, target: str, source: str = "auto", glossary: str = "") -> str | None:
        if not self.enabled:
            return None
        return self._cache.get(self.make_key(text, target, source, glossary))

    def put(self, text: str, target: str, source: str, value: str, glossary: str = "") -> None:
        if not self.enabled:
            return
        self._cache.put(self.make_key(text, target, source, glossary), value)

    def clear(self) -> None:
        self._cache.clear()

    def snapshot(self) -> dict:
        return self._cache.snapshot()
