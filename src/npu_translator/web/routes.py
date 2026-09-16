"""`/api/*` 路由（SPEC.md · WebUI（nputweb））。

## 为什么必须 `run_in_executor`，而且 executor 只有 **1 个 worker**

`Translator.translate()` 是**同步阻塞**的（`pipe.generate()` 独占）。
在 `async def` 里直接调它 → 阻塞事件循环 → **整个服务卡死**，包括 `/api/health` 在内的
所有请求一起失联。所以必须丢到线程里。

那为什么不是多线程池？NPU 是单流设备（`SPEC.md · NPU 实现要点`）：
并发不会更快，只会让多个 batch 同时驻留内存 → OOM。engine 内部那把全局锁也决定了
它们最终仍会串行 —— 多线程只是把「排队」从队列挪进了锁竞争。所以 executor 的 worker 数是 **1**。

## 返回值不带 retry-after 之类复杂语义

出错一律 `{"error":{"code","message"}}`，**不回传 traceback**（会泄漏绝对路径，
违反 Git 约定，也是第 9 条安全措施）。
"""
from __future__ import annotations

import asyncio
import errno
import re
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from ..languages import LANGUAGES, common, others
from ..orchestrate import OrchestrateConfig, Translator
from ..segment import NEWLINE_MODES
from .app import ServerContext, error_payload
from .limits import TooLarge, check_body_size

__all__ = ["build_router", "max_body_bytes", "scrub_paths"]

# 请求体字节上限：按「字符数 × 4（UTF-8 最宽）+ 固定余量」折算。
# 为什么不用纯字符数比较 body：body 是字节流，中日文一个字 3 字节，
# 拿字节数直接比字符上限会把正常的中文输入拒掉。
_BODY_SLACK = 1024

# 错误摘要的硬上限。既是「别把半个异常文本吐给用户」，也是「别让响应体无限长」。
_MAX_ERROR_CHARS = 300

# ------------------------------------------------------------ 路径脱敏
# 为什么必须脱敏：OpenVINO / transformers 抛的异常消息里常带**模型绝对路径**
# （「找不到 ...\openvino_model.xml」）。直接回给客户端 = 把本机目录结构、
# 盘符、以及 `C:\Users\<用户名>\...` 里的用户名写进 HTTP 响应体 ——
# 既踩了隐私高压线，也等于给攻击者画了张地图。
#
# Windows 盘符：必须要求冒号后**紧跟**分隔符，否则 "Note: x" / "Ratio: 3/4"
# 这类正常英文会被误伤（单字母 + 冒号太常见了）。
_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>:|?*]*")

# POSIX：只收这几个**一定会带用户名/机器信息**的顶级目录，
# 不做通用 `/xxx/yyy` 匹配 —— 那会把 "and/or"、"50/50" 之类一起吃掉。
_POSIX_PATH_RE = re.compile(
    r"/(?:home|Users|usr|mnt|opt|srv|var|tmp|root|etc|data)"
    r"(?:/[^\s\"'<>:|?*]*)?"
)

_PATH_PLACEHOLDER = "<path>"


def scrub_paths(text: str) -> str:
    """把文本里的本机绝对路径换成 `<path>`。给一切「可能回显给客户端」的文本过一遍。"""
    return _POSIX_PATH_RE.sub(_PATH_PLACEHOLDER, _WIN_PATH_RE.sub(_PATH_PLACEHOLDER, text))


def _safe_error_message(exc: BaseException, limit: int = _MAX_ERROR_CHARS) -> str:
    """把异常压成「一行 + 不含路径」的摘要。

    只取 `type(exc).__name__` + message 的**第一行**：多行 message 里往往夹着
    traceback 片段或后续 frames 的说明，那些才是泄漏高发区。
    """
    text = f"{type(exc).__name__}: {exc}".split("\n")[0]
    return scrub_paths(text)[:limit]


