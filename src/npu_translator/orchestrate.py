"""共用编排层：翻译一段文本的完整流程（SPEC.md · WebUI（nputweb））。

## 为什么抽这一层

CLI / TUI / WebUI / M3-service 都要做同一件事::

    分段 → 查缓存 → 批内去重 → 池翻译 → 回填 → 打包行数校验 → 按行拼接

D8 明确要求**先抽**共用编排层再各自做 UI，否则这四份实现会把「批内去重」
「hard 打包行数校验」「批回填」这些踩过坑的细节复制四遍，然后各自漂移。
（前两个都是实测出来的 bug：不去重则重复段被翻 N 遍；不校验则 hard 模式行数会掉。）

## 职责边界（越界就等于把 UI 逻辑漏进来）

本层**只做编排**，不碰：

- **输入怎么来**（stdin / 文件 / HTTP body）→ 调用方的事
- **输出怎么走**（stdout / 文件 / JSON）→ 调用方的事
- **退出码 / HTTP 状态码怎么映射** → 调用方的事
  （同一件"失败"，CLI 是退出码 4，WebUI 是 200 + `failed` 计数）

## 有状态 vs 无状态

`Translator` 是**有状态**的：持有 worker 池与译文缓存，`prepare()` 一次之后
可以反复 `translate()`。WebUI / TUI / service 都是长生命周期，需要这个；
CLI 是一次性的，用完即弃，行为与抽出之前逐字一致。

## 关于吞吐口径（重要，别误读）

`Outcome.chars_per_second` 用的是**输出字符数 / 池内推理秒数**，不是 tokenizer 的真 token 数。
理由：本项目的 GeneAI 路径拿不到 decode 计数（`TranslateEngine.tokens` 本身就是 `len(out)`），
字符/s 口径见 `SPEC.md · 实测基线`（M2 异构表用的就是字符/s）。宁可用一个口径一致可比的真数，
也不用一个看着像 token/s 的估数。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import config as cfg
from .cache import TranslationCache
from .languages import is_supported
from .pool import FatalWorkerError, TranslationPool, Worker, build_workers
from .segment import NEWLINE_MODES, SOFT, Plan, Unit, segment

__all__ = ["OrchestrateConfig", "Outcome", "Translator"]


@dataclass
class OrchestrateConfig:
    """编排参数。与 CLI 的 `Options` 是两回事 —— 这里**不含** IO 与输出相关的东西。"""

    target: str = "en"
    source: str = "auto"
    device: str = cfg.DEVICE
    newline: str = SOFT
    no_segment: bool = False
    no_cache: bool = False
    model_path: str | None = None
    cpu_props: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.newline not in NEWLINE_MODES:
            raise ValueError(f"newline 必须是 {NEWLINE_MODES} 之一，收到 {self.newline!r}")

    @property
    def supported_target(self) -> bool:
        """目标语言是否在已知语种表内（CLI 据此提示，WebUI 据此决定是否放行）。"""
        return is_supported(self.target)


@dataclass
class Outcome:
    """一次翻译的结果。IO 无关，可直接 JSON 化。"""

    text: str
    device: str = ""
    units: int = 0            # 翻译单元（= 送模型的次数）数
    lines: int = 0            # 输入行数
    chars: int = 0            # 输入字符数
    elapsed_s: float = 0.0    # 端到端（含缓存查询与拼接）
    infer_s: float = 0.0      # 纯推理（池内）耗时
    model_calls: int = 0      # 真正送模型的次数（= 去重后的 todo 数）
    failed: int = 0           # 未译出、保留原文的段数
    reused: int = 0           # 缓存命中 + 批内复用
    cache_hits: int = 0       # 其中来自缓存的部分
    packed_retries: int = 0   # hard 打包行数不匹配、退回逐行重翻的单元数
    # 逐段失败明细 [(单元序号, 错误)]。CLI 据此逐条 warn，WebUI 据此提示用户哪段是原文
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def cached(self) -> bool:
        """这次是不是整段命中缓存（没动过模型）。"""
        return self.units > 0 and self.reused >= self.units and self.infer_s == 0.0

    @property
    def chars_per_second(self) -> float:
        """见模块 docstring 的「关于吞吐口径」—— 这是字符/s，不是 token/s。"""
        if self.infer_s <= 0:
            return 0.0
        return round(len(self.text) / self.infer_s, 2)

    def to_dict(self) -> dict:
        """给 WebUI / service 用的扁平字典（键名与 nputweb 的 API 契约一致）。"""
        return {
            "text": self.text,
            "device": self.device,
            "segments": self.units,
            "lines": self.lines,
            "chars": self.chars,
            "elapsed_s": round(self.elapsed_s, 3),
            "infer_s": round(self.infer_s, 3),
            "chars_per_second": self.chars_per_second,
            "failed": self.failed,
            "model_calls": self.model_calls,
            "reused": self.reused,
            "cached": self.cached,
        }


# ---------------------------------------------------------------- 进度快照
@dataclass
class ProgressSnapshot:
    """当前翻译进度（WebUI 轮询 `/api/health` 用）。

    为什么不用 SSE：本模块是单并发串行队列，同一时刻只有一个请求在跑，
    轮询一个共享快照就能拿到**真实**的段进度，不必为此引入流式协议
    （见 SPEC.md · 架构与目录结构 · 共用编排层 orchestrate.py）——
    前端的「翻译中 3/7 段」因此是真数据，不是假动画。
    """

    active: bool = False
    done: int = 0
    total: int = 0
    device: str = ""


class Translator:
    """有状态编排器：持有 worker 池与缓存，可反复调用。

    惰性：构造**不碰模型**（worker 是惰性的），`prepare()` 才加载。
    这条是刻意的 —— 调用方要把 `prepare()` 放进后台线程，
    让它和「读输入 / 等 HTTP 请求」并行（NPU 首次编译约 30 s）。

    :param opts: 编排参数
    :param max_retries: 单段失败重试次数（`TranslationPool` 语义）
    :param on_degrade: worker 被摘除时的回调 `(名字, 原因)`；WebUI 据此报降级
    :param on_progress: 段完成回调 `(已完成, 总数)`；WebUI 据此显示进度
    :param pool_factory: 注入点，单测用来塞替身 worker，**不加载模型**
    """

    def __init__(
        self,
        opts: OrchestrateConfig | None = None,
        *,
        max_retries: int = 2,
        on_degrade: Callable[[str, str], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        cache: TranslationCache | None = None,
        pool_factory: Callable[[OrchestrateConfig], list[Worker]] | None = None,
    ) -> None:
        self.opts = opts or OrchestrateConfig()
        self._on_progress = on_progress
        self._cache = cache if cache is not None else TranslationCache(enabled=not self.opts.no_cache)
        self.degraded: list[str] = []
        self._prepared = False

        factory = pool_factory or _default_pool_factory
        workers = factory(self.opts)
        self._pool = TranslationPool(
            workers,
            max_retries=max_retries,
            on_done=self._on_pool_done,
            on_degrade=self._degrade,
        )
        self._ext_on_degrade = on_degrade

        self._progress = ProgressSnapshot()
        self._progress_lock = threading.Lock()

    # ------------------------------------------------------------ 池 / 设备
    def _degrade(self, name: str, err: str) -> None:
        """内部钩子：先记账，再转发给外部回调（None 时也不许吞掉账目）。"""
        if name not in self.degraded:
            self.degraded.append(name)
        if self._ext_on_degrade:
            self._ext_on_degrade(name, err)

    @property
    def pool(self) -> TranslationPool:
        return self._pool

    @property
    def workers(self) -> list[Worker]:
        return self._pool.workers

    @property
    def cache(self) -> TranslationCache:
        return self._cache

    @property
    def device(self) -> str:
        """实际生效的设备名（未加载时返回解析结果，供启动横幅显示）。"""
        try:
            return self.workers[0].name  # type: ignore[union-attr]
        except IndexError:
            return self.opts.device.upper()

    @property
    def devices(self) -> list[str]:
        """回退链（含已失效的），启动横幅要显示「NPU → CPU」这种链。"""
        names = [w.name for w in self._pool.workers]
        return names or [self.opts.device.upper()]

    @property
    def is_ready(self) -> bool:
        """模型是否已就绪。WebUI 的 `/api/health` 用它区分 loading / ready。"""
        return bool(self._pool.workers) and self._prepared

    def _on_pool_done(self, done: int, total: int) -> None:
        """池子每落定一段回调一次：更新共享快照，再转发给外部。"""
        self._note_progress(done, total)
        if self._on_progress:
            self._on_progress(done, total)

    # ------------------------------------------------------------ 预热
    def prepare(self) -> list[Worker]:
        """加载模型（阻塞，约 4 s 热 / 30 s 冷）。幂等。

        失败的 worker 被摘除；全挂则抛 `FatalWorkerError`（调用方映射成自己的错误语义）。
        """
        workers = self._pool.prepare()
        self._prepared = True
        return workers

    # ------------------------------------------------------------ 进度
    @property
    def progress(self) -> ProgressSnapshot:
        with self._progress_lock:
            return ProgressSnapshot(**self._progress.__dict__)

    def _note_progress(self, done: int, total: int) -> None:
        with self._progress_lock:
            self._progress.active = done < total
            self._progress.done = done
            self._progress.total = total
            self._progress.device = self.device

    # ------------------------------------------------------------ 翻译
    def translate(
        self,
        text: str,
        target: str | None = None,
        source: str | None = None,
    ) -> Outcome:
        """翻译一段（可含换行）文本。

        :param target / source: 覆盖 `OrchestrateConfig` 里的语向；
                                WebUI 每次请求的语言可能不同，所以用参数而不是成员变量
        """
        opts = self.opts
        tgt = opts.target if target is None else target
        src = opts.source if source is None else source

        t_start = time.perf_counter()
        plan = self._plan(text)
        n = len(plan.units)

        cache = self._cache
        results: list[str | None] = [None] * n
        todo: list[Unit] = []
        dup_units: list[Unit] = []
        queued: dict[str, int] = {}
        reused = 0
        cache_hits = 0

        # 同一批里文本完全相同的段只翻一次。顺序很重要：**先查缓存，再去重**，
        # 否则「批量查缓存」时缓存必然是空的（还没写入过），重复段会被翻 N 遍。
        for unit in plan.units:
            hit = cache.get(unit.text, tgt, src)
            if hit is not None:
                results[unit.index] = hit
                reused += 1
                cache_hits += 1
            elif unit.text in queued:
                dup_units.append(unit)
            else:
                queued[unit.text] = unit.index
                todo.append(unit)

        failed = 0
        failures: list[tuple[int, str]] = []
        infer_s = 0.0
        if todo:
            if not self._pool.workers:
                raise FatalWorkerError("没有可用的 worker")
            self._note_progress(0, len(todo))
            pool_results = self._pool.run([u.text for u in todo], tgt, src)
            infer_s = sum(r.elapsed_s for r in pool_results)

            fresh: dict[str, str] = {}
            for unit, res in zip(todo, pool_results):
                if res.ok:
                    results[unit.index] = res.text
                    fresh[unit.text] = res.text
                    cache.put(unit.text, tgt, src, res.text)
                else:
                    failed += 1
                    failures.append((unit.index, res.error or ""))
                    results[unit.index] = unit.text  # 段级失败保留原文

            # 批内回填：去重时被跳过的段，用刚才那一次的结果
            for unit in dup_units:
                if unit.text in fresh:
                    results[unit.index] = fresh[unit.text]
                    cache.put(unit.text, tgt, src, fresh[unit.text])
                    reused += 1
                else:
                    # 对应的那次翻译失败了，这里只能保留原文（失败已在上一步计过一次）
                    results[unit.index] = unit.text
            self._note_progress(len(todo), len(todo))

        # hard 打包快路径：模型吐出的行数对不上就退回逐行重翻
        packed_retries = 0
        mismatch = plan.validate([r or "" for r in results])
        if mismatch:
            for i in mismatch:
                unit = plan.units[i]
                if len(unit.sources) < 2:
                    continue
                rows = self._pool.run(list(unit.sources), tgt, src)
                results[i] = "\n".join(
                    r.text if r.ok else s for r, s in zip(rows, unit.sources)
                )
                packed_retries += 1

        out = plan.join([r if r is not None else "" for r in results])

        return Outcome(
            text=out,
            device=self.device,
            units=n,
            lines=len(plan.lines),
            chars=len(text),
            elapsed_s=round(time.perf_counter() - t_start, 3),
            infer_s=round(infer_s, 3),
            model_calls=len(todo),
            failed=failed,
            reused=reused,
            cache_hits=cache_hits,
            packed_retries=packed_retries,
            failures=failures,
        )

    # ------------------------------------------------------------ 流式
    def stream(
        self,
        text: str,
        target: str | None = None,
        source: str | None = None,
        max_new_tokens: int | None = None,
    ) -> Iterator[str]:
        """逐 token 产出。与分段互斥（多段没有单一 token 流），调用方负责拦。

        只从第一个 worker 出 —— 流式只有一个设备产出（SPEC.md · CLI 管道契约）。
        """
        opts = self.opts
        tgt = opts.target if target is None else target
        src = opts.source if source is None else source
        worker = self._pool.workers[0]
        engine = getattr(worker, "engine", None)
        if engine is None:
            raise RuntimeError(f"worker {worker.name} 不支持流式")
        yield from engine.stream(text, target=tgt, source=src, max_new_tokens=max_new_tokens)

    # ------------------------------------------------------------ 分段
    def _plan(self, text: str) -> Plan:
        """按配置把文本切成 Plan。`--no-segment` 是"明知会截断也要单段"的逃生舱。"""
        opts = self.opts
        if opts.no_segment:
            lines = text.split("\n")
            return Plan(
                mode=SOFT,
                lines=lines,
                units=[
                    Unit(
                        text=text,
                        index=0,
                        line_start=0,
                        line_end=max(0, len(lines) - 1),
                        sources=(),
                    )
                ],
            )
        return segment(text, mode=opts.newline, pack=opts.newline == "hard")


def _default_pool_factory(opts: OrchestrateConfig) -> list[Worker]:
    """按设备模式建 worker 列表（惰性，不加载模型）。"""
    return build_workers(
        opts.device,
        model_path=opts.model_path,
        cpu_props=opts.cpu_props or None,
    )


def translate_once(
    text: str,
    target: str = "en",
    source: str = "auto",
    **kwargs: Any,
) -> Outcome:
    """一次性翻译（脚本 / 交互式用的便捷入口）：内部建 Translator 并预热。

    长生命周期的场景（WebUI / TUI / service）**不要**用这个 —— 每次调用都会重建
    流水线，冷启动 30 s，而且绕开了「进程级单例」的初衷（SPEC.md · 架构与目录结构）。
    """
    orch = Translator(OrchestrateConfig(target=target, source=source, **kwargs))
    orch.prepare()
    return orch.translate(text)
