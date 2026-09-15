"""Intel NPU 本地多语言翻译程序（npu_translator）。

设计约束：**import 本包不得拉起 OpenVINO 运行时**。
OpenVINO 初始化有百毫秒级开销且常驻占内存，纯工具函数（语种表、后处理、
prompt 拼装）的使用者不该为此买单，因此重型对象一律惰性导入。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

# 轻量模块：直接导出
from .languages import LANGUAGES, Language, all_codes, common, en_name, get, is_supported, others, zh_name
from .postprocess import clean
from .prompt import build

__all__ = [
    "__version__",
    "LANGUAGES",
    "Language",
    "all_codes",
    "common",
    "others",
    "en_name",
    "zh_name",
    "get",
    "is_supported",
    "build",
    "clean",
    "TranslateEngine",
    "get_engine",
    "DeviceManager",
    "resolve_device",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from .device import DeviceManager, resolve_device
    from .engine import TranslateEngine, get_engine


# ---------------------------------------------------------------- 惰性导入
_LAZY: dict[str, tuple[str, str]] = {
    "TranslateEngine": (".engine", "TranslateEngine"),
    "get_engine": (".engine", "get_engine"),
    "DeviceManager": (".device", "DeviceManager"),
    "resolve_device": (".device", "resolve_device"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        module_path, attr = _LAZY[name]
        from importlib import import_module

        value = getattr(import_module(module_path, __name__), attr)
        globals()[name] = value  # 缓存，后续直接命中
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *_LAZY.keys()])
