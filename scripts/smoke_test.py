"""冒烟测试：5 语种 × 3 句，验证翻译正确且无多余解释。

用法:
    python scripts/smoke_test.py --device NPU
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from npu_translator import config as cfg  # noqa: E402
from npu_translator.languages import en_name  # noqa: E402
from npu_translator.prompt import build  # noqa: E402

CASES = [
    ("zh", "en", [
        "今天天气很好,我们一起去公园散步吧。",
        "这个项目的目标是构建一个完全离线的本地翻译程序。",
        "请确保所有数据都留在这台机器上。",
    ]),
    ("en", "zh", [
        "The weather is nice today, let's go for a walk in the park.",
        "Artificial intelligence is profoundly changing the way we live.",
        "Please make sure all data stays on this machine.",
    ]),
    ("zh", "ja", [
        "你好,很高兴认识你。",
        "这是一份关于人工智能发展的报告。",
        "请把这份文件翻译成日语。",
    ]),
    ("en", "ko", [
        "Good morning, how are you today?",
        "The meeting has been rescheduled to next Monday.",
        "Thank you for your hard work.",
    ]),
    ("zh", "fr", [
        "我来自中国,很高兴来到这里。",
        "这份合同需要在明天之前签署。",
        "祝您旅途愉快。",
    ]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="NPU")
    ap.add_argument("--model", default=cfg.MODEL_PATH)
    ap.add_argument("--max-new-tokens", type=int, default=200)
    ap.add_argument("--out", default="docs/smoke_result.json")
    args = ap.parse_args()

    import openvino_genai as ov_genai

    pipe_cfg = cfg.npu_pipeline_config() if args.device.upper() == "NPU" else {}
    t0 = time.perf_counter()
    pipe = ov_genai.LLMPipeline(args.model, args.device, **pipe_cfg)
    load_s = time.perf_counter() - t0
    print(f"设备 {args.device} 加载耗时 {load_s:.2f} s\n")

    gen = ov_genai.GenerationConfig()
    gen.max_new_tokens = args.max_new_tokens
    gen.temperature = cfg.TEMPERATURE
    gen.top_p = cfg.TOP_P
    gen.do_sample = cfg.DO_SAMPLE
    gen.repetition_penalty = cfg.REPETITION_PENALTY

    passed = failed = 0
    records = []
    for src, tgt, sentences in CASES:
        print(f"--- {src} -> {tgt} ({en_name(tgt)}) ---")
        for s in sentences:
            prompt = build(s, en_name(tgt), source_lang=src)
            t1 = time.perf_counter()
            out = pipe.generate(prompt, gen).strip()
            dt = time.perf_counter() - t1
            ok = bool(out) and out != s
            passed += ok
            failed += not ok
            records.append({
                "src": src, "tgt": tgt, "input": s, "output": out,
                "ok": ok, "elapsed_s": round(dt, 3),
            })
            print(f"  [{'OK ' if ok else 'BAD'}] {dt:6.2f}s  {s}")
            print(f"        -> {out}")
        print()

    print(f"通过 {passed} / {passed + failed}")
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入: {out_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
