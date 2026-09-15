"""推理引擎：LLMPipeline 封装（架构与目录结构）。

要点：
- **惰性加载**：构造不碰模型，首次 translate 才 load（避免 import 即吃 1GB 内存）
- **进程级单例**：`get_engine()`，多设备/多实例会导致 NPU 编译冲突与 OOM
- **全局串行锁**：NPU 是单流设备，并发不会更快，只会 OOM（R6）
- **设备回退**：NPU 加载或推理失败 → GPU(Intel) → CPU
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Iterator

from . import config as cfg
from .device import DeviceManager
from .languages import en_name, get
from .postprocess import clean
from .prompt import build

logger = logging.getLogger(__name__)

# 设备回退顺序（与 device.FALLBACK_CHAIN 一致）
_LOAD_FALLBACK = ("NPU", "GPU", "CPU")


@dataclass
class TranslateResult:
    text: str
    device: str
    tokens: int
    elapsed_s: float
    tokens_per_second: float

    def __str__(self) -> str:  # pragma: no cover
        return self.text


class TranslateEngine:
    """翻译引擎。线程安全（推理串行化），但**不要**创建多个实例。"""

    def __init__(
        self,
        model_path: str | None = None,
        device: str | None = None,
        manager: DeviceManager | None = None,
        max_new_tokens_cap: int | None = None,
        pipeline_props: dict | None = None,
    ) -> None:
        self.model_path = model_path or cfg.MODEL_PATH
        self.preferred_device = device or cfg.DEVICE
        self.max_new_tokens_cap = max_new_tokens_cap or cfg.MAX_NEW_TOKENS_CAP
        self.pipeline_props = dict(pipeline_props or {})
        self._manager = manager or DeviceManager()

        self._pipe: Any | None = None
        self._device: str | None = None
        self._lock = threading.Lock()  # 串行化推理
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------ 属性
    @property
    def device(self) -> str:
        """实际生效的设备（未加载时返回解析出的目标设备）。"""
        if self._device:
            return self._device
        return self._manager.resolve(self.preferred_device)

    @property
    def is_loaded(self) -> bool:
        return self._pipe is not None

    # ------------------------------------------------------------ 加载
    def _pipeline_config(self, device: str) -> dict:
        """NPU 需要静态形状 + 编译缓存；CPU/GPU 用默认。

        `pipeline_props` 承载 CPU 调度类属性（`INFERENCE_NUM_THREADS` /
        `SCHEDULING_CORE_TYPE` / `ENABLE_HYPER_THREADING`，见「CLI 管道契约」）。
        """
        base = cfg.npu_pipeline_config() if device.upper().startswith("NPU") else {}
        return {**base, **self.pipeline_props}

    def _config_variants(self, device: str) -> list[dict]:
        """先试完整配置，失败再退回无 CPU 属性的配置。

        原因：这些属性名跨 OpenVINO 版本可能改名或不被支持，而用户只是想限流，
        不该因为一个调度属性直接拿不到模型。
        """
        full = self._pipeline_config(device)
        if not self.pipeline_props:
            return [full]
        base = cfg.npu_pipeline_config() if device.upper().startswith("NPU") else {}
        return [full, base]

    def load(self) -> "TranslateEngine":
        """加载模型，失败则沿回退链降级。幂等。"""
        if self._pipe is not None:
            return self

        with self._load_lock:
            if self._pipe is not None:  # 双重检查
                return self

            import openvino_genai as ov_genai

            wanted = self._manager.resolve(self.preferred_device)
            # 从用户选定的位置开始往后回退
            if wanted.upper().startswith("GPU"):
                candidates = [wanted, "CPU"]
            elif wanted.upper().startswith("NPU"):
                candidates = ["NPU", *( [self._manager.intel_gpu()] if self._manager.intel_gpu() else [] ), "CPU"]
            else:
                candidates = ["CPU"]

            last_exc: Exception | None = None
            for dev in candidates:
                if dev is None:
                    continue
                for attempt_cfg in self._config_variants(dev):
                    try:
                        logger.info("加载模型到 %s: %s", dev, self.model_path)
                        pipe = ov_genai.LLMPipeline(self.model_path, dev, **attempt_cfg)
                        self._pipe = pipe
                        self._device = dev
                        logger.info("模型已就绪，设备=%s", dev)
                        return self
                    except Exception as exc:
                        last_exc = exc
                        logger.warning("设备 %s 加载失败: %s: %s", dev, type(exc).__name__, exc)

            raise RuntimeError(f"所有设备均加载失败，最后错误: {last_exc}")

    def unload(self) -> None:
        with self._load_lock:
            self._pipe = None
            self._device = None

    def warmup(self) -> float:
        """用一句极短 prompt 触发 NPU 编译，返回耗时（秒）。

        首次编译约 30 s，命中 .npucache 后约 4 s（实测，见实测基线）。
        """
        self.load()
        import time

        t0 = time.perf_counter()
        with self._lock:
            self._pipe.generate("Translate the following segment into English, without additional explanation.\n\nhi",
                                self._generation_config(8))
        return time.perf_counter() - t0

    # ------------------------------------------------------------ 生成配置
    def _generation_config(self, max_new_tokens: int) -> Any:
        import openvino_genai as ov_genai

        g = ov_genai.GenerationConfig()
        g.max_new_tokens = max_new_tokens
        g.temperature = cfg.TEMPERATURE
        g.top_p = cfg.TOP_P
        if cfg.TOP_K:
            g.top_k = cfg.TOP_K
        g.do_sample = cfg.DO_SAMPLE
        g.repetition_penalty = cfg.REPETITION_PENALTY
        return g

    @staticmethod
    def _target_name(target: str) -> str:
        """把语言代码转成 prompt 需要的英文名。

        ⚠️ 不能用 `target.islower()` 判断：`zh-Hant` 含大写，会被误判成"已经是英文名"
        而原样塞进 prompt，模型看不懂就直接回吐原文。因此一律查语种表。
        """
        lang = get(target)
        return lang.target_name if lang else en_name(target)

    @staticmethod
    def _estimate_max_tokens(text: str) -> int:
        """按源文本长度估算生成上限。

        粗估：中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token，
        译文通常比原文长（取 1.6 倍），再加固定余量。
        """
        ascii_ratio = sum(c.isascii() for c in text) / max(len(text), 1)
        approx_tokens = len(text) * (0.30 if ascii_ratio > 0.7 else 0.85)
        return int(approx_tokens * 1.6) + 32

    # ------------------------------------------------------------ 翻译
    def translate(
        self,
        text: str,
        target: str,
        source: str = "auto",
        terminology: str | None = None,
        context: str | None = None,
        max_new_tokens: int | None = None,
        postprocess: bool = True,
    ) -> TranslateResult:
        """翻译一段文本。

        :param text: 待翻译文本（建议 ≤256 token，长文本请先过 segment）
        :param target: 目标语言**代码**（en / ja / ...），也接受英文名
        :param source: 源语言代码，auto 表示交给模型判断（模板按是否含中文选择）
        :param terminology: 术语表（可选）
        :param context: 上下文（可选）
        """
        import time

        self.load()
        target_name = self._target_name(target)
        prompt = build(text, target_name, source_lang=source, terminology=terminology, context=context)

        if max_new_tokens is None:
            max_new_tokens = min(self.max_new_tokens_cap, self._estimate_max_tokens(text))

        gen = self._generation_config(max_new_tokens)
        t0 = time.perf_counter()
        with self._lock:  # NPU 单流：串行化
            raw = self._pipe.generate(prompt, gen)
        elapsed = time.perf_counter() - t0

        out = clean(raw) if postprocess else raw.strip()
        n_tokens = max(len(out), 1)
        return TranslateResult(
            text=out,
            device=self._device or "?",
            tokens=n_tokens,
            elapsed_s=round(elapsed, 3),
            tokens_per_second=round(n_tokens / elapsed, 2) if elapsed > 0 else 0.0,
        )

    def stream(
        self,
        text: str,
        target: str,
        source: str = "auto",
        max_new_tokens: int | None = None,
    ) -> Iterator[str]:
        """真·流式输出，逐个 subword 产出。

        实现说明：GenAI 的 streamer 是**同步回调**，`generate()` 会阻塞到生成结束，
        直接 `yield from` 只会逐字符吐出完整结果。因此这里把 generate 放到工作线程，
        用队列把 token 传回调用方，实现边生成边消费。
        """
        import queue
        import threading as _th

        self.load()
        target_name = self._target_name(target)
        prompt = build(text, target_name, source_lang=source)
        if max_new_tokens is None:
            max_new_tokens = min(self.max_new_tokens_cap, self._estimate_max_tokens(text))
        gen = self._generation_config(max_new_tokens)

        q: queue.Queue = queue.Queue()

        def _worker() -> None:
            try:
                with self._lock:  # 锁在线程内持有，避免死锁
                    self._pipe.generate(prompt, gen, lambda sub: (q.put(sub), False)[1])
            except Exception as exc:  # noqa: BLE001 - 需要把异常传回主线程
                q.put(exc)
            finally:
                q.put(None)  # 结束哨兵

        _th.Thread(target=_worker, daemon=True).start()

        while True:
            item = q.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item


# ---------------------------------------------------------------- 单例
_engine: TranslateEngine | None = None
_engine_lock = threading.Lock()


def get_engine(**kwargs: Any) -> TranslateEngine:
    """获取进程级单例引擎。首次调用可传参，后续调用忽略参数。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = TranslateEngine(**kwargs)
    return _engine
