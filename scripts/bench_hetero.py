"""异构并行基准（CLI 管道契约）。

要回答两个问题：

1. **动态派活的加速比是否 ≥1.15×**（CLI 管道契约）。不达标则 hetero 降为纯手动开关，
   不进推荐路径。对比基线是**本机最快的单设备**（实测是 CPU，不是 NPU）。
2. **`--cpu-threads` 的取值扫描**（活跃风险 R13）：0 / 8 / 12 / 16 / 24 各跑一遍。
   结果只写进 `docs/` 与 README 作调优建议，**不做默认值**——本机结论不可外推。

每种模式跑在**独立子进程**里：两条 pipeline 共存要 4 GB，串行跑完再释放，
免得内存基线把后一轮的数字带偏。

用法::

    python scripts/bench_hetero.py --all --out docs/bench_hetero.json
    python scripts/bench_hetero.py --mode hetero
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from npu_translator import config as cfg  # noqa: E402
from npu_translator.pool import TranslationPool, build_workers, cpu_pipeline_props  # noqa: E402

# 与 2026-09-12 静态分配那次实测保持同一组负载，数字才有可比性
SEGMENTS = [
    "今天天气不错，我们决定去公园散步，顺便在路边的咖啡店买两杯拿铁。",
    "人工智能技术正在深刻改变软件开发的方式，程序员需要不断学习新的工具和方法。",
    "The quick brown fox jumps over the lazy dog while the sun sets behind the mountains.",
    "这份报告详细分析了过去三年里全球半导体供应链的结构性变化与未来趋势。",
    "请确保在提交代码之前运行完整的单元测试，并检查所有的边界条件是否处理正确。",
    "Machine translation quality has improved dramatically with the advent of large language models.",
]

DEFAULT_SWEEP = "0,8,12,16,24"


def _rss_mb() -> float:
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def run_one(mode: str, threads: str, max_new_tokens: int, model: str | None,
            core_type: str = "any") -> dict:
    """跑一种配置，返回指标字典。"""
    cpu_props = cpu_pipeline_props(threads, core_type)
    workers = build_workers(mode, cpu_props=cpu_props, max_new_tokens_cap=max_new_tokens,
                            model_path=model)
    pool = TranslationPool(workers)

    t0 = time.perf_counter()
    pool.prepare()
    load_s = time.perf_counter() - t0
    rss_loaded = _rss_mb()

    t0 = time.perf_counter()
    results = pool.run(SEGMENTS, "en")
    infer_s = time.perf_counter() - t0
    rss_peak = _rss_mb()

    chars = sum(len(r.text) for r in results)
    failed = sum(1 for r in results if not r.ok)

    per_device: dict[str, dict] = {}
    for r in results:
        d = per_device.setdefault(r.device or "?", {"segments": 0, "elapsed_s": 0.0, "chars": 0})
        d["segments"] += 1
        d["elapsed_s"] = round(d["elapsed_s"] + r.elapsed_s, 3)
        d["chars"] += len(r.text)

    return {
        "mode": mode,
        "threads": threads,
        "core_type": core_type,
        "workers": [w.name for w in pool.workers],
        "max_new_tokens": max_new_tokens,
        "segments": len(SEGMENTS),
        "load_s": round(load_s, 3),
        "infer_s": round(infer_s, 3),
        "chars": chars,
        "chars_per_s": round(chars / infer_s, 2) if infer_s > 0 else 0.0,
        "rss_after_load_mb": rss_loaded,
        "rss_peak_mb": rss_peak,
        "failed": failed,
        "per_device": per_device,
        "translations": [r.text for r in results],
    }


def _run_in_subprocess(args: list[str]) -> dict | None:
    """子进程跑一种配置，避免多条 pipeline 的内存基线互相污染。"""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print(f"[失败] {' '.join(args)}\n{proc.stderr[-2000:]}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        print(f"[解析失败] {exc}: {proc.stdout[-500:]}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="异构并行基准")
    ap.add_argument("--mode", default="all",
                    choices=["npu", "cpu", "hetero", "sweep", "all"])
    ap.add_argument("--threads", default="0", help="CPU 线程数：0 / half / 正整数")
    ap.add_argument("--core-type", default="any", choices=["any", "pcore", "ecore"])
    ap.add_argument("--sweep", default=DEFAULT_SWEEP, help="线程数扫描列表，逗号分隔")
    ap.add_argument("--max-new-tokens", type=int, default=96)
    ap.add_argument("--model", default=None, help="模型路径，默认 config.MODEL_PATH")
    ap.add_argument("--out", default="docs/bench_hetero.json")
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)  # 子进程用
    args = ap.parse_args()

    if args.json:
        print(json.dumps(run_one(args.mode, args.threads, args.max_new_tokens,
                                 args.model, args.core_type), ensure_ascii=False))
        return 0

    runs: list[dict] = []
    plans: list[list[str]] = []
    if args.mode in {"npu", "all"}:
        plans.append(["--mode", "npu"])
    if args.mode in {"cpu", "all"}:
        plans.append(["--mode", "cpu", "--threads", args.threads,
                      "--core-type", args.core_type])
    if args.mode in {"hetero", "all"}:
        plans.append(["--mode", "hetero", "--threads", args.threads,
                      "--core-type", args.core_type])
    if args.mode in {"sweep", "all"}:
        for t in [x.strip() for x in args.sweep.split(",") if x.strip()]:
            plans.append(["--mode", "cpu", "--threads", t, "--core-type", args.core_type])

    common = ["--max-new-tokens", str(args.max_new_tokens)]
    if args.model:
        common += ["--model", args.model]

    for plan in plans:
        print(f">>> {' '.join(plan)}", flush=True)
        rec = _run_in_subprocess(plan + common)
        if rec:
            runs.append(rec)
            print(f"    {rec['infer_s']:.2f}s  {rec['chars_per_s']:.1f} 字符/s  "
                  f"加载 {rec['load_s']:.2f}s  RSS {rec['rss_peak_mb']:.0f} MB", flush=True)

    if not runs:
        print("没有成功的基准结果", file=sys.stderr)
        return 1

    # 加速比：对比**本机最快的单设备**（不是固定的 NPU）
    singles = [r for r in runs if r["mode"] in {"npu", "cpu"} and r["threads"] == args.threads]
    singles = singles or [r for r in runs if r["mode"] in {"npu", "cpu"}]
    hetero = next((r for r in runs if r["mode"] == "hetero"), None)
    summary: dict = {"runs": runs}
    if hetero and singles:
        best = min(singles, key=lambda r: r["infer_s"])
        speedup = round(best["infer_s"] / hetero["infer_s"], 3) if hetero["infer_s"] else 0.0
        summary["speedup_vs_best_single"] = speedup
        summary["best_single"] = {"mode": best["mode"], "infer_s": best["infer_s"],
                                  "chars_per_s": best["chars_per_s"]}
        summary["hetero"] = {"infer_s": hetero["infer_s"], "chars_per_s": hetero["chars_per_s"]}
        summary["verdict"] = ("达标" if speedup >= 1.15 else "未达标（<1.15×，hetero 降为手动开关）")
        print(f"\n动态派活加速比 vs 最快单设备({best['mode']}): {speedup}×  "
              f"→ {summary['verdict']}", flush=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
