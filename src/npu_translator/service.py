"""`nputserve` 的适配层：请求校验 → 调 `orchestrate.Translator` → 组装响应（SPEC.md · WebUI（nputweb））。

## 这一层**不做什么**（比"做什么"更重要）

它是薄薄一层的适配层，**不是第二份编排**。以下事情一律不在这里发生：

    分段 · 查缓存 · 批内去重 · 池翻译 · 回填 · 打包行数校验 · 按行拼接

全部在 `orchestrate.Translator` 里，且每一条都是踩过坑的（不去重 → 重复段被翻 N 遍；
不校验 → hard 模式行数会掉）。在这里重写一遍 = 造第二份必然漂移的实现。

同理，**本文件不 import fastapi**：它是纯逻辑，能脱离网络栈单测。
（`web/routes.py` 在模块级 import fastapi，所以要复用它的脱敏函数只能**延迟** import，
见本文件底部的 `_safe_error_message`。）

## 三个"为什么这样设计"

1. **流式不走 executor。**
   `Translator.stream()` 是**同步**生成器，内部还起了一个线程阻塞在 `q.get()` 上。
   executor 恒为 `max_workers=1`（NPU 单流，多开只会抢锁 + OOM），
   一个流式请求进去就占死整个池，`/v1/translate` 全部饿死。
   所以每个流起一个**专用 daemon 线程**，用 `loop.call_soon_threadsafe` 把 token 推进
   `asyncio.Queue`，由本文件的 pump 协程消费。

2. **`InferenceLane` 是准入凭证，不是物理锁。**
   物理串行由 engine 内部的 `self._lock` 保证（那层不能动，也不够）。
   lane 的作用是把"NPU 单流"这件事从 engine 内部的隐式状态提升为服务层的显式对象，
   于是"等"变得**可超时**（503 `lane_busy` + `Retry-After`，而不是让调用方挂到 504）、
   **可观测**（`/v1/health` 的 `lane` / `streaming`）、**可释放**（超时立刻放）。
   ⚠️ 关键认知：释放 lane 之后孤儿线程仍持有 engine 的物理锁，下一个请求会自然地在
   那把真锁上排队，不会互相破坏。反过来，如果 lane 要等孤儿跑完才释放，
   那就是把 Q2（504 孤儿）换个地方再犯一次。

3. **Q2 孤儿：超时后"结果不返回给调用方（可能仍写入缓存）"。**
   `engine.generate()` 同步阻塞在原生调用里，`asyncio` 取消不了（与活跃风险 R9 同构）。
   超时只能"放弃等待"，不能"取消推理"。所以：
   - 超时 → `ticket.abandon()`（孤儿 +1）→ 立刻释放 lane → 中间件释放队列位 → 504
   - 孤儿线程跑完 → `_guarded()` 的 finally 里 `ticket.settle()` **自己把账记平**
     （孤儿 −1；吞掉异常，避免 concurrent.futures 的"异常从未被取回"噪音）
   措辞必须说"可能仍写入缓存"：`Translator.translate()` 内部会写缓存，适配层拦不住
   —— 这其实是好事（同样的输入下次直接命中，孤儿没白跑），但不能写成"丢弃结果"，
   那会让人以为缓存里也没有。

## 与 nputweb 的关系

`nputserve` 是**独立进程**（独立端口 / 独立 token / 独立限流桶），只挂 `/v1/*`，
不挂进 `nputweb`。中间件栈、鉴权、限流、TLS 全部复用 `web/` 子包；
本文件只补服务层特有的三样东西：**推理通道（lane）**、**流槽**、**孤儿记账**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from .languages import LANGUAGES, common, others
from .orchestrate import OrchestrateConfig, Outcome, Translator
from .segment import NEWLINE_MODES, SOFT
from .web.app import SecurityConfig
from .web.limits import QueueGate

__all__ = [
    "DEFAULT_MAX_STREAM_CHARS",
    "ApiError",
    "InferenceLane",
    "JobTicket",
    "LaneTicket",
    "ServiceConfig",
    "ServiceRuntime",
    "StreamSession",
    "StreamSlots",
    "TranslateRequest",
    "TranslatorService",
]

logger = logging.getLogger("nputserve.service")

#: 流式输入字符上限的默认值。**实测得出，不是拍脑袋**（依据见 `ServiceConfig` 的注释）。
DEFAULT_MAX_STREAM_CHARS = 160


# ---------------------------------------------------------------- 错误
@dataclass
class ApiError(Exception):
    """统一的业务错误。`server.py` 把它翻译成 HTTP 状态码 + 统一错误体。

    用异常而不是返回码：校验散落在多个函数里，返回码要一路冒泡到路由，
    中间任何一层忘了检查就变成 500。异常至少是"忘了接就红"。

    ⚠️ `message` **绝不能**带 traceback 或本机路径（会泄漏绝对路径与用户名，
    见 SPEC.md · WebUI（nputweb））。
    """

    status: int = 500
    code: str = "internal_error"
    message: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # dataclass 的 __init__ 不会填 Exception.args，补上让 str(exc) / 日志有东西看
        self.args = (self.message,)

    def to_payload(self) -> dict:
        """统一错误体 `{"error": {"code": ..., "message": ...}}`。"""
        return {"error": {"code": self.code, "message": self.message}}


# ---------------------------------------------------------------- 请求
@dataclass
class TranslateRequest:
    """校验之后的翻译请求。到这一层为止，字段都已确定且合法。"""

    text: str
    target: str
    source: str = "auto"
    newline: str = SOFT
    strict: bool = False
    max_new_tokens: Optional[int] = None


# ---------------------------------------------------------------- 配置
@dataclass
class ServiceConfig:
    """`nputserve` 自己的开关（与 `SecurityConfig` 不重叠：那边是安全基线，这边是并发模型）。

    | 字段 | 默认 | 理由 |
    |---|---|---|
    | `lane_wait_timeout_s` | `10.0` | 等**推理通道**的上限。超时给 503 `lane_busy` + `Retry-After`，比让调用方挂着等到 504 诚实。注意这与 `security.timeout_s`（"跑推理"的超时）是两件事，别混 |
    | `max_streams` | `1` | 并发流上限。NPU 单流，>1 无意义；第二个流**不排队** |
    | `max_stream_chars` | `160` | 实测值，见下。流式**不分段**，超了会静默截断 |
    | `stream_keepalive_s` | `15.0` | SSE 心跳间隔，防止中间缓冲层把长连接攒着不发 |

    ## `max_stream_chars = 160` 的实测依据（真 tokenizer 测得，不是估算）

    硬约束（SPEC.md · NPU 实现要点）：KV cache 总容量 = `MAX_PROMPT_LEN`(512) +
    `MIN_RESPONSE_LEN`(256) = **768**；且**生成 token 数超过 256 会静默截断**。
    批量翻译靠 `segment.py` 分段规避（单段 ≤ 512 字符），而流式与分段互斥，
    整段原文直接进 prompt，所以输入必须比批量短得多。

    用模型目录里真实的 `openvino_tokenizer.xml` 实测（一次性验证脚本未入库，
    结论写在这里以免失传）：

    | 语向 | 字符 | prompt token | 生成上限(`_estimate_max_tokens`) | 合计 | 判定 |
    |---|---|---|---|---|---|
    | 中→英 | 150 | 81 | 236 | 317 | OK |
    | 中→英 | 200 | 106 | 304 | 410 | **截断**（生成 > 256） |
    | 日→英 | 150 | 135 | 236 | 371 | OK |
    | 日→英 | 200 | 177 | 304 | 481 | **截断**（生成 > 256） |
    | 英→中 | 468 | 87 | 256 | 343 | 临界 |

    二分得到三条约束全部满足的最大字符数：**中/日源 165、英源 468**。
    CJK 源最吃紧（1 字 ≈ 0.85 token，译文通常还比原文长），**165 是全局最坏值**。
    取 **160** = 165 再留 3% 余量（160 字上：生成上限 249 ≤ 256，prompt ≈ 86 ≤ 512，
    合计 335 ≤ 768）。模板固定开销实测 11–15 token，已含在内。

    想放大就用 `--max-stream-chars`，但**超过上面的值就会静默截断** ——
    那是 NPU 静态形状的硬约束，服务层救不了。
    """

    lane_wait_timeout_s: float = 10.0
    max_streams: int = 1
    max_stream_chars: int = DEFAULT_MAX_STREAM_CHARS
    stream_keepalive_s: float = 15.0

    def __post_init__(self) -> None:
        if self.max_streams < 1:
            raise ValueError("max_streams 必须 >= 1")
        if self.max_stream_chars < 1:
            raise ValueError("max_stream_chars 必须 >= 1")
        if self.stream_keepalive_s <= 0:
            raise ValueError("stream_keepalive_s 必须 > 0")
        # 0 = 无限等（`--lane-wait 0` 的逃生舱）；负数没有意义
        if self.lane_wait_timeout_s < 0:
            raise ValueError("lane_wait_timeout_s 不得为负（0 = 无限等待）")


# ---------------------------------------------------------------- 推理通道
@dataclass
class LaneTicket:
    """`InferenceLane.acquire()` 的凭据，必须交回 `release()`（漏一次就永久堵住）。"""

    kind: str
    acquired_at: float


class InferenceLane:
    """推理通道准入：把「NPU 单流」从 engine 内部的隐式锁提升为服务层的显式对象。

    **它不是物理锁**：物理串行由 engine 的 `self._lock` 保证。lane 只回答
    "现在能不能进场"，并让等待**可超时 / 可观测 / 可释放**。

    为什么超时后必须**立刻**释放（而不是等孤儿跑完）：孤儿仍持有 engine 的物理锁，
    新请求拿到 lane 后自然会在那把真锁上排队 —— 两把锁各管一段，不冲突。
    若 lane 要等孤儿跑完才放，等于把 Q2 的"永久阻塞"换个地方再犯一次。
    """

    def __init__(self, wait_timeout_s: float = 10.0) -> None:
        self.wait_timeout_s = wait_timeout_s
        self._lock: asyncio.Lock | None = None
        self._lock_loop: Any = None
        self._owner: str = ""
        self._since: float = 0.0

    def _get_lock(self) -> asyncio.Lock:
        """按事件循环懒建锁。

        为什么不能直接 `asyncio.Lock()` 建在 `__init__` 里：3.11 的 `asyncio.Lock`
        会在**首次争用**时把自己绑定到当时的事件循环，之后再换 loop 用就抛
        "bound to a different event loop"。生产环境一个进程只有一个 loop 无所谓，
        但**单测里每个 `asyncio.run()` 都是新 loop**，不处理的话第二个用例直接红。
        重建即重置占用状态 —— 在"上一个 loop 已结束"的前提下这是安全的。
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
            self._owner = ""
            self._since = 0.0
        return self._lock

    async def acquire(self, kind: str = "translate") -> LaneTicket:
        """取得通道。等待超过 `wait_timeout_s` 抛 503 `lane_busy`（**锁不会被持有**）。

        `wait_timeout_s <= 0` 表示无限等（`--lane-wait 0` 的逃生舱）。
        """
        lock = self._get_lock()
        timeout = self.wait_timeout_s if self.wait_timeout_s > 0 else None
        try:
            # 3.11 的 `Lock.acquire()` 被取消时不会"半拿到锁"，可以放心 wait_for
            await asyncio.wait_for(lock.acquire(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise ApiError(
                503, "lane_busy",
                f"推理通道已被占用，等待超过 {self.wait_timeout_s:.0f}s，请稍后重试"
                f"（当前占用者：{self._owner or '未知'}）",
                headers={"Retry-After": str(max(1, int(self.wait_timeout_s)))},
            ) from None
        self._owner = kind
        self._since = time.monotonic()
        return LaneTicket(kind=kind, acquired_at=self._since)

    def release(self, ticket: LaneTicket | None = None) -> None:
        """释放通道。**必须**在 finally 里调，漏一次就永久堵住。"""
        self._owner = ""
        self._since = 0.0
        lock = self._lock
        if lock is not None and lock.locked():
            lock.release()

    @property
    def owner(self) -> str:
        """当前占用者：`""` / `"translate"` / `"stream"`（`/v1/health` 的 `lane` 字段）。"""
        return self._owner

    def held_s(self) -> float:
        """已被占用多少秒（未占用时为 0）。"""
        if not self._owner:
            return 0.0
        return round(time.monotonic() - self._since, 3)


# ---------------------------------------------------------------- 孤儿记账
@dataclass
class JobTicket:
    """一次推理的记账单。唯一职责：把「孤儿数」记平。

    孤儿 = 调用方已经放弃（超时 / 断开），但线程还在跑的请求。
    计数不平衡的后果是 `/v1/health` 的 `orphans` 永久非零，
    运维看到会以为设备一直被占着 —— 所以 `settle()` 必须由**孤儿自己**在 finally 里调，
    不依赖任何 future（那个 future 可能已经被取消、已经没人引用了）。
    """

    seq: int
    kind: str
    runtime: "ServiceRuntime"
    abandoned: bool = False
    # 内部字段：保证 settle() 只记一次账（abandon 可能被调多次）
    _settled: bool = field(default=False, init=False, repr=False, compare=False)
    error: BaseException | None = field(default=None, init=False, repr=False, compare=False)

    def abandon(self) -> None:
        """调用方放弃等待：孤儿 +1。**幂等**。"""
        with self.runtime._lock:
            if self.abandoned or self._settled:
                return
            self.abandoned = True
            self.runtime.orphans += 1
        logger.debug("请求 #%s(%s) 已放弃等待；孤儿数=%s",
                     self.seq, self.kind, self.runtime.orphans)

    def settle(self) -> None:
        """线程真跑完了：把账记平（孤儿 −1）。**幂等**。"""
        with self.runtime._lock:
            if self._settled:
                return
            self._settled = True
            if self.abandoned:
                self.runtime.orphans = max(0, self.runtime.orphans - 1)
                logger.debug(
                    "孤儿请求 #%s(%s) 落地，结果不返回给调用方（可能仍写入缓存）；剩余孤儿=%s",
                    self.seq, self.kind, self.runtime.orphans)


def _guarded(ticket: JobTicket, job: Callable[[], Any]) -> Any:
    """包一层：无论成功失败，**孤儿自己负责把自己记平**。

    吞掉异常是刻意的：`concurrent.futures` 里没人取回的异常会在 GC 时打一条
    "exception never retrieved" 噪音，而这条请求的调用方早就不存在了。
    异常本体留在 `ticket.error` 上，给需要报 500 的路径用。
    """
    try:
        return job()
    except BaseException as exc:  # noqa: BLE001 - 记下来，由调用方决定怎么报
        ticket.error = exc
        logger.debug("请求 #%s(%s) 以异常结束: %s", ticket.seq, ticket.kind,
                     type(exc).__name__)
        return None
    finally:
        ticket.settle()


# ---------------------------------------------------------------- 流槽
class StreamSlots:
    """并发流槽。快失败：无空位直接 `False`，**不排队**。

    ⚠️ 这里刻意**不用** `asyncio.Semaphore`：我们只需要"非阻塞地抢一个位子"，
    而 `asyncio.Semaphore` 会把状态绑到某个事件循环上（跨 `asyncio.run()` 单测会炸），
    还引入了一个我们根本不需要的等待队列 —— 流式的语义恰恰是"抢不到就走"。
    一个 `threading.Lock` + 计数器就够了，而且能在同步代码里直接读 `in_use`。
    """

    def __init__(self, total: int = 1) -> None:
        self.total = max(1, int(total))
        self._in_use = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_use >= self.total:
                return False
            self._in_use += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

    @property
    def free(self) -> int:
        return max(0, self.total - self.in_use)


# ---------------------------------------------------------------- 运行时
@dataclass
class ServiceRuntime:
    """`nputserve` 的运行时状态。**在 `server.py` 里由 `ServerContext` 组装**。

    为什么与 `ServerContext` 分开：`ServerContext` 是 nputweb 的概念（token /
    host_policy / 静态面那套），服务层额外需要 lane / 流槽 / 孤儿计数 / `ServiceConfig`。
    塞进 `ServerContext` 会让 nputweb 的每个实例都背着这些无关字段 ——
    更糟的是那要动到 300 项既有单测的地基。

    :param executor: **必须**是 `max_workers=1`（NPU 单流，多开只会抢锁 + OOM）
    """

    translator: Translator
    security: SecurityConfig = field(default_factory=SecurityConfig)
    config: ServiceConfig = field(default_factory=ServiceConfig)
    executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nputserve-worker"),
    )
    gate: QueueGate = field(default_factory=QueueGate)
    lane: InferenceLane = field(default_factory=InferenceLane)
    started_at: float = field(default_factory=time.time)

    orphans: int = field(default=0, init=False)
    _seq: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False,
                                  repr=False, compare=False)
    _slots: StreamSlots = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._slots = StreamSlots(self.config.max_streams)
        # lane 的等待上限以 `ServiceConfig` 为准：两处配置各说各话时，
        # 排查的人会先怀疑 lane，所以让 ServiceConfig 做唯一的真值来源。
        self.lane.wait_timeout_s = self.config.lane_wait_timeout_s

    # ---------------------------------------------------------- 序号与提交
    def next_seq(self) -> int:
        """请求序号（日志 / SSE 事件里定位用）。"""
        with self._lock:
            self._seq += 1
            return self._seq

    def new_ticket(self, kind: str) -> JobTicket:
        return JobTicket(seq=self.next_seq(), kind=kind, runtime=self)

    def spawn(self, ticket: JobTicket, job: Callable[[], Any]) -> Future:
        """把阻塞的推理丢进**唯一的** executor worker。"""
        return self.executor.submit(_guarded, ticket, job)

    def shutdown(self, wait: bool = False) -> None:
        """关停线程池。关停路径**不要**等 —— NPU 那段 generate 可能卡在原生调用里。"""
        self.executor.shutdown(wait=wait, cancel_futures=True)

    # ---------------------------------------------------------- 流槽
    @property
    def stream_slots(self) -> StreamSlots:
        return self._slots

    @property
    def streaming(self) -> bool:
        """当前是否有流式请求占着（`False` 表示通道空闲）。"""
        return self._slots.in_use > 0


