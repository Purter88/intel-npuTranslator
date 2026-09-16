"""限流 / 队列 / 体积上限（SPEC.md · WebUI（nputweb））。

为什么这些都必须是**默认全开**的量：

1. **体积上限（413）**：NPU 的 KV cache 只有 768（SPEC.md · NPU 实现要点），
   一段超长文本能独占引擎几十分钟。没有上限 = 任何人都能用一个请求把服务按死
   —— 这不是"可能被攻击"，这是**一键 DoS**。
2. **单请求超时（504）**：同上，而且 NPU 被别的进程抢占时可能永远不返回（R9）。
3. **队列上限（503）**：排队是内存承诺。队列无限 = 内存无上限。
4. **单 IP 限流（429）**：挡的是"廉价重复请求"，也是挡住扫 token 的那一类尝试。

本模块**纯同步**，不依赖 asyncio / starlette —— 逻辑必须能在无网络的环境里单测。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "QueueGate",
    "RateDecision",
    "RateLimiter",
    "TooLarge",
    "check_body_size",
]

WINDOW_SECONDS = 60.0


class TooLarge(ValueError):
    """请求体超过上限。上层映射成 413。"""


# ---------------------------------------------------------------- 限流
@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int = 0
    retry_after: float = 0.0   # 秒，建议 `Retry-After` 头用


class RateLimiter:
    """滑动窗口限流（按 key 通常是来源 IP）。

    用 deque 存时间戳而不是"固定窗口计数"：固定窗口会在窗口切换的瞬间
    允许两倍流量（前窗口末尾 + 新窗口开头各打满），突刺照样打穿配额。

    :param per_minute: 每个窗口的请求上限（`0` = 不限）
    :param window_s: 窗口长度
    :param clock: 注入时钟，**单测要 FakeClock**，别用 sleep 耗时间
    """

    def __init__(
        self,
        per_minute: int = 30,
        window_s: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = max(0, int(per_minute))
        self.window_s = float(window_s)
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def check(self, key: str) -> RateDecision:
        """看看这个 key 现在还能不能请求（**不计数**，纯查询）。"""
        if not self.enabled:
            return RateDecision(allowed=True, remaining=0)
        now = self._clock()
        with self._lock:
            self._evict(key, now)
            hits = self._hits.get(key, deque())
            used = len(hits)
            if used >= self.per_minute:
                # 队首那个时间戳过期后就有名额了
                retry = max(0.0, self.window_s - (now - hits[0])) if hits else 0.0
                return RateDecision(allowed=False, remaining=0, retry_after=round(retry, 3))
            return RateDecision(allowed=True, remaining=self.per_minute - used)

    def hit(self, key: str) -> RateDecision:
        """查询 + 计数（真要放行时调这个，别调 check）。"""
        if not self.enabled:
            return RateDecision(allowed=True, remaining=0)
        now = self._clock()
        with self._lock:
            self._evict(key, now)
            hits = self._hits.setdefault(key, deque())
            if len(hits) >= self.per_minute:
                retry = max(0.0, self.window_s - (now - hits[0])) if hits else 0.0
                return RateDecision(allowed=False, remaining=0, retry_after=round(retry, 3))
            hits.append(now)
            return RateDecision(allowed=True, remaining=self.per_minute - len(hits))

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _evict(self, key: str, now: float) -> None:
        """丢掉窗口外的旧时间戳（调用方必须持锁）。"""
        hits = self._hits.get(key)
        if not hits:
            return
        cutoff = now - self.window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)


# ---------------------------------------------------------------- 队列
class QueueGate:
    """准入闸门：限制"同时在系统里"的请求数，超出给 503。

    它**不负责串行化**（那是 asyncio.Lock 的事）：这里只回答"还能不能进来"，
    并保持一个可读的队列深度给 `/api/health` 与前端的「排队中，第 N 位」。

    :param max_pending: 同时在系统里的请求上限（含正在执行的那个）
    """

    def __init__(self, max_pending: int = 8) -> None:
        self.max_pending = max(1, int(max_pending))
        self._count = 0
        self._lock = threading.Lock()

    @property
    def depth(self) -> int:
        """队列深度（含正在执行的那个请求）。"""
        with self._lock:
            return self._count

    @property
    def waiting(self) -> int:
        """纯排队的人数（深度去掉正在执行的那个）。"""
        with self._lock:
            return max(0, self._count - 1)

    def try_enter(self) -> int | None:
        """尝试进场。成功返回**队列位置**（1 = 正在执行），被拒返回 None。"""
        with self._lock:
            if self._count >= self.max_pending:
                return None
            self._count += 1
            return self._count

    def leave(self) -> None:
        """释放。`try_enter` 成功之后**必须**在 finally 里调（漏一次就永久堵住）。"""
        with self._lock:
            self._count = max(0, self._count - 1)


# ---------------------------------------------------------------- 体积
def check_body_size(content_length: str | int | None, limit: int) -> int:
    """先看 `Content-Length`：**超限立刻拒绝，不要去读 body**。

    Starlette 没有内置 body 上限，不查这一步就等于允许对方把 10 GB 灌进来
    再由 Python 慢慢读满内存。

    :raises TooLarge: 超过上限（412？不 —— 上层映射成 413）
    :return: 已声明的长度
    """
    try:
        declared = int(content_length or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise TooLarge(f"请求体过大：{declared} 字节（上限 {limit}）")
    return declared
