"""跨平台性能基准（SPEC.md · CLI 管道契约 · --benchmark）。

给 `nputr -b` 用，也可被 `scripts/` 或第三方脚本直接 import。

## 三条设计约束

1. **不复用 `TranslateEngine`** —— engine 自带设备回退链，NPU 加载失败会静默落到 CPU，
   那样报告会把 CPU 的成绩标成 NPU，正好是基准最不能犯的错。
   基准必须**钉死设备**：直接用 `LLMPipeline(model_path, device, **cfg)`。

2. **管道可注入** —— `pipeline_factory` / `gen_config_factory` 让单测完全不碰 OpenVINO
   （套件必须保持秒级，见 tests/test_cli_exit.py 的说明）。

3. **报告自带环境指纹** —— 跨平台对比的前提是知道数据来自哪台机器：
   OS / CPU 核数 / Python / OpenVINO / NPU 驱动与 tiles 全都进 JSON。
   本机「CPU 比 NPU 快 1.9×」不可外推（SPEC.md · 已知问题与活跃风险 · 跨机器默认值不可外推），换机器结论可能反转。

## 与 scripts/bench.py 的关系

`scripts/bench.py` 是 M0 的一次性探测脚本（能扫静态形状、能指定 cache dir）。
本模块是**常驻的、跨平台的**基准：固定 prompt 集 + 固定 repeats，
两侧共用 `DEFAULT_PROMPTS`，保证 `docs/bench_all.json` 的 M0 基线与新数据可比。
"""
from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from . import config as cfg
from .device import DeviceManager
from .languages import target_name
from .prompt import build

__all__ = [
    "BENCH_SOURCE_SENTENCES",
    "BenchReport",
    "DEFAULT_PROMPTS",
    "DeviceResult",
    "RunResult",
    "build_prompts",
    "display_path",
    "environment_info",
    "pipeline_config_for",
    "render_text",
    "run_benchmark",
    "select_devices",
]

# ★ 与 scripts/bench.py 的 BENCH_PROMPTS 完全一致（逐字）。
#   改这里等于改 M0 基线的口径，跨版本数据就不可比了。
DEFAULT_PROMPTS: tuple[str, ...] = (
    "将以下文本翻译为English,注意只需要输出翻译后的结果,不要额外解释:\n\n今天天气很好,我们一起去公园散步吧。",
    "Translate the following segment into Chinese, without additional explanation.\n\nThe rapid development of artificial intelligence has brought profound changes to every aspect of our daily lives.",
    "将以下文本翻译为Japanese,注意只需要输出翻译后的结果,不要额外解释:\n\n这个项目的目标是构建一个完全离线的本地翻译程序。",
    "Translate the following segment into French, without additional explanation.\n\nPlease make sure that all the data stays on this machine and never leaves the local network.",
    "将以下文本翻译为English,注意只需要输出翻译后的结果,不要额外解释:\n\n人工智能技术正在深刻地改变着我们的生活方式。",
)

# `-b --to xx` 时用它现场拼 prompt（源语言 -> 目标语言固定，便于跨机器对比同一语向）
BENCH_SOURCE_SENTENCES: tuple[tuple[str, str], ...] = (
    ("zh", "今天天气很好,我们一起去公园散步吧。"),
    ("en", "The rapid development of artificial intelligence has brought profound changes to every aspect of our daily lives."),
    ("zh", "这个项目的目标是构建一个完全离线的本地翻译程序。"),
    ("en", "Please make sure that all the data stays on this machine and never leaves the local network."),
    ("zh", "人工智能技术正在深刻地改变着我们的生活方式。"),
)


# ---------------------------------------------------------------- 辅助
def _finite(value: float) -> float:
    """把 NaN / inf 归零（JSON 不允许 NaN，展示也不该出现）。"""
    return value if value == value and value not in (float("inf"), float("-inf")) else 0.0


def display_path(value: Any) -> Any:
    """把绝对路径收敛成相对 / 文件名（SPEC.md · Git 约定：本机绝对路径禁止入库）。

    基准报告是要进 `docs/` 的，而模型路径与 `NPUW_CACHE_DIR` 天然是绝对路径
    （`config.MODEL_PATH` 就长 `X:\\...\\models\\...`）。不收敛就等于把机器目录结构
    写进版本库 —— 换台机器这份报告也读不出意义，只有噪音。
    """
    if not isinstance(value, str):
        return value
    try:
        p = Path(value)
        if not p.is_absolute():
            return value
        if p.is_relative_to(cfg.ROOT):
            return p.relative_to(cfg.ROOT).as_posix()
        return p.name
    except Exception:  # noqa: BLE001 - 路径形态异常时原样返回，不影响基准
        return value