# 客户端在 body 读完前断开时，socket 层抛的 OSError 的 errno。
# 为什么只收这几个：广撒网地 `except OSError` 会把**真**的磁盘/权限故障也吞成
# 「客户端断开」，那是把服务器自己的问题赖到客户端头上，排查时会带偏。
_DISCONNECT_ERRNOS = frozenset(
    code for code in (
        getattr(errno, name, None)
        for name in ("ECONNRESET", "ECONNABORTED", "EPIPE", "ENOTCONN",
                     "ESHUTDOWN", "ETIMEDOUT", "EBADF")
    )
    if code is not None
)

# Windows 上 Winsock 错误**不会**翻译成 errno，只能看 winerror：
# 10053=WSAECONNABORTED 10054=WSAECONNRESET 10058=WSAESHUTDOWN 64=WSAENETDOWN
_DISCONNECT_WINERRORS = frozenset({10053, 10054, 10058, 64})


def _is_client_gone(exc: BaseException) -> bool:
    """这个异常是不是「客户端半途跑了」。"""
    if isinstance(exc, ClientDisconnect):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in _DISCONNECT_WINERRORS:
        return True
    return exc.errno in _DISCONNECT_ERRNOS


def max_body_bytes(limit_chars: int) -> int:
    return limit_chars * 4 + _BODY_SLACK


async def _run_translation(ctx: ServerContext, text: str, target: str,
                           source: str) -> Any:
    """在单 worker 线程池里跑阻塞翻译，并施加超时。

    为什么走 executor 而不是直接调：见模块 docstring —— 阻塞事件循环会让
    `/api/health` 一起失联，表现为「服务假死」而不是「翻译慢」。
    """
    loop = asyncio.get_running_loop()
    coro = loop.run_in_executor(
        ctx.executor,
        lambda: ctx.translator.translate(text, target=target, source=source),
    )
    return await asyncio.wait_for(coro, timeout=ctx.security.timeout_s)


