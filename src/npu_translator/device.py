"""设备探测与回退链（SPEC.md · NPU 实现要点）。

回退顺序：NPU → GPU(Intel iGPU) → CPU

⚠️ 注意：OpenVINO 会把 NVIDIA dGPU 也枚举成 `GPU.1`，但它**不走 CUDA 后端**，
    直接拿来推理会失败。因此选 GPU 时必须按 vendor 过滤（0x8086 = Intel）。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# 回退链顺序
FALLBACK_CHAIN = ("NPU", "GPU", "CPU")
INTEL_VENDOR_ID = "0x8086"

# 用户可写的设备别名 -> OpenVINO 设备族
_ALIASES = {
    "auto": "AUTO",
    "npu": "NPU",
    "gpu": "GPU",
    "igpu": "GPU",
    "cpu": "CPU",
}


def normalize(name: str) -> str:
    """把用户写的 `npu` / `AUTO` 之类归一成 OpenVINO 设备族名。"""
    key = (name or "auto").strip().lower()
    return _ALIASES.get(key, name.strip().upper())


class DeviceManager:
    """封装 ov.Core，惰性初始化（构造时不碰 OpenVINO）。"""

    def __init__(self) -> None:
        self._core: Any | None = None

    @property
    def core(self) -> Any:
        if self._core is None:
            import openvino as ov

            self._core = ov.Core()
        return self._core

    # ------------------------------------------------------------ 探测
    def available_devices(self) -> list[str]:
        try:
            return list(self.core.available_devices)
        except Exception as exc:
            logger.warning("枚举设备失败: %s", exc)
            return ["CPU"]

    def device_name(self, device: str) -> str:
        try:
            return str(self.core.get_property(device, "FULL_DEVICE_NAME"))
        except Exception:
            return "<unknown>"

    def is_intel(self, device: str) -> bool:
        """判断是否为 Intel 设备（用于剔除 NVIDIA dGPU）。"""
        try:
            arch = str(self.core.get_property(device, "DEVICE_ARCHITECTURE"))
        except Exception:
            return device.upper() in {"CPU", "NPU"}
        return INTEL_VENDOR_ID in arch or device.upper() in {"CPU", "NPU"}

    def intel_gpu(self) -> str | None:
        """返回第一个 Intel GPU 的具体设备名（GPU.0 之类），没有则 None。"""
        for dev in self.available_devices():
            if dev.upper().startswith("GPU") and self.is_intel(dev):
                return dev
        return None

    def npu_info(self) -> dict[str, Any]:
        """采集 NPU 关键属性；NPU 不可用时返回 {'available': False}。"""
        if "NPU" not in self.available_devices():
            return {"available": False}
        info: dict[str, Any] = {"available": True}
        for prop in (
            "FULL_DEVICE_NAME",
            "DEVICE_ARCHITECTURE",
            "NPU_DRIVER_VERSION",
            "NPU_MAX_TILES",
            "DEVICE_UUID",
        ):
            try:
                info[prop] = self.core.get_property("NPU", prop)
            except Exception as exc:
                info[prop] = f"N/A ({type(exc).__name__})"
        return info

    # ------------------------------------------------------------ 选择
    def resolve(self, preferred: str = "auto") -> str:
        """按回退链选出实际可用的设备名。

        :param preferred: auto | npu | gpu | cpu
        :return: 可直接传给 LLMPipeline 的设备字符串
        """
        wanted = normalize(preferred)
        available = self.available_devices()

        if wanted != "AUTO":
            if wanted == "GPU":
                # GPU 需要落到具体的 Intel 设备，避免撞上 NVIDIA dGPU
                igpu = self.intel_gpu()
                if igpu:
                    logger.info("GPU 回退到 Intel 设备: %s", igpu)
                    return igpu
                logger.warning("无可用 Intel GPU，降级 CPU")
                return "CPU"
            if wanted in available:
                return wanted
            logger.warning("设备 %s 不可用，降级 CPU（可用: %s）", wanted, available)
            return "CPU"

        # auto：沿回退链找第一个可用的
        for family in FALLBACK_CHAIN:
            if family == "GPU":
                igpu = self.intel_gpu()
                if igpu:
                    return igpu
                continue
            if family in available:
                return family
        return "CPU"  # CPU 一定在 available_devices 里


def set_process_priority(level: str = "normal") -> bool:
    """调整**整个进程**的 CPU 优先级（CLI 的 `--cpu-priority`，见 SPEC.md · CLI 管道契约）。

    :param level: `idle` | `below` | `normal`
    :return: 是否真的改了

    ⚠️ 这是进程级的，会连带拖慢 NPU 的宿主侧调度，不只是推理线程。
    `normal` 是默认值，此时什么都不做。
    """
    import os

    key = (level or "normal").strip().lower()
    if key == "normal":
        return False

    import psutil

    proc = psutil.Process()
    if os.name == "nt":
        classes = {
            "idle": getattr(psutil, "IDLE_PRIORITY_CLASS", None),
            "below": getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None),
        }
        cls = classes.get(key)
        if cls is None:
            return False
        proc.nice(cls)
    else:
        proc.nice({"idle": 19, "below": 10}.get(key, 0))
    return True


@lru_cache(maxsize=1)
def _default_manager() -> DeviceManager:
    return DeviceManager()


def resolve_device(preferred: str = "auto", manager: DeviceManager | None = None) -> str:
    """便捷函数：解析出可用的设备名。"""
    return (manager or _default_manager()).resolve(preferred)


def device_report(manager: DeviceManager | None = None) -> str:
    """给人看的多行设备报告（CLI /v1/health 用）。"""
    m = manager or _default_manager()
    lines = []
    for dev in m.available_devices():
        tag = "" if m.is_intel(dev) else "  (非 Intel，本项目不可用)"
        lines.append(f"  {dev:<8} {m.device_name(dev)}{tag}")
    npu = m.npu_info()
    lines.append(f"  NPU 可用: {npu['available']}")
    if npu["available"]:
        lines.append(f"    架构={npu.get('DEVICE_ARCHITECTURE')} "
                     f"驱动={npu.get('NPU_DRIVER_VERSION')} "
                     f"tiles={npu.get('NPU_MAX_TILES')}")
    lines.append(f"  选定设备: {m.resolve('auto')}")
    return "\n".join(lines)