def _mean(values: Sequence[float]) -> float:
    vals = [_finite(v) for v in values]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


# ---------------------------------------------------------------- 环境指纹
def environment_info() -> dict[str, Any]:
    """跨平台环境指纹。缺什么记 N/A，**不因探测失败中断基准**。"""
    import os

    info: dict[str, Any] = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "N/A",
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
    }
    try:
        import openvino as ov

        info["openvino"] = str(ov.__version__)
    except Exception as exc:  # noqa: BLE001 - 指纹缺失不该中断基准
        info["openvino"] = f"N/A ({type(exc).__name__})"
    try:
        from .device import DeviceManager as _DM

        info["npu"] = _DM().npu_info()
    except Exception as exc:  # noqa: BLE001
        info["npu"] = f"N/A ({type(exc).__name__})"
    return info


# ---------------------------------------------------------------- 设备与配置
def select_devices(preferred: str = "auto", manager: DeviceManager | None = None) -> list[str]:
    """挑出要跑基准的设备列表。

    - `auto`（含**没显式指定** `-d` 时）→ 所有可用且本项目真能用的设备：
      NPU → Intel iGPU → CPU。跨平台机器上 NPU / iGPU 不存在时自动跳过。
    - 显式指定（`-d npu` 等）→ 只跑这一个；设备不可用时由 `DeviceManager.resolve`
      降级，报告里显示的是**实际跑的设备**。
    - `hetero` 在基准里没有意义（要的是单个设备的成绩），按 auto 处理。

    ⚠️ GPU 必须过 vendor 过滤：OpenVINO 会把 NVIDIA dGPU 列成 `GPU.1`，
    但它不走 CUDA 后端，跑上去必失败（SPEC.md · 踩坑记录）。
    """
    m = manager or DeviceManager()
    key = (preferred or "auto").strip().lower()
    if key in {"", "auto", "hetero"}:
        picked: list[str] = []
        available = [d.upper() for d in m.available_devices()]
        if "NPU" in available:
            picked.append("NPU")
        igpu = m.intel_gpu()
        if igpu:
            picked.append(igpu)
        if "CPU" in available or not picked:
            picked.append("CPU")
        # 去重保序（intel_gpu() 可能返回已在列表里的名字）
        return list(dict.fromkeys(picked))
    return [m.resolve(preferred)]


def pipeline_config_for(device: str, cpu_props: dict | None = None) -> dict:
    """按设备拼流水线配置：NPU 要静态形状 + 编译缓存，CPU 可带线程/核心类型。"""
    base = cfg.npu_pipeline_config() if device.upper().startswith("NPU") else {}
    extra = cpu_props if (cpu_props and device.upper().startswith("CPU")) else {}
    return {**base, **extra}


def build_prompts(target: str | None = None) -> list[str]:
    """`-b --to xx` 时按指定语向拼 prompt；否则用固定混合集。"""
    if not target:
        return list(DEFAULT_PROMPTS)
    name = target_name(target)
    return [build(text, name, source_lang=src) for src, text in BENCH_SOURCE_SENTENCES]


# ---------------------------------------------------------------- 数据结构
@dataclass
class RunResult:
    """单次生成的测量值。"""

    prompt_id: int
    repeat: int
    tokens: int
    total_s: float
    ttft_s: float
    decode_s: float
    tok_s: float
    rss_mb: float
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "repeat": self.repeat,
            "tokens": self.tokens,
            "total_s": round(_finite(self.total_s), 4),
            "ttft_s": round(_finite(self.ttft_s), 4),
            "decode_s": round(_finite(self.decode_s), 4),
            "tok_s": round(_finite(self.tok_s), 3),
            "rss_mb": round(_finite(self.rss_mb), 1),
            "output": self.output,
        }