@dataclass
class StreamSession:
    """一次流式请求的会话。路由**先** `acquire_stream()` 拿到它，再建 StreamingResponse。

    为什么不能让 `stream_events()` 自己去抢：async generator 的第一行代码要等到
    body 开始迭代才执行，那时响应头已经发出去了，503 就没法给了（只能 200 + 半截流）。
    """

    req: TranslateRequest
    ticket: JobTicket
    lane_ticket: LaneTicket


# ---------------------------------------------------------------- SSE 格式化
class _Sentinel:
    """队列结束哨兵。用类实例而不是 `None`，方便将来把 `None` 也当数据传。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只在调试输出里出现
        return "<SENTINEL>"


_SENTINEL = _Sentinel()


def sse_event(name: str, data: Any) -> str:
    """一个 SSE 事件。`ensure_ascii=False`：中文转义后体积翻三倍，没必要。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_comment(text: str) -> str:
    """SSE 注释行。

    首行发它是为了**冲掉中间缓冲层**（有些代理/网关会攒够一定字节才往下游发，
    首个 token 会迟到好几秒）。keepalive 也用它。
    """
    return f": {text}\n\n"


# ---------------------------------------------------------------- 服务
class TranslatorService:
    """`/v1/*` 的适配层：校验 + 调 Translator + 组装响应。**不含编排**。"""

    def __init__(self, runtime: ServiceRuntime) -> None:
        self.runtime = runtime

    # ---------------------------------------------------------- 只读端点
    def languages(self) -> dict:
        """与 `/api/languages` 同形（`total` / `common[]` / `others[]`）。"""
        def pack(items: list) -> list[dict]:
            return [
                {
                    "code": lang.code,
                    "zh_name": lang.zh_name,
                    "en_name": lang.en_name,
                    "native": lang.native,
                    # zh-Hant 的 prompt 名是「繁体中文」而不是 Traditional Chinese ——
                    # 模型认中文名，给英文名会回吐原文（SPEC.md · Prompt 与语言）
                    "prompt_name": lang.target_name,
                }
                for lang in items
            ]

        return {
            "total": len(LANGUAGES),
            "common": pack(common()),
            "others": pack(others()),
        }

    def health(self) -> dict:
        """`/v1/health`。比 `/api/health` 多 `active_device` / `lane` / `streaming` / `orphans`。

        `active_device` 与 `device` 同值、两个键都给：前者是外部要求的字段名，
        后者兼容 nputweb 的口径（别让已有脚本为了一个键名改一次）。
        """
        runtime = self.runtime
        translator = runtime.translator
        snap = translator.progress
        sec = runtime.security
        cfg = runtime.config
        return {
            "status": "ready" if translator.is_ready else "loading",
            "active_device": translator.device,
            "device": translator.device,
            "devices": translator.devices,
            "degraded": list(translator.degraded),
            # 是否正被流式请求占着（流式的价值在实时性，占着却不让人知道是坑）
            "streaming": runtime.streaming,
            # 谁占着推理通道："" / "translate" / "stream"
            "lane": runtime.lane.owner,
            # Q2：被放弃但仍在跑的推理数。非零 = 设备还被孤儿占着，
            # 这是**预期行为**不是 bug（engine 的物理锁在孤儿手里）
            "orphans": runtime.orphans,
            "queue": runtime.gate.depth,
            "waiting": runtime.gate.waiting,
            "max_pending": runtime.gate.max_pending,
            "progress": {"active": snap.active, "done": snap.done, "total": snap.total},
            "tls": sec.https,
            "uptime_s": round(time.time() - runtime.started_at, 1),
            "limits": {
                "max_input_chars": sec.max_input_chars,
                "max_stream_chars": cfg.max_stream_chars,
                "timeout_s": sec.timeout_s,
                "queue_size": sec.queue_size,
                "rate_per_min": sec.rate_per_min,
                "max_streams": cfg.max_streams,
                "lane_wait_timeout_s": cfg.lane_wait_timeout_s,
            },
        }

    # ---------------------------------------------------------- 校验
    def parse_translate(self, payload: Any, *, streaming: bool = False) -> TranslateRequest:
        """把请求体校验成 `TranslateRequest`。所有 400 / 413 分支集中在这里。

        :param streaming: 流式与分段互斥，因此会**强制单段**（忽略 `newline` / `strict`）
            并额外施加 `max_stream_chars`
        """
        sec = self.runtime.security
        cfg = self.runtime.config

        if not isinstance(payload, dict):
            raise ApiError(400, "bad_json", "请求体必须是 JSON 对象")

        text = payload.get("text")
        if not isinstance(text, str):
            raise ApiError(400, "bad_request", "text 必须是字符串")
        limit = cfg.max_stream_chars if streaming else sec.max_input_chars
        if len(text) > limit:
            raise ApiError(
                413, "payload_too_large",
                f"输入 {len(text)} 字符，超过{'流式' if streaming else ''}上限 {limit}"
                + ("（流式不分段，超了会被静默截断）" if streaming else ""),
            )
        if not text.strip():
            raise ApiError(400, "empty_input", "待翻译文本为空")

        opts = self.runtime.translator.opts
        target = str(payload.get("target") or opts.target)
        source = str(payload.get("source") or opts.source)
        if not OrchestrateConfig(target=target).supported_target:
            raise ApiError(400, "bad_target", f"不支持的目标语言: {target!r}")

        # 流式与分段互斥（多段没有单一 token 流），所以 newline / strict 一律忽略。
        # 为什么不报错而是忽略：调用方很可能直接复用了 `/v1/translate` 的请求体，
        # 因为带了个无关字段就 400 是没必要的摩擦。
        newline = str(payload.get("newline") or opts.newline)
        if not streaming and newline not in NEWLINE_MODES:
            raise ApiError(400, "bad_newline", f"newline 必须是 {NEWLINE_MODES} 之一")
        strict = False if streaming else bool(payload.get("strict", False))

        max_new_tokens = payload.get("max_new_tokens")
        if max_new_tokens is not None:
            if not isinstance(max_new_tokens, int) or isinstance(max_new_tokens, bool):
                raise ApiError(400, "bad_request", "max_new_tokens 必须是整数")
            if max_new_tokens < 1:
                raise ApiError(400, "bad_request", "max_new_tokens 必须 >= 1")

        return TranslateRequest(
            text=text,
            target=target,
            source=source,
            newline=newline,
            strict=strict,
            max_new_tokens=max_new_tokens,
        )

    # ---------------------------------------------------------- 翻译
    async def translate(self, req: TranslateRequest, *, queue_position: int = 0) -> dict:
        """跑一次翻译。超时走 Q2 孤儿路径（504 + `X-NPUT-Orphan: 1`）。"""
        runtime = self.runtime
        sec = runtime.security
        ticket = runtime.new_ticket("translate")
        lane_ticket = await runtime.lane.acquire("translate")
        started = time.perf_counter()
        try:
            future = runtime.spawn(
                ticket,
                lambda: runtime.translator.translate(req.text, target=req.target,
                                                     source=req.source),
            )
            try:
                outcome: Outcome | None = await asyncio.wait_for(
                    asyncio.wrap_future(future), timeout=sec.timeout_s)
            except (asyncio.TimeoutError, TimeoutError):
                # Q2：底层 generate 卡在原生调用里，取消不了，只能"放弃等待"。
                # ① abandon 记账 → ② finally 立刻放 lane → ③ 中间件 finally 放队列位。
                ticket.abandon()
                raise ApiError(
                    504, "timeout",
                    f"单个请求超过 {sec.timeout_s:.0f}s 未完成；已放弃等待并释放队列位置。"
                    f"底层推理不可取消，可能仍在占用设备，"
                    f"其结果不返回给调用方（可能仍写入缓存）。",
                    headers={"X-NPUT-Orphan": "1"},
                ) from None
        finally:
            # ★ lane 必须**立刻**释放，不等孤儿。理由见 `InferenceLane` 的 docstring。
            runtime.lane.release(lane_ticket)

        if outcome is None:
            exc = ticket.error
            raise ApiError(500, "internal_error",
                           _safe_error_message(exc) if exc else "翻译失败")
        if req.strict and not outcome.ok:
            raise ApiError(500, "partial_failure", f"有 {outcome.failed} 段未译出")

        data = outcome.to_dict()
        data["newline"] = req.newline
        data["queue_position"] = queue_position
        data["request_id"] = ticket.seq
        data["wall_s"] = round(time.perf_counter() - started, 3)
        return data

    # ---------------------------------------------------------- 流式
    async def acquire_stream(self, req: TranslateRequest) -> StreamSession:
        """抢流槽 + 推理通道。**快失败**：无空位直接 503，不排队。

        为什么第二个流不排队：流式的价值是"边生成边消费"，排在另一个流后面
        等于把整段输出缓冲下来，语义已经没了，还白占一条连接。快失败比挂住好。
        """
        runtime = self.runtime
        slots = runtime.stream_slots
        if not slots.try_acquire():
            raise ApiError(
                503, "stream_busy",
                f"已有 {runtime.config.max_streams} 个流式请求在进行中，请稍后重试",
                headers={"Retry-After": "2"},
            )
        try:
            lane_ticket = await runtime.lane.acquire("stream")
        except BaseException:
            # 拿到了槽却没拿到通道 → 槽必须还回去，否则流永久堵死
            slots.release()
            raise
        return StreamSession(req=req, ticket=runtime.new_ticket("stream"),
                             lane_ticket=lane_ticket)

    def _release_stream(self, session: StreamSession) -> None:
        """归还 lane 与流槽。**必须**在 finally 里调（漏一次就永久堵住）。"""
        self.runtime.lane.release(session.lane_ticket)
        self.runtime.stream_slots.release()
        # 正常结束时也要 settle 一次（幂等）：被 abandon 的路径已经记过账了，
        # 这里是保证「没被 abandon 的任务」也走完记账流程。
        session.ticket.settle()

    async def stream_events(
        self,
        req: TranslateRequest,
        is_gone: Callable[[], Awaitable[bool]],
        *,
        session: StreamSession | None = None,
    ) -> AsyncIterator[str]:
        """产出**已格式化**的 SSE 文本行（`event: ...\\ndata: ...\\n\\n`）。

        :param is_gone: 由路由注入 `request.is_disconnected`；本文件因此不依赖 fastapi
        :param session: `acquire_stream()` 的产物。路由**必须**先拿到它再建响应
            （见 `StreamSession` 的注释）；不传时这里自己 acquire（单测方便，
            也保证不会忘记释放）
        """
        runtime = self.runtime
        sec = runtime.security
        cfg = runtime.config
        if session is None:
            session = await self.acquire_stream(req)
        req = session.req
        ticket = session.ticket

        try:
            yield sse_comment("nputserve SSE")
            yield sse_event("ready", {
                "request_id": ticket.seq,
                "device": runtime.translator.device,
                "lane": session.lane_ticket.kind,
                "max_stream_chars": cfg.max_stream_chars,
            })

            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            started = time.perf_counter()
            # 客户端断开时置位，让泵线程停止读取（它仍会把当前这次 generate 跑完 ——
            # 那是 engine 的物理锁，取消不了）
            stop = threading.Event()

            def pump_worker() -> None:
                # ★ 专用线程，不是 executor 的 worker：见模块 docstring 第 1 条
                try:
                    for token in runtime.translator.stream(
                        req.text, target=req.target, source=req.source,
                        max_new_tokens=req.max_new_tokens,
                    ):
                        if stop.is_set() or ticket.abandoned:
                            break
                        _post(loop, queue, token)
                except BaseException as exc:  # noqa: BLE001 - 传回事件循环再抛
                    _post(loop, queue, exc)
                finally:
                    _post(loop, queue, _SENTINEL)

            threading.Thread(target=pump_worker, daemon=True,
                             name=f"nputserve-stream-{ticket.seq}").start()

            chunks: list[str] = []
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(),
                                                  timeout=cfg.stream_keepalive_s)
                except (asyncio.TimeoutError, TimeoutError):
                    # 心跳间隔到了还没 token：先问客户端还在不在。
                    # 不问的话，一条已经断开的连接会把 lane 一直占到总超时。
                    if await is_gone():
                        stop.set()
                        ticket.abandon()
                        yield sse_event("error", _orphan_payload(
                            "client_closed", "客户端已断开"))
                        return
                    if time.perf_counter() - started > sec.timeout_s:
                        stop.set()
                        ticket.abandon()
                        yield sse_event("error", _orphan_payload(
                            "timeout", f"流式请求超过 {sec.timeout_s:.0f}s 未完成"))
                        return
                    yield sse_comment("keepalive")
                    continue

                if item is _SENTINEL:
                    break
                if isinstance(item, BaseException):
                    yield sse_event("error", {
                        "code": "internal_error",
                        "message": _safe_error_message(item),
                        "abandoned": False,
                    })
                    return
                chunks.append(str(item))
                yield sse_event("token", {"delta": str(item)})

                if await is_gone():
                    stop.set()
                    ticket.abandon()
                    yield sse_event("error", _orphan_payload(
                        "client_closed", "客户端已断开"))
                    return

            text = "".join(chunks)
            elapsed = max(time.perf_counter() - started, 1e-6)
            yield sse_event("done", {
                "request_id": ticket.seq,
                "text": text,
                "device": runtime.translator.device,
                "chars": len(text),
                "elapsed_s": round(elapsed, 3),
                # 流式不走 TranslationPool，没有"池内计时"这一层，所以 infer_s 与
                # elapsed_s 同值。保留这个字段是为了让两个端点的响应形状一致，
                # 别让调用方写两套解析代码。
                "infer_s": round(elapsed, 3),
                "chars_per_second": round(len(text) / elapsed, 2),
            })
        finally:
            self._release_stream(session)


