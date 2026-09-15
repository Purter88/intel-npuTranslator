"""性能基准：NPU / GPU / CPU 三设备对比。

M0 阶段核心交付物：输出 TTFT、tok/s、峰值内存、编译耗时，
作为「NPU 到底能不能用」的决策依据（风险 R1）。

用法:
    .\\.venv\\Scripts\\python.exe scripts\\bench.py --model models/HY-MT1.5-1.8B-int4-ov-npu --device NPU
    .\\.venv\\Scripts\\python.exe scripts\\bench.py --all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from npu_translator.benchmark import DEFAULT_PROMPTS as BENCH_PROMPTS  # noqa: E402

# ★ 与 npu_translator/benchmark.py 共用同一份 prompt 集：
#   两侧口径必须一致，否则 docs/bench_all.json 的 M0 基线就没法跟 `nputr -b` 的数据比。


def build_pipeline_config(device: str, max_prompt_len: int, min_response_len: int, cache_dir: str) -> dict:
    """按设备构造流水线配置。NPU 需要静态形状 + 编译缓存。"""
    if device.upper() != "NPU":
        return {}
    return {
        "MAX_PROMPT_LEN": max_prompt_len,
        "MIN_RESPONSE_LEN": min_response_len,
        "NPUW_CACHE_DIR": str(ROOT / cache_dir),
        "GENERATE_HINT": "BEST_PERF",
    }


def measure(model_path: str, device: str, max_prompt_len: int, min_response_len: int,
            cache_dir: str, max_new_tokens: int, warmup: bool) -> dict:
    import openvino_genai as ov_genai

    cfg = build_pipeline_config(device, max_prompt_len, min_response_len, cache_dir)
    print(f"\n{'=' * 70}")
    print(f"设备: {device}   模型: {model_path}")
    print(f"配置: {cfg if cfg else '<默认>'}")
    print("=" * 70)

    t0 = time.perf_counter()
    pipe = ov_genai.LLMPipeline(model_path, device, **cfg) if cfg else ov_genai.LLMPipeline(model_path, device)
    load_s = time.perf_counter() - t0
    print(f"加载/编译耗时: {load_s:.2f} s")

    gen_config = ov_genai.GenerationConfig()
    gen_config.max_new_tokens = max_new_tokens
    gen_config.temperature = 0.0
    gen_config.top_p = 1.0
    gen_config.do_sample = False
    gen_config.repetition_penalty = 1.05

    if warmup:
        print("预热中(触发 NPU 编译)...")
        t_w = time.perf_counter()
        pipe.generate(BENCH_PROMPTS[0], gen_config)
        print(f"预热耗时: {time.perf_counter() - t_w:.2f} s")

    results = []
    tracemalloc.start()
    for i, prompt in enumerate(BENCH_PROMPTS, 1):
        first_token_at = None
        n_tokens = 0
        t_start = time.perf_counter()

        def streamer(subword: str):
            nonlocal first_token_at, n_tokens
            if first_token_at is None:
                first_token_at = time.perf_counter()
            n_tokens += 1
            return False

        gen_config.max_new_tokens = max_new_tokens
        text = pipe.generate(prompt, gen_config, streamer)
        t_end = time.perf_counter()

        total = t_end - t_start
        ttft = (first_token_at - t_start) if first_token_at else float("nan")
        decode = t_end - first_token_at if first_token_at else float("nan")
        tps = (n_tokens - 1) / decode if decode and decode > 0 and n_tokens > 1 else float("nan")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        r = {
            "id": i,
            "tokens": n_tokens,
            "total_s": round(total, 3),
            "ttft_s": round(ttft, 3),
            "decode_s": round(decode, 3),
            "tok_s": round(tps, 2),
            "peak_py_mem_mb": round(peak / 1024 / 1024, 1),
            "output": text.strip()[:120],
        }
        results.append(r)
        print(f"  [{i}] tok={n_tokens:<4} ttft={r['ttft_s']:>7.3f}s "
              f"total={r['total_s']:>7.3f}s  tps={r['tok_s']:>7.2f}  | {r['output']}")
    tracemalloc.stop()

    valid = [r["tok_s"] for r in results if r["tok_s"] == r["tok_s"]]
    summary = {
        "device": device,
        "model": model_path,
        "load_s": round(load_s, 2),
        "config": cfg,
        "avg_tok_s": round(sum(valid) / len(valid), 2) if valid else None,
        "avg_ttft_s": round(sum(r["ttft_s"] for r in results) / len(results), 3) if results else None,
        "results": results,
    }
    print(f"\n>>> {device} 平均: {summary['avg_tok_s']} tok/s | 平均 TTFT: {summary['avg_ttft_s']} s "
          f"| 加载: {summary['load_s']} s")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/HY-MT1.5-1.8B-int4-ov-npu")
    ap.add_argument("--device", default="NPU")
    ap.add_argument("--all", action="store_true", help="依次跑 NPU / GPU / CPU")
    ap.add_argument("--max-prompt-len", type=int, default=1024)
    ap.add_argument("--min-response-len", type=int, default=512)
    ap.add_argument("--cache-dir", default=".npucache")
    ap.add_argument("--max-new-tokens", type=int, default=128)
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--out", default="docs/bench_result.json")
    args = ap.parse_args()

    devices = ["NPU", "GPU", "CPU"] if args.all else [args.device]
    summaries = []
    for dev in devices:
        try:
            summaries.append(
                measure(args.model, dev, args.max_prompt_len, args.min_response_len,
                        args.cache_dir, args.max_new_tokens, not args.no_warmup)
            )
        except Exception as exc:
            print(f"[FAIL] {dev}: {type(exc).__name__}: {exc}")
            summaries.append({"device": dev, "error": f"{type(exc).__name__}: {exc}"})

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入: {out_path}")

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    for s in summaries:
        if "error" in s:
            print(f"  {s['device']:<6} 失败: {s['error'][:80]}")
        else:
            print(f"  {s['device']:<6} {str(s['avg_tok_s']):>8} tok/s   "
                  f"TTFT {s['avg_ttft_s']} s   加载 {s['load_s']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