@dataclass
class DeviceResult:
    """单设备汇总。`error` 非空表示这台设备整个没跑起来。"""

    device: str
    config: dict = field(default_factory=dict)
    load_s: float = 0.0
    warmup_s: float | None = None
    runs: list[RunResult] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.runs)

    @property
    def avg_tok_s(self) -> float:
        return _mean([r.tok_s for r in self.runs])

    @property
    def avg_ttft_s(self) -> float:
        return _mean([r.ttft_s for r in self.runs])

    @property
    def avg_total_s(self) -> float:
        return _mean([r.total_s for r in self.runs])

    @property
    def avg_tokens(self) -> float:
        return _mean([float(r.tokens) for r in self.runs])

    @property
    def peak_rss_mb(self) -> float:
        return round(max([_finite(r.rss_mb) for r in self.runs], default=0.0), 1)

    @property
    def min_tok_s(self) -> float:
        return round(min([_finite(r.tok_s) for r in self.runs], default=0.0), 2)

    @property
    def max_tok_s(self) -> float:
        return round(max([_finite(r.tok_s) for r in self.runs], default=0.0), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "ok": self.ok,
            "config": {k: display_path(v) for k, v in self.config.items()},
            "load_s": round(_finite(self.load_s), 3),
            "warmup_s": None if self.warmup_s is None else round(_finite(self.warmup_s), 3),
            "avg_tok_s": self.avg_tok_s,
            "min_tok_s": self.min_tok_s,
            "max_tok_s": self.max_tok_s,
            "avg_ttft_s": self.avg_ttft_s,
            "avg_total_s": self.avg_total_s,
            "avg_tokens": self.avg_tokens,
            "peak_rss_mb": self.peak_rss_mb,
            "error": self.error,
            "warnings": self.warnings,
            "runs": [r.to_dict() for r in self.runs],
        }


@dataclass
class BenchReport:
    """一次基准的完整结果（可直接 `to_json()` 存盘）。"""

    env: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    devices: list[DeviceResult] = field(default_factory=list)

    @property
    def ok_devices(self) -> list[DeviceResult]:
        return [d for d in self.devices if d.ok]

    @property
    def failed_devices(self) -> list[DeviceResult]:
        return [d for d in self.devices if not d.ok]

    def fastest(self) -> DeviceResult | None:
        """吞吐最高的设备（D3 默认设备决策的直接依据）。"""
        oks = self.ok_devices
        return max(oks, key=lambda d: d.avg_tok_s) if oks else None

    def lowest_ttft(self) -> DeviceResult | None:
        oks = [d for d in self.ok_devices if d.avg_ttft_s > 0]
        return min(oks, key=lambda d: d.avg_ttft_s) if oks else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "env": self.env,
            "model": display_path(self.model),
            "settings": self.settings,
            "devices": [d.to_dict() for d in self.devices],
            "summary": {
                "fastest": self.fastest().device if self.fastest() else None,
                "lowest_ttft": self.lowest_ttft().device if self.lowest_ttft() else None,
            },
        }

    def to_json(self, indent: int = 2) -> str:
        # ensure_ascii=False：报告里有中文语种名与译文样本
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------- 默认工厂（可被单测替换）
def _make_pipeline(model_path: str, device: str, **config: Any):
    """默认管道工厂：钉死设备，**不走回退链**（见模块 docstring 第 1 条）。"""
    import openvino_genai as ov_genai

    return ov_genai.LLMPipeline(model_path, device, **config)


def _make_gen_config(max_new_tokens: int):
    """默认生成配置：与翻译路径**同一套**参数（config.py），否则基准不具代表性。"""
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


def _rss_mb() -> float:
    """当前进程 RSS（MB）。跨平台走 psutil，拿不到就 0。"""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1048576
    except Exception:  # noqa: BLE001 - 内存是辅助指标，缺了不影响基准
        return 0.0


# ---------------------------------------------------------------- 单次测量
def _measure(pipe: Any, prompt: str, gen: Any, prompt_id: int, repeat: int) -> RunResult:
    """跑一次生成，用 streamer 拿 TTFT 与 token 数。

    streamer 是**同步回调**：每个 subword 调一次，返回 False 表示继续生成。
    `generate()` 会阻塞到生成结束，所以 TTFT 与总耗时都能在一个闭包里量完。
    """
    first_at: float | None = None
    n_tokens = 0

    def streamer(_subword: str) -> bool:
        nonlocal first_at, n_tokens
        if first_at is None:
            first_at = time.perf_counter()
        n_tokens += 1
        return False

    t0 = time.perf_counter()
    text = pipe.generate(prompt, gen, streamer)
    t_end = time.perf_counter()

    total = t_end - t0
    ttft = (first_at - t0) if first_at is not None else float("nan")
    decode = (t_end - first_at) if first_at is not None else float("nan")
    # 与 scripts/bench.py 口径一致：扣除第一个 token 的解码时间
    tps = (n_tokens - 1) / decode if decode and decode > 0 and n_tokens > 1 else 0.0
    return RunResult(
        prompt_id=prompt_id,
        repeat=repeat,
        tokens=n_tokens,
        total_s=total,
        ttft_s=ttft,
        decode_s=decode,
        tok_s=tps,
        rss_mb=_rss_mb(),
        output=str(text).strip()[:120],
    )


