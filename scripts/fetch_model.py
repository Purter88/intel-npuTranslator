"""模型获取：优先 ModelScope（国内），失败回退 HF 镜像。

用法:
    # 已量化的 NPU 就绪权重（推荐，省去导出）
    python scripts/fetch_model.py --repo rainhenry/HY-MT1.5-1.8B-int4-ov-npu
    # 原始模型（用于本地导出量化）
    python scripts/fetch_model.py --repo tencent/HY-MT1.5-1.8B
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def download(repo: str, local_dir: str, source: str = "modelscope") -> int:
    target = ROOT / local_dir
    target.mkdir(parents=True, exist_ok=True)

    if source == "modelscope":
        from modelscope import snapshot_download

        print(f"[ModelScope] {repo} -> {target}")
        path = snapshot_download(repo, local_dir=str(target))
        print(f"[OK] 下载完成: {path}")
        return 0

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from huggingface_hub import snapshot_download

    print(f"[HF mirror] {repo} -> {target}")
    path = snapshot_download(
        repo_id=repo,
        local_dir=str(target),
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.py"],
    )
    print(f"[OK] 下载完成: {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="rainhenry/HY-MT1.5-1.8B-int4-ov-npu")
    ap.add_argument("--local-dir", default=None, help="默认取 repo 的最后一段")
    ap.add_argument("--source", default="modelscope", choices=["modelscope", "hf"])
    args = ap.parse_args()

    local_dir = args.local_dir or args.repo.split("/")[-1]
    try:
        return download(args.repo, f"models/{local_dir}", args.source)
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