def _post(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, item: Any) -> None:
    """从泵线程往事件循环推一个东西。

    ⚠️ 必须容忍「循环已经关了」：孤儿线程可能比事件循环活得久（关停、
    单测里 `asyncio.run()` 结束时都常见），此时 `call_soon_threadsafe` 会抛
    `RuntimeError('Event loop is closed')`。往一个没人读的队列推东西本来就没意义，
    但让它变成线程里的**未捕获异常**会污染日志与测试输出 —— 那才是真问题。
    """
    if loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(queue.put_nowait, item)
    except RuntimeError:  # pragma: no cover - 极窄的竞态窗口
        pass


def _orphan_payload(code: str, message: str) -> dict:
    """SSE error 事件的载荷。「不可取消」这条语义必须每次都说，
    否则调用方会误以为 504 / error = 服务端已经停了。"""
    return {
        "code": code,
        "message": f"{message}；底层推理不可取消，"
                   f"结果不返回给调用方（可能仍写入缓存）",
        "abandoned": True,
    }


# ---------------------------------------------------------------- 错误脱敏
def _safe_error_message(exc: BaseException) -> str:
    """复用 `web.routes` 的脱敏摘要（**延迟** import，理由见下）。

    `web/routes.py` 在模块级 `from fastapi import APIRouter`，而本文件**不得**拉起
    fastapi —— 否则"适配层可以脱离网络栈单测"这条保证就没了。
    放进函数体：模块级干净；运行时 fastapi 必然已装（不然服务根本起不来）。
    """
    from .web.routes import _safe_error_message as _impl

    return _impl(exc)
