"""全局配置：所有写死的常量集中在此，可通过环境变量覆盖。

约定（执行约定）：模型路径、MAX_PROMPT_LEN、量化参数等常量
只允许出现在本文件，业务代码一律从这里取。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------- 路径
MODEL_DIR = ROOT / os.getenv("NPT_MODEL_DIR", "models")
DEFAULT_MODEL = os.getenv("NPT_MODEL", "HY-MT1.5-1.8B-int4-ov-npu")
MODEL_PATH = str(MODEL_DIR / DEFAULT_MODEL)

CACHE_DIR = ROOT / os.getenv("NPT_CACHE_DIR", ".npucache")

# ---------------------------------------------------------------- 设备
# auto | npu | gpu | cpu
DEVICE = os.getenv("NPT_DEVICE", "npu")
# auto 模式下：若 NPU 基准低于 CPU 则自动选 CPU
AUTO_FALLBACK_ON_SLOW_NPU = _env_bool("NPT_AUTO_FALLBACK_ON_SLOW_NPU", False)

# ---------------------------------------------------------------- NPU 静态形状
# KV cache 总容量 = MAX_PROMPT_LEN + MIN_RESPONSE_LEN
# ★ 实测甜点（2026-09-11，见实测基线）：512+256 相比 1024+512
#   TTFT 1.247s -> 0.591s（-53%），吞吐 29.83 -> 32.41 tok/s（+9%）
#   代价是 KV cache 上限 768，因此长文本必须靠分段（SEGMENT_MAX_TOKENS）规避
MAX_PROMPT_LEN = _env_int("NPT_MAX_PROMPT_LEN", 512)
MIN_RESPONSE_LEN = _env_int("NPT_MIN_RESPONSE_LEN", 256)
GENERATE_HINT = os.getenv("NPT_GENERATE_HINT", "BEST_PERF")
PREFILL_CHUNK_SIZE = _env_int("NPT_PREFILL_CHUNK_SIZE", 1024)

# ---------------------------------------------------------------- 量化（导出用）
WEIGHT_FORMAT = os.getenv("NPT_WEIGHT_FORMAT", "int4")
GROUP_SIZE = _env_int("NPT_GROUP_SIZE", 128)  # -1 = 通道量化
RATIO = float(os.getenv("NPT_RATIO", "1.0"))
SYM = _env_bool("NPT_SYM", True)

# ---------------------------------------------------------------- 生成参数
TEMPERATURE = float(os.getenv("NPT_TEMPERATURE", "0.0"))
TOP_P = float(os.getenv("NPT_TOP_P", "1.0"))
TOP_K = _env_int("NPT_TOP_K", 0)
REPETITION_PENALTY = float(os.getenv("NPT_REPETITION_PENALTY", "1.05"))
DO_SAMPLE = _env_bool("NPT_DO_SAMPLE", False)
MAX_NEW_TOKENS_CAP = _env_int("NPT_MAX_NEW_TOKENS_CAP", 2048)

# ---------------------------------------------------------------- 服务
HOST = os.getenv("NPT_HOST", "127.0.0.1")
PORT = _env_int("NPT_PORT", 8765)
LRU_CACHE_SIZE = _env_int("NPT_LRU_CACHE_SIZE", 512)

# ---------------------------------------------------------------- 分段
SEGMENT_MAX_CHARS = _env_int("NPT_SEGMENT_MAX_CHARS", 512)
SEGMENT_MAX_TOKENS = _env_int("NPT_SEGMENT_MAX_TOKENS", 256)

# ---------------------------------------------------------------- 长尾语种通道（v1 不启用）
ENABLE_NLLB_FALLBACK = _env_bool("NPT_ENABLE_NLLB_FALLBACK", False)
NLLB_MODEL = os.getenv("NPT_NLLB_MODEL", "")

# ---------------------------------------------------------------- 基准（-b / --benchmark）
# 跨平台可比性靠"固定口径"：固定 prompt 集（benchmark.DEFAULT_PROMPTS）+ 固定重复次数。
# 改这两个值等于改基线口径，跨机器 / 跨版本数据就不可比了。
BENCH_REPEATS = _env_int("NPT_BENCH_REPEATS", 3)
BENCH_MAX_NEW_TOKENS = _env_int("NPT_BENCH_MAX_NEW_TOKENS", 128)
# 预热那一次不计入统计（NPU 首次推理含编译，混进来会把平均值拉爆）
BENCH_WARMUP = _env_bool("NPT_BENCH_WARMUP", True)

# ---------------------------------------------------------------- 退出行为（CLI 管道契约）
# ★ 硬退出：flush 之后直接 os._exit，绕开解释器关停阶段。
#   背景（2026-09-12 实测，见踩坑记录）：译文输出完毕后进程里仍挂着
#   OpenVINO 的 daemon 线程（ThreadPoolExecutor），CPython 关停时会把它 join 掉；
#   一旦该线程被原生调用卡住（NPU 被占用 / 驱动态异常），进程就卡在终端不退。
#   关掉它可以对比"是否真是关停阶段卡住"，但默认必须开。
HARD_EXIT = _env_bool("NPT_HARD_EXIT", True)

# 关停诊断：退出前把仍存活的线程列表打到 stderr。
# 再出现"输出完但不退出"时开着它跑一次，直接拿到证据而不是猜。
EXIT_DEBUG = _env_bool("NPT_EXIT_DEBUG", False)

# 预热等待上限（秒）。0 = 无限等待 —— **不推荐**：NPU 被别的进程占用时（R9）
# 会永久挂起且零输出，看起来跟"卡死"一模一样。
WARMUP_TIMEOUT = _env_int("NPT_WARMUP_TIMEOUT", 300)


def resolve_model_path(value: str | None) -> str:
    """把「模型名或路径」解析成模型目录路径（供 CLI `--model` 用）。

    两种写法都收，跟 `NPT_MODEL` 的语义保持一致：

    - **纯名字**（不含分隔符、非绝对）→ 当作 `MODEL_DIR` 下的子目录
      （`--model Qwen3-1.7B-int4-ov` → `models\\Qwen3-1.7B-int4-ov`）
    - **含分隔符或绝对路径** → 原样使用（相对当前工作目录）

    :param value: 空 / `None` 表示未指定，回落到 `MODEL_PATH`（含 `NPT_MODEL` 环境变量的结果）
    """
    raw = (value or "").strip()
    if not raw:
        return MODEL_PATH
    candidate = Path(raw)
    if candidate.is_absolute() or os.sep in raw or "/" in raw:
        return str(candidate)
    return str(MODEL_DIR / raw)


def available_models() -> list[str]:
    """`MODEL_DIR` 下已下载的模型目录名（给 `--model` 打错时的提示用）。

    只看一层子目录，模型权重都落在 `models/<名字>/` 这种形态里。
    """
    try:
        if not MODEL_DIR.is_dir():
            return []
        return sorted(p.name for p in MODEL_DIR.iterdir() if p.is_dir())
    except OSError:
        return []


def npu_pipeline_config() -> dict:
    """NPU 流水线配置（NPU 实现要点）。"""
    return {
        "MAX_PROMPT_LEN": MAX_PROMPT_LEN,
        "MIN_RESPONSE_LEN": MIN_RESPONSE_LEN,
        "NPUW_CACHE_DIR": str(CACHE_DIR),
        "GENERATE_HINT": GENERATE_HINT,
    }
