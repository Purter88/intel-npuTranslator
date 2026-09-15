"""nputweb —— WebUI 子包（WebUI（nputweb））。

与上层介绍的差别：本包只是**命名空间**，真正的模块一律惰性导入。

两条硬约束：

1. **import 本包不得拉起 OpenVINO**（架构约定，与顶层 `npu_translator` 同理）。
   OpenVINO 初始化有百毫秒级开销且常驻占内存，而 `import npu_translator.web`
   可能只为了读一个常量 / 在单测里 import 某个纯函数。
2. **import 本包也不得拉起 fastapi / uvicorn / cryptography**。
   这些是 `optional-dependencies.web`，主装不包含；顶层 import 会让
   `import npu_translator.web` 在没装 extra 的环境里直接 ImportError，
   连"请先 pip install -e .[web]"这句友好提示都来不及说。

所以导出分两级：

- **纯常量 / 纯函数**（`nputweb_version` / `DEFAULT_PORT` 之类）直接放这里
- **重依赖模块**（`tls` / `app` / `routes` / `cli`）走 `__getattr__` 惰性导出，
  缺依赖时给可读的安装提示而不是裸 ImportError
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_INPUT_CHARS",
    "DEFAULT_PORT",
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_RATE_PER_MIN",
    "DEFAULT_TIMEOUT_S",
    "SERVER_BANNER",
    "nputweb_version",
]

# ---------------------------------------------------------------- 常量（无依赖，直接可读）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_INPUT_CHARS = 5000
DEFAULT_TIMEOUT_S = 120
DEFAULT_QUEUE_SIZE = 8
DEFAULT_RATE_PER_MIN = 30

SERVER_BANNER = "nputweb"


def nputweb_version() -> str:
    """nputweb 的版本 — 与分发包同版本，不做独立版本号。

    独立版本号会让人以为它是独立分发包，而 D-A 明确它是**同仓库子包**。
    """
    from .. import __version__

    return __version__


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from .app import create_app
    from .auth import TokenChecker, host_allows, is_loopback
    from .limits import RateLimiter, SlidingWindow
    from .routes import build_router
    from .tls import TlsMode, cert_fingerprint, ensure_self_signed, resolve_tls

_LAZY: dict[str, tuple[str, str]] = {
    # 纯逻辑（可在无网络 / 无终端环境里单测）
    "TlsMode": (".tls", "TlsMode"),
    "resolve_tls": (".tls", "resolve_tls"),
    "ensure_self_signed": (".tls", "ensure_self_signed"),
    "cert_fingerprint": (".tls", "cert_fingerprint"),
    "TokenChecker": (".auth", "TokenChecker"),
    "is_loopback": (".auth", "is_loopback"),
    "host_allows": (".auth", "host_allows"),
    "RateLimiter": (".limits", "RateLimiter"),
    "SlidingWindow": (".limits", "SlidingWindow"),
    # 重依赖（fastapi / cryptography）
    "create_app": (".app", "create_app"),
    "build_router": (".routes", "build_router"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        module_path, attr = _LAZY[name]
        from importlib import import_module

        try:
            value = getattr(import_module(module_path, __name__), attr)
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", None) or module_path
            raise ModuleNotFoundError(
                f"缺少可选依赖 {missing!r}。nputweb 需要额外的运行时包，"
                f"请安装：pip install -e .[web]"
            ) from exc
        globals()[name] = value  # 缓存，后续直接命中
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *_LAZY.keys()])
