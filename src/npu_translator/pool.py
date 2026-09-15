"""异构调度：动态派活 + 有序重组 + 降级链（CLI 管道契约）。

## 为什么是动态派活而不是静态比例

2026-09-12 实测（6 段 × max_new_tokens=96）：

| 模式 | 耗时 | 吞吐 |
|---|---|---|
| NPU 单独 | 7.73 s | 115 字符/s |
| CPU 单独（8 线程 PCORE_ONLY） | **4.04 s** | 219 字符/s |
| 异构 50/50 **静态**分配 | 4.24 s | **比 CPU 单干慢 5%** |

CPU 比 NPU 快 1.9×，静态 50/50 后 CPU 2.51 s 干完就闲着，NPU 4.24 s 成为短板
——**快设备被慢设备拖到同速**。所以：

1. 谁空闲谁领下一段（不预设比例），快设备自然多干
2. 长段优先（LPT），避免尾巴上卡一个长段让其他人空等
3. 自动适配任意设备速度比，**也自动适配不同机器**（R13：本机"CPU 快 1.9×"不可外推）

争抢代价实测只有约 10%（NPU 单段 1.29 s → 1.41 s），并行本身是划算的。

## 有序性

结果直接按 index 落位，`run()` 返回的列表顺序恒等于输入顺序。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, Sequence

from . import config as cfg

logger = logging.getLogger(__name__)

__all__ = [
    "CallableWorker",
    "EngineWorker",
    "FatalWorkerError",
    "TaskResult",
    "TranslationPool",
    "Worker",
    "build_workers",
    "cpu_pipeline_props",
]


class FatalWorkerError(RuntimeError):
    """worker 级致命错误（模型加载失败 / 设备被抢占）。

    抛出它的 worker 会被永久摘除，池子用剩下的设备继续跑；
    普通异常只判该段失败，worker 保留。
    """


@dataclass
class TaskResult:
    index: int
    text: str
    device: str = ""
    elapsed_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Worker(Protocol):
    name: str

    def translate(self, text: str, target: str, source: str) -> str: ...
    def prepare(self) -> None: ...  # 可选，用 hasattr 判断


class TranslationPool:
    """把一批文本派给多个（异构）设备。

    :param workers: 至少 1 个；1 个时走串行快路径，不起线程
    :param max_retries: 单段失败重试次数，重试优先换一个 worker
    :param on_done: `(已完成数, 总数)` 进度回调
    :param on_degrade: worker 被摘除时的回调，用于 CLI 提示降级
    """

    def __init__(
        self,
        workers: Sequence[Worker],
        max_retries: int = 2,
        on_done: Callable[[int, int], None] | None = None,
        on_degrade: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workers: list[Worker] = list(workers)
        self.max_retries = max(0, max_retries)
        self.on_done = on_done
        self.on_degrade = on_degrade
        self.dead: set[str] = set()

    # ------------------------------------------------------------ 预热
    def prepare(self) -> list[Worker]:
        """并行预热（NPU 首次编译约 30 s，CPU 也要建流水线）。

        失败的 worker 直接摘除；全挂则抛 `FatalWorkerError`。
        """
        alive: list[Worker] = []
        errors: dict[str, str] = {}

        def _prep(w: Worker) -> None:
            try:
                if hasattr(w, "prepare"):
                    w.prepare()
                alive.append(w)
            except Exception as exc:  # noqa: BLE001
                errors[w.name] = f"{type(exc).__name__}: {exc}"
                logger.warning("worker %s 预热失败: %s", w.name, exc)

        threads = [threading.Thread(target=_prep, args=(w,), daemon=True) for w in self.workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for name, err in errors.items():
            self.dead.add(name)
            if self.on_degrade:
                self.on_degrade(name, err)

        self.workers = [w for w in self.workers if w.name not in self.dead]
        if not self.workers:
            raise FatalWorkerError(f"所有设备均不可用: {errors}")
        return self.workers

    # ------------------------------------------------------------ 执行
    def run(self, texts: Sequence[str], target: str, source: str = "auto") -> list[TaskResult]:
        """翻译一批文本，**返回顺序恒等于输入顺序**。"""
        n = len(texts)
        if n == 0:
            return []
        if not self.workers:
            raise FatalWorkerError("没有可用的 worker")
        if len(self.workers) == 1 or n == 1:
            return self._run_serial(texts, target, source)
        return self._run_parallel(texts, target, source)

    def _run_serial(self, texts: Sequence[str], target: str, source: str) -> list[TaskResult]:
        w = self.workers[0]
        out: list[TaskResult] = []
        for i, text in enumerate(texts):
            out.append(self._do_one(w, i, text, target, source))
            if self.on_done:
                self.on_done(i + 1, len(texts))
        return out

    def _do_one(self, w: Worker, index: int, text: str, target: str, source: str) -> TaskResult:
        t0 = time.perf_counter()
        try:
            out = w.translate(text, target, source)
        except FatalWorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 - 段级失败不该带走整批
            return TaskResult(index=index, text="", device=w.name,
                              elapsed_s=round(time.perf_counter() - t0, 3),
                              error=f"{type(exc).__name__}: {exc}")
        return TaskResult(index=index, text=out, device=w.name,
                          elapsed_s=round(time.perf_counter() - t0, 3))

    def _run_parallel(self, texts: Sequence[str], target: str, source: str) -> list[TaskResult]:
        n = len(texts)
        results: list[TaskResult | None] = [None] * n
        attempts = [0] * n
        avoided: dict[int, set[str]] = {}
        lock = threading.Lock()
        done = [0]
        inflight = [0]      # 正在被某个 worker 处理的任务数
        remaining = [n]     # 尚未产出最终结果的任务数（队列里的 + 在飞的）
        alive = [len(self.workers)]  # 仍在轮询的线程数

        # LPT：长段优先，避免尾巴上卡一个长段让快设备空等
        q: queue.PriorityQueue = queue.PriorityQueue()
        for i, t in enumerate(texts):
            q.put((-len(t), i))

        def settle(idx: int, res: TaskResult) -> None:
            """落定一个任务（成功或重试耗尽）。"""
            results[idx] = res
            remaining[0] -= 1
            done[0] += 1
            if self.on_done:
                self.on_done(done[0], n)

        def loop(w: Worker) -> None:
            idle = 0
            try:
                while True:
                    with lock:
                        if remaining[0] <= 0:
                            return
                    try:
                        prio, idx = q.get_nowait()
                    except queue.Empty:
                        # 不能见空就退：别的 worker 可能正拿着任务在重试（一失败就放回队列），
                        # 也可能还没来得及领。只有"队列空 + 无在飞任务"持续一段时间才真退出。
                        with lock:
                            nobody_working = inflight[0] == 0
                        if nobody_working:
                            idle += 1
                            if idle > 200:  # 约 200 ms
                                return
                        else:
                            idle = 0
                        time.sleep(0.001)
                        continue
                    idle = 0

                    with lock:
                        skip = (
                            w.name in avoided.get(idx, set())
                            and attempts[idx] < len(self.workers)
                            and alive[0] > 1
                        )
                        if skip:
                            # 换个人来。这里必须 sleep 一下：否则本线程会立刻把任务
                            # 又领回来（队列里大概率只剩它），另一个线程根本抢不到。
                            q.put((prio, idx))
                            time.sleep(0.002)
                            continue
                        inflight[0] += 1

                    try:
                        res = self._do_one(w, idx, texts[idx], target, source)
                    except FatalWorkerError as exc:
                        # worker 级致命：摘掉它，任务还回队列让别的设备接手
                        with lock:
                            self.dead.add(w.name)
                            inflight[0] -= 1
                            q.put((prio, idx))
                        if self.on_degrade:
                            self.on_degrade(w.name, str(exc))
                        return

                    with lock:
                        inflight[0] -= 1
                        if res.ok:
                            settle(idx, res)
                            continue
                        # 失败：重试，并记下这个 worker，下次优先换人
                        attempts[idx] += 1
                        avoided.setdefault(idx, set()).add(w.name)
                        if attempts[idx] > self.max_retries:
                            settle(idx, res)
                        else:
                            q.put((prio, idx))
            finally:
                with lock:
                    alive[0] -= 1

        threads = [threading.Thread(target=loop, args=(w,), daemon=True, name=f"pool-{w.name}")
                   for w in self.workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            # 兜底：理论上不会走到这里，真走到也不能返回 None 让调用方炸
            for i in missing:
                results[i] = TaskResult(index=i, text="", error="未被执行")
        return [r for r in results if r is not None]  # type: ignore[misc]


# ---------------------------------------------------------------- worker 实现
class CallableWorker:
    """测试用 worker：`fn(text, target, source) -> str`。

    单测用它验证调度语义（有序性 / 重试 / 换人），**不加载模型**。
    """

    def __init__(
        self,
        name: str,
        fn: Callable[[str, str, str], str],
        prepare_fn: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self._fn = fn
        self._prepare_fn = prepare_fn
        self.calls: list[str] = []

    def translate(self, text: str, target: str, source: str) -> str:
        self.calls.append(text)
        return self._fn(text, target, source)

    def prepare(self) -> None:
        if self._prepare_fn:
            self._prepare_fn()


class EngineWorker:
    """把一个 `TranslateEngine` 包装成池子里的 worker。

    惰性：构造不碰模型，`prepare()` 才加载（放进池子的并行预热线程里跑）。
    """

    def __init__(
        self,
        device: str,
        model_path: str | None = None,
        pipeline_props: dict | None = None,
        max_new_tokens_cap: int | None = None,
    ) -> None:
        self.device = device
        self.name = device.upper()
        self.model_path = model_path or cfg.MODEL_PATH
        self.pipeline_props = dict(pipeline_props or {})
        self.max_new_tokens_cap = max_new_tokens_cap
        self._engine: Any | None = None

    @property
    def engine(self) -> Any:
        if self._engine is None:
            from .engine import TranslateEngine

            self._engine = TranslateEngine(
                model_path=self.model_path,
                device=self.device,
                pipeline_props=self.pipeline_props,
                max_new_tokens_cap=self.max_new_tokens_cap,
            )
        return self._engine

    def prepare(self) -> None:
        try:
            self.engine.load()
        except Exception as exc:  # noqa: BLE001
            raise FatalWorkerError(f"{self.name} 加载失败: {exc}") from exc

    def translate(self, text: str, target: str, source: str) -> str:
        try:
            return self.engine.translate(text, target=target, source=source).text
        except FatalWorkerError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 设备被抢占 / OOM 之类：判定为 worker 致命，摘除后其余设备继续
            if "device" in str(exc).lower() or isinstance(exc, (RuntimeError, MemoryError, OSError)):
                raise FatalWorkerError(f"{self.name} 推理失败: {exc}") from exc
            raise


# ---------------------------------------------------------------- CPU 调度
_CPU_CORE_TYPES = {
    "any": "ANY_CORE",
    "pcore": "PCORE_ONLY",
    "ecore": "ECORE_ONLY",
}


def cpu_pipeline_props(
    threads: str | int = 0,
    core_type: str = "any",
    ht: str | None = None,
) -> dict:
    """构造 CPU 侧的 OpenVINO 属性（CLI 管道契约）。

    :param threads: `0` = OpenVINO 自动（**默认，不写死核心数**，R13）；
                    `half` = `cpu_count() // 2`；正整数 = 显式
    :param core_type: `any` | `pcore` | `ecore`，原样透传，**代码不做任何机器判断**
    :param ht: `on` / `off`，`None` = OpenVINO 默认

     ⚠️ 跨机器警告：本机（275HX，8P+16E）的实测最优值**不可外推**。
    Lunar Lake 只有 4P+4LP-E，`ecore` 会慢到没意义。所以默认值保持"自动"。
    """
    import os

    props: dict[str, Any] = {}

    resolved = 0
    if isinstance(threads, str):
        key = threads.strip().lower()
        if key == "half":
            resolved = max(1, (os.cpu_count() or 2) // 2)
        elif key.isdigit():
            resolved = int(key)
        else:
            resolved = 0
    else:
        resolved = max(0, int(threads))

    if resolved > 0:
        props["INFERENCE_NUM_THREADS"] = resolved

    ct = _CPU_CORE_TYPES.get(str(core_type).strip().lower())
    if ct and ct != "ANY_CORE":
        props["SCHEDULING_CORE_TYPE"] = ct

    if ht is not None:
        flag = str(ht).strip().lower()
        if flag in {"on", "off"}:
            props["ENABLE_HYPER_THREADING"] = "YES" if flag == "on" else "NO"
    return props


def build_workers(
    device: str,
    model_path: str | None = None,
    cpu_props: dict | None = None,
    hetero_devices: Iterable[str] = ("NPU", "CPU"),
    max_new_tokens_cap: int | None = None,
) -> list[Worker]:
    """按设备模式建 worker 列表。

    - `hetero` → NPU + CPU 两条流水线（**代价约 4 GB 内存**，实测见「CLI 管道契约」）
    - 其他 → 单设备，**不加载第二条 pipeline**（CLI 管道契约）
    """
    dev = (device or cfg.DEVICE).strip().lower()

    def _make(d: str) -> EngineWorker:
        return EngineWorker(
            device=d,
            model_path=model_path,
            pipeline_props=cpu_props if d.upper() == "CPU" else None,
            max_new_tokens_cap=max_new_tokens_cap,
        )

    if dev == "hetero":
        return [_make(d) for d in hetero_devices]
    return [_make(dev)]