def build_router(ctx: ServerContext) -> Any:
    """构造 `/api/*` 路由。鉴权 / 限流都在中间件层，这里只管业务语义。

    ⚠️ 两个 Starlette 陷阱，都在这里踩过，改动前先看一眼：

    1. `Request` / `JSONResponse` 必须**在模块级 import**：本文件顶部有
       `from __future__ import annotations`，注解会变成字符串，FastAPI 在运行时按
       **模块 globals** 去解析它们。把 import 挪进函数体内的话注解解析不到 `Request`，
       FastAPI 会把 `request` 当成**查询参数** —— 所有 POST 一律 422（缺失 `query.request`），
       而错误信息里完全不提路由函数，极难查。
    2. `JSONResponse` 的第一个位置参数是 **content**，不是 status_code。
       写 `JSONResponse(400, payload)` 会把数字当响应体、字典当状态码，
       一直炸到最里层的 `init_headers`（`status_code < 200` 的 TypeError）。
       → 一律写关键字参数。
    """
    router = APIRouter()
    sec = ctx.security

    def _too_large(exc: TooLarge) -> JSONResponse:
        # 见 build_router docstring 陷阱 2：status_code 必须写关键字
        return JSONResponse(status_code=413,
                            content=error_payload("payload_too_large", str(exc)))

    # ------------------------------------------------------------ 翻译
    @router.post("/api/translate")
    async def translate(request: Request) -> Any:
        # 先验声明长度：**不等 body 读完就拒**，别让 10 GB 慢慢流进内存
        cap = max_body_bytes(sec.max_input_chars)
        try:
            check_body_size(request.headers.get("content-length"), cap)
        except TooLarge as exc:
            return _too_large(exc)

        # ★ 客户端可能在 body 传完之前就断开（关标签页、刷新、前端 abort）。
        #   不接住的话异常会冒泡到 uvicorn，**打出的 traceback 里带本机绝对路径**，
        #   而这条路径（含用户名）会出现在服务器控制台甚至日志里 —— 隐私高压线。
        #
        #   为什么**不**捕 `asyncio.CancelledError`：3.8 之后它是 BaseException，
        #   本来就不会被 `except Exception` 吃掉；而且关停时 uvicorn 正是靠取消
        #   来做优雅 shutdown，在这里吞掉会让服务关不干净。
        try:
            body = await request.body()
        except (ClientDisconnect, OSError) as exc:  # noqa: BLE001
            if not _is_client_gone(exc):
                raise
            # 499 不是标准 RFC 状态码，但 nginx 用了这么多年，含义大家认：客户端主动断开
            return JSONResponse(status_code=499,
                                content=error_payload("client_closed", "客户端已断开"))
        # 二次校验：Content-Length 可以和实际不一致（也说不清是被篡改还是中间层改写）
        if len(body) > cap:
            return _too_large(TooLarge(
                f"请求体过大：{len(body)} 字节（字符上限 {sec.max_input_chars}）"))

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - body 不是 JSON 是最常见的客户端错误
            return JSONResponse(status_code=400, content=error_payload("bad_json", "请求体不是合法 JSON"))

        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content=error_payload("bad_json", "请求体必须是 JSON 对象"))

        text = payload.get("text")
        if not isinstance(text, str):
            return JSONResponse(status_code=400, content=error_payload("bad_request", "text 必须是字符串"))
        if len(text) > sec.max_input_chars:
            return _too_large(TooLarge(
                f"输入 {len(text)} 字符，超过上限 {sec.max_input_chars}"))
        if not text.strip():
            return JSONResponse(status_code=400, content=error_payload("empty_input", "待翻译文本为空"))

        target = str(payload.get("target") or ctx.translator.opts.target)
        source = str(payload.get("source") or ctx.translator.opts.source)
        newline = str(payload.get("newline") or ctx.translator.opts.newline)
        strict = bool(payload.get("strict", False))

        if not OrchestrateConfig(target=target).supported_target:
            return JSONResponse(status_code=400, content=error_payload("bad_target", f"不支持的目标语言: {target!r}"))
        if newline not in NEWLINE_MODES:
            return JSONResponse(
                status_code=400,
                content=error_payload("bad_newline", f"newline 必须是 {NEWLINE_MODES} 之一"),
            )

        try:
            outcome = await _run_translation(ctx, text, target, source)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=504, content=error_payload(
                "timeout", f"单个请求超过 {sec.timeout_s:.0f}s 未完成"))
        except Exception as exc:  # noqa: BLE001
            # ★ 只回 type + message 的**摘要**，绝不回 traceback（见 _safe_error_message）
            return JSONResponse(status_code=500, content=error_payload(
                "internal_error", _safe_error_message(exc)))

        data = outcome.to_dict()
        data["newline"] = newline
        data["queue_position"] = request.scope.get("nputweb_queue_position", 0)
        if strict and not outcome.ok:
            return JSONResponse(status_code=500, content= {**data, "error": error_payload(
                "partial_failure", f"有 {outcome.failed} 段未译出")["error"]})
        return data

    # ------------------------------------------------------------ 语种
    @router.get("/api/languages")
    async def languages() -> dict:
        def pack(items: list) -> list[dict]:
            return [
                {
                    "code": lang.code,
                    "zh_name": lang.zh_name,
                    "en_name": lang.en_name,
                    "native": lang.native,
                    # zh-Hant 的 prompt 名是「繁体中文」而不是 Traditional Chinese —— 把那个坑固化进 API
                    "prompt_name": lang.target_name,
                }
                for lang in items
            ]

        return {
            "total": len(LANGUAGES),
            "common": pack(common()),
            "others": pack(others()),
        }

    # ------------------------------------------------------------ 健康与队列
    @router.get("/api/health")
    async def health() -> dict:
        translator: Translator = ctx.translator
        snap = translator.progress
        return {
            "status": "ready" if translator.is_ready else "loading",
            "device": translator.device,
            "devices": translator.devices,
            "degraded": list(translator.degraded),
            "queue": ctx.gate.depth,
            "waiting": ctx.gate.waiting,
            "max_pending": ctx.gate.max_pending,
            "progress": {"active": snap.active, "done": snap.done, "total": snap.total},
            "tls": ctx.https,
            "uptime_s": round(time.time() - ctx.started_at, 1),
        }

    return router
