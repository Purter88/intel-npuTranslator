"""本地导出 OpenVINO IR + INT4 量化（模型）。

⚠️ 绝大多数情况**不需要**跑这个脚本：现成权重 rainhenry/HY-MT1.5-1.8B-int4-ov-npu
   已 INT4 量化且 NPU 就绪，直接用 scripts/fetch_model.py 拉取即可。
   只有换模型或换量化方式时才需要本地导出。

必须用**独立的导出 venv**（requirements-export.txt），不要装进运行时 .venv：
optimum-intel / transformers / torch 与 openvino-genai 版本强绑定，混装会冲突。

用法:
    python -m venv .venv-export
    .\\.venv-export\\Scripts\\python.exe -m pip install -r requirements-export.txt
    .\\.venv-export\\Scripts\\python.exe scripts\\export_model.py
    .\\.venv-export\\Scripts\\python.exe scripts\\export_model.py --model tencent/HY-MT1.5-1.8B --group-size -1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def export(model: str, out_dir: str, weight_format: str, group_size: int, ratio: float, sym: bool) -> int:
    target = ROOT / out_dir
    target.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "optimum.exporters.openvino",
        "--model", model,
        "--task", "text-generation-with-past",
        "--weight-format", weight_format,
        "--ratio", str(ratio),
        "--group-size", str(group_size),
        "--trust-remote-code",
        str(target),
    ]
    if sym:
        cmd.append("--sym")

    print("[导出] " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("[FAIL] 导出失败", file=sys.stderr)
        return result.returncode
    print(f"[OK] 已导出到 {target}")
    print(f"     genai 加载方式: LLMPipeline(r'{target}', 'NPU', **npu_pipeline_config())")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="tencent/HY-MT1.5-1.8B", help="HF/ModelScope 模型 ID 或本地路径")
    ap.add_argument("--out", default="models/HY-MT1.5-1.8B-int4-gq128-ov")
    ap.add_argument("--weight-format", default="int4", choices=["int4", "int8", "fp16"])
    ap.add_argument("--group-size", type=int, default=128, help="128=分组量化(精度好)；-1=通道量化(性能好)")
    ap.add_argument("--ratio", type=float, default=1.0)
    ap.add_argument("--no-sym", action="store_true", help="关闭对称量化（NPU 官方要求 --sym，默认开启）")
    args = ap.parse_args()

    return export(args.model, args.out, args.weight_format, args.group_size, args.ratio, not args.no_sym)


if __name__ == "__main__":
    raise SystemExit(main())