# ---------------------------------------------------------------- 主入口
def run_benchmark(
    devices: Sequence[str] | None = None,
    *,
    model_path: str | None = None,
    prompts: Sequence[str] | None = None,
    repeats: int | None = None,
    max_new_tokens: int | None = None,
    warmup: bool | None = None,
    cpu_props: dict | None = None,
    manager: DeviceManager | None = None,
    pipeline_factory: Callable[..., Any] | None = None,
    gen_config_factory: Callable[[int], Any] | None = None,
    on_event: Callable[[str], None] | None = None,
    with_env: bool = True,
) -> BenchReport:
    """跑基准，返回 `BenchReport`。**单台设备失败不会中断其余设备**。

    :param devices: 设备列表；`None` 时按 `select_devices("auto")` 自动挑
    :param repeats: 每个 prompt 重复次数（默认 `cfg.BENCH_REPEATS`）
    :param max_new_tokens: 生成上限（默认 `cfg.BENCH_MAX_NEW_TOKENS`，NPU 上须 ≤ `MIN_RESPONSE_LEN`）
    :param warmup: 是否先跑一次不计入统计的生成（默认 `cfg.BENCH_WARMUP`）
    :param pipeline_factory: `(model_path, device, **config) -> pipe`，注入用
    :param gen_config_factory: `(max_new_tokens) -> gen_config`，注入用
    :param on_event: 进度 / 警告回调（CLI 打到 stderr）
    :param with_env: 关掉可跳过环境指纹采集（单测里省掉 import openvino）
    """
    model = model_path or cfg.MODEL_PATH
    prompt_list = list(prompts) if prompts else build_prompts()
    reps = int(repeats if repeats is not None else cfg.BENCH_REPEATS)
    reps = max(1, reps)
    tokens = int(max_new_tokens if max_new_tokens is not None else cfg.BENCH_MAX_NEW_TOKENS)
    do_warmup = bool(cfg.BENCH_WARMUP if warmup is None else warmup)

    make_pipe = pipeline_factory or _make_pipeline
    make_gen = gen_config_factory or _make_gen_config
    dev_list = list(devices) if devices else select_devices("auto", manager)

    def emit(msg: str) -> None:
        if on_event:
            on_event(msg)

    report = BenchReport(
        env=environment_info() if with_env else {},
        model=model,
        settings={
            "repeats": reps,
            "max_new_tokens": tokens,
            "warmup": do_warmup,
            "prompts": len(prompt_list),
            "devices": list(dev_list),
        },
    )

    for dev in dev_list:
        config = pipeline_config_for(dev, cpu_props)
        entry = DeviceResult(device=dev, config=config)
        report.devices.append(entry)

        if dev.upper().startswith("NPU") and tokens > cfg.MIN_RESPONSE_LEN:
            msg = (f"{dev}: max_new_tokens={tokens} 超过 MIN_RESPONSE_LEN={cfg.MIN_RESPONSE_LEN}，"
                   f"超窗会被静默截断（SPEC.md · NPU 实现要点）")
            entry.warnings.append(msg)
            emit("警告: " + msg)

        emit(f"[{dev}] 载入模型（首次需编译，NPU 约 30 秒）...")
        try:
            t0 = time.perf_counter()
            pipe = make_pipe(model, dev, **config)
            entry.load_s = time.perf_counter() - t0
            emit(f"[{dev}] 载入耗时 {entry.load_s:.2f}s")
        except Exception as exc:  # noqa: BLE001 - 单台设备失败不能带走整轮基准
            entry.error = f"{type(exc).__name__}: {exc}"
            emit(f"[{dev}] 加载失败: {entry.error}")
            continue

        gen = make_gen(tokens)

        if do_warmup:
            try:
                t_w = time.perf_counter()
                pipe.generate(prompt_list[0], gen)
                entry.warmup_s = time.perf_counter() - t_w
                emit(f"[{dev}] 预热耗时 {entry.warmup_s:.2f}s")
            except Exception as exc:  # noqa: BLE001
                entry.error = f"{type(exc).__name__}: {exc}"
                emit(f"[{dev}] 预热失败: {entry.error}")
                continue

        total_runs = reps * len(prompt_list)
        done = 0
        try:
            for rep in range(reps):
                for pid, prompt in enumerate(prompt_list):
                    entry.runs.append(_measure(pipe, prompt, gen, pid, rep))
                    done += 1
                    emit(f"[{dev}] {done}/{total_runs} "
                         f"{entry.runs[-1].tok_s:.2f} tok/s")
        except Exception as exc:  # noqa: BLE001 - 中途炸了也保留已测到的样本
            entry.error = f"{type(exc).__name__}: {exc}"
            emit(f"[{dev}] 推理失败: {entry.error}")

        if entry.runs:
            emit(f"[{dev}] 平均 {entry.avg_tok_s:.2f} tok/s | "
                 f"TTFT {entry.avg_ttft_s:.3f}s | 峰值 RSS {entry.peak_rss_mb:.0f} MB")
    return report


