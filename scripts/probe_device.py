"""M0 环境探针：验证 OpenVINO 可用设备与 NPU 状态。

用法:
    .\\.venv\\Scripts\\python.exe scripts\\probe_device.py
"""
from __future__ import annotations

import platform
import sys


def main() -> int:
    print("=" * 60)
    print("M0 环境探针")
    print("=" * 60)
    print(f"Python     : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"Platform   : {platform.platform()}")

    try:
        import openvino as ov
    except ImportError as exc:  # pragma: no cover
        print(f"[FAIL] import openvino 失败: {exc}")
        return 1

    print(f"OpenVINO   : {ov.__version__}")

    try:
        import openvino_genai as ov_genai

        print(f"GenAI      : {ov_genai.__version__}")
    except ImportError as exc:
        print(f"[WARN] import openvino_genai 失败: {exc}")

    core = ov.Core()
    print("-" * 60)
    print("可用设备:")

    npu_found = False
    for device in core.available_devices:
        try:
            name = core.get_property(device, "FULL_DEVICE_NAME")
        except Exception:
            name = "<unknown>"
        try:
            arch = core.get_property(device, "DEVICE_ARCHITECTURE")
        except Exception:
            arch = ""
        print(f"  - {device:<10} {name}  {arch}")
        if device.upper().startswith("NPU"):
            npu_found = True

    print("-" * 60)
    if not npu_found:
        print("[FAIL] 未检测到 NPU 设备")
        return 2

    # NPU 详细信息
    for prop in (
        "FULL_DEVICE_NAME",
        "DEVICE_ARCHITECTURE",
        "NPU_DEVICE_ID",
        "NPU_DRIVER_VERSION",
        "NPU_COMPILATION_MODE_PARAMS",
        "NPU_MAX_TILES",
        "DEVICE_UUID",
    ):
        try:
            value = core.get_property("NPU", prop)
        except Exception as exc:
            value = f"N/A ({type(exc).__name__})"
        print(f"NPU {prop:<28}: {value}")

    print("-" * 60)
    print("[OK] NPU 设备可见")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