# ---------------------------------------------------------------- 渲染
def render_text(report: BenchReport) -> str:
    """人读的报告（CLI 默认输出）。"""
    env = report.env or {}
    st = report.settings or {}
    lines = [
        "nputr benchmark" + (f"  {env['time']}" if env.get("time") else ""),
        f"模型: {display_path(report.model)}",
    ]
    if env:
        lines.append(
            f"环境: {env.get('platform', '?')} | CPU {env.get('cpu_count', '?')} 核 | "
            f"Python {env.get('python', '?')} | OpenVINO {env.get('openvino', '?')}"
        )
        npu = env.get("npu")
        if isinstance(npu, dict) and npu.get("available"):
            lines.append(
                f"NPU: {npu.get('FULL_DEVICE_NAME', '?')} | arch={npu.get('DEVICE_ARCHITECTURE', '?')} "
                f"驱动={npu.get('NPU_DRIVER_VERSION', '?')} tiles={npu.get('NPU_MAX_TILES', '?')}"
            )
    lines.append(
        f"设置: 重复 {st.get('repeats', '?')} 次 × {st.get('prompts', '?')} prompt | "
        f"max_new_tokens={st.get('max_new_tokens', '?')} | 预热={st.get('warmup', '?')}"
    )
    lines.append("")

    # 波动列不是凑数：本机两轮实测 GPU 37.6 / 25.4 tok/s（±30%），NPU 32.45 / 32.70（±0.8%）。
    # 只给平均值会把"这台设备的数字能不能信"这个最关键的信息藏掉。
    header = (f"{'设备':<10}{'加载s':>9}{'预热s':>9}{'tok/s':>10}{'区间':>14}"
              f"{'TTFT s':>10}{'总耗时s':>10}{'tokens':>9}{'峰值RSS MB':>12}")
    lines.append(header)
    lines.append("-" * 93)

    for d in report.devices:
        if not d.ok:
            lines.append(f"{d.device:<10}{'—':>9}{'—':>9}{'失败':>10}{'—':>14}  {d.error or ''}")
            continue
        warm = f"{d.warmup_s:.2f}" if d.warmup_s is not None else "—"
        spread = f"{d.min_tok_s:.1f}~{d.max_tok_s:.1f}"
        lines.append(
            f"{d.device:<10}{d.load_s:>9.2f}{warm:>9}{d.avg_tok_s:>10.2f}{spread:>14}"
            f"{d.avg_ttft_s:>10.3f}{d.avg_total_s:>10.3f}{d.avg_tokens:>9.1f}{d.peak_rss_mb:>12.0f}"
        )

    oks = report.ok_devices
    if len(oks) > 1:
        lines.append(
            "注: 串行测多设备时 RSS 是**累加值**（前一台的内存未必马上归还），"
            "单设备常驻请看单独跑 -d 的数字"
        )
    if oks:
        lines.append("")
        fast = report.fastest()
        low = report.lowest_ttft()
        if fast:
            lines.append(f"吞吐最快: {fast.device} {fast.avg_tok_s:.2f} tok/s")
        if low:
            lines.append(f"TTFT 最短: {low.device} {low.avg_ttft_s:.3f} s")
        if fast and len(oks) > 1:
            others = [d for d in oks if d is not fast]
            slow = min(others, key=lambda d: d.avg_tok_s)
            if slow.avg_tok_s > 0:
                lines.append(f"对比: 比最慢的 {slow.device} 快 {fast.avg_tok_s / slow.avg_tok_s:.2f}×")
            noisy = max(oks, key=lambda d: d.max_tok_s - d.min_tok_s)
            if noisy.max_tok_s > 0 and noisy.max_tok_s - noisy.min_tok_s > 0.2 * noisy.max_tok_s:
                lines.append(
                    f"⚠️ {noisy.device} 波动 {noisy.min_tok_s:.1f}~{noisy.max_tok_s:.1f} tok/s"
                    f"（>20%），单次结果不可信，多跑几轮再下结论"
                )

    warns = [w for d in report.devices for w in d.warnings]
    if warns:
        lines.append("")
        for w in warns:
            lines.append(f"警告: {w}")

    failed = report.failed_devices
    if failed:
        lines.append("")
        lines.append("失败设备: " + ", ".join(
            f"{d.device}({(d.error or '无样本')[:60]})" for d in failed
        ))
    return "\n".join(lines) + "\n"
