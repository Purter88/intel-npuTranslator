"""FastAPI 工厂：中间件装配（WebUI（nputweb））。

## 中间件顺序（每一层都有理由）

```
SecurityHeaders      ← 最外层：即使请求被后面任何一层拒掉，响应也带安全头
  └ AccessLog        ← 脱敏后自记（uvicorn 自带那条**默认关掉**，它会记完整 URL）
      └ Security     ← Host 白名单 → 限流 → 鉴权 → 队列准入
          └ router(/api/*)
              └ StaticFiles("/")   ← **必须最后挂**，否则会吞掉 /api/*
```

（注意：上图是「请求进入顺序」；Starlette 的 `add_middleware` 是倒着来的，
见 `create_app` 里的说明。）

## 为什么 `uvicorn` 的 access log 要默认关

它记录**完整 URL**。启动横幅为了方便会在 URL 里带 `?token=`，一旦用户用这个 URL 访问，
token 就落进了日志/滚动终端。与其事后脱敏，不如从源头不记 —— Debug 模式下我们
自己记一条**不带 query** 的行。

## `/docs` `/openapi.json` 为什么默认 404

它们会列出全部接口签名，等于给想探测的人一份地图。默认全关，`--debug` 才开（第 6 条安全措施）。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..orchestrate import Translator
from .auth import HostPolicy, TokenChecker
from .limits import QueueGate, RateLimiter

__all__ = ["SecurityConfig", "ServerContext", "create_app"]

logger = logging.getLogger("nputweb.access")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# 只豁免「限流 + 队列准入」两条路径规则的路径集合。
# **Host 白名单与鉴权不在豁免范围内** —— 见 SecurityMiddleware 的说明。
#
# /api/health 为什么在里面：它是前端的常驻心跳，频率由前端自己决定、服务端管不着。
# 一次心跳扣一个配额的话，40 次/分钟的轮询会把 30/min 的额度吃光，
# 真正的翻译请求全变成 429 —— 这就是 nputweb 开着约 45 秒后必挂的根因。
_EXEMPT_FROM_LIMITS = frozenset({"/api/health"})

StatusCode = int
Headers = list[tuple[bytes, bytes]]


@dataclass
class SecurityConfig:
    """安全相关开关。**默认值就是章：`prepare()`安全生产 checklist 的执行形态。"""

    require_token: bool = True
    max_input_chars: int = 5000
    timeout_s: float = 120.0
    queue_size: int = 8
    rate_per_min: int = 30
    debug: bool = False
    https: bool = False      # 决定是否送 HSTS
    # 由 tls.py 解析产物回填；留在这里是为了 `_serve()` 不必再回头追 CLI 的状态
    ssl_certfile: str = ""
    ssl_keyfile: str = ""


@dataclass
class ServerContext:
    """路由与中间件共用的一份运行时状态。

    为什么不用全局单例：WebUI 是单实例没错，但**单测要能造多个互不干扰的实例**
    （否则测就是串味的，后写的用例会被前一个的缓存/队列影响）。
    """

    translator: Translator
    token: TokenChecker = field(default_factory=TokenChecker)
    host_policy: HostPolicy = field(default_factory=HostPolicy.build)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    gate: QueueGate = field(default_factory=QueueGate)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    started_at: float = field(default_factory=time.time)
    # ★ 只有 1 个 worker：NPU 是单流设备，多开只会抢锁 + OOM（NPU 实现要点）
    executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="nputweb-worker"),
    )

    def __post_init__(self) -> None:
        # max_workers 可能是负数或 0（配置手滑），ThreadPoolExecutor 会抛得很含糊
        if self.gate.max_pending < 1:
            raise ValueError("queue_size 必须 >= 1")

    @property
    def https(self) -> bool:
        return self.security.https

    def shutdown(self, wait: bool = False) -> None:
        """关停时释放线程池。

        :param wait: 是否等在跑的任务做完。关停路径**不要**等 —— NPU 那段
            `generate()` 可能卡在原生调用里（R9），等它等于关不掉。
        """
        self.executor.shutdown(wait=wait, cancel_futures=True)


def create_app(ctx: ServerContext) -> Any:
    """构造 app。所有依赖通过 `ctx` 注入，**不读全局状态**。"""
    from fastapi import FastAPI
    from starlette.staticfiles import StaticFiles

    from .routes import build_router

    sec = ctx.security
    app = FastAPI(
        title="nputweb",
        version="0.1.0",
        # 第 6 条安全措施：这三条默认是给攻击者看的地图，一律默认 404
        docs_url="/docs" if sec.debug else None,
        redoc_url="/redoc" if sec.debug else None,
        openapi_url="/openapi.json" if sec.debug else None,
    )

    # ⚠️ Starlette 的中间件栈是「先 add 的在外层」（add_middleware 追加到列表，
    # build_middleware_stack 从列表尾部往回包）。想要上面的嵌套关系，就得**倒着 add**：
    # 想让 SecurityHeaders 最外 → 它要第一个 add。顺序写反的话，被 Security 拒掉的 400
    # 就不带安全头了 —— 那种响应恰恰最需要它们。
    app.add_middleware(SecurityMiddleware, ctx=ctx)
    app.add_middleware(AccessLogMiddleware, ctx=ctx)
    app.add_middleware(SecurityHeadersMiddleware, https=sec.https)

    app.include_router(build_router(ctx))

    # ★ 静态目录必须最后挂：StaticFiles 会把所有它下面的路径都吃掉，
    #   先挂的话 /api/* 会被当成静态文件请求返回 404。
    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


# ---------------------------------------------------------------- 错误体
def error_payload(code: str, message: str) -> dict:
    """统一错误体。**绝不回传 traceback** —— 那会把本机绝对路径写进 HTTP 响应，
    既是信息泄漏，也违反 Git 约定（WebUI（nputweb）第 9 条）。"""
    return {"error": {"code": code, "message": message}}


async def send_json(send: Callable[[dict], Awaitable[None]], status: int, payload: dict,
                    extra_headers: Headers | None = None) -> None:
    """用裸 ASGI `send` 回一个 JSON 响应（中间件层没有 Request/Response 对象可用）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- 中间件
class SecurityHeadersMiddleware:
    """第 5 条安全措施：CSP / XFO / nosniff / Referrer-Policy /（HTTPS 时）HSTS。"""

    def __init__(self, app: Any, https: bool = False) -> None:
        self.app = app
        self.https = https

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + self._headers()
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _headers(self) -> Headers:
        # 只放行同源资源：页面零外链，所以 'self' 就够了。
        # data: 留给浏览器端生成的 Blob 下载（「下载 .txt」不经服务端）。
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "form-action 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        headers: Headers = [
            (b"content-security-policy", csp.encode("ascii")),
            (b"x-frame-options", b"DENY"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if self.https:
            headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        return headers


class SecurityMiddleware:
    """准入检查：Host 白名单 → 限流 → 鉴权 → 队列准入。

    顺序是刻意的：

    1. **Host 白名单最早**：DNS rebinding 的请求连配额都不该消耗，直接 400
    2. **限流早于鉴权**：否则 token 可以被无限次尝试
    3. **鉴权早于队列**：没资格的请求不该占队列位置

    ## 限流挡的到底是什么

    限流挡的是**廉价重复请求**——扫 token、刷翻译接口这类「单次成本极低、可以无限重放」的调用。
    它对防暴力破解**有帮助，但不足以单独当控制点**：4 位 PIN 在 30/min 下还要 5.6 小时，
    可一旦某个端点不受限流约束，同样的 4 位 PIN 只要 5 秒。强度必须另做校验 ——
    本轮 health 豁免**限流与队列准入**（已拍板的决策 D10），**鉴权与 Host 白名单不在豁免内**，
    所以强度校验（D11）是 health 这条路径上唯一的鉴权强度来源，它不在这个文件里。
    而 D11 只在**非回环**绑定时强制：回环 + 弱 token 是「黄字警告后放行」
    （见 `cli.resolve_binding` 的 D11 分支），
    也就是说回环场景下这条无限速通道**没有强度兜底** —— 这是已知的 Low 级残留风险。

    措辞要准确：是「豁免限流 + 队列」，**不是**「完全豁免」。
    说成「完全」会让人以为鉴权也一起免了，进而误判 health 是未鉴权的探测端点。

    顺带一提：第 2 步和第 3 步是**代码顺序、不是条件依赖** ——
    跳过第 2 步（限流）完全不影响第 3 步（鉴权）照常执行。

    ## `/api/health` 为什么豁免限流 + 队列准入

    health 是前端的常驻心跳。心跳频率只由前端调度决定，服务端拦不住 ——
    只要它按次扣配额，40 次/分钟的轮询就会把 30/min 的额度吃光，真正的翻译请求只能拿到 429，
    用户刷新页面 → in-flight 请求被 abort → 服务端刷 ConnectionResetError。

    豁免的**只有限流与队列准入两件事**：Host 白名单和鉴权一步不少，
    所以 health 依然是需要 token 的端点，不构成未鉴权的探测面。
    队列也一样要看：health 只是读状态，让它占队列位置会把排队的翻译请求挤成 503。
    """

    def __init__(self, app: Any, ctx: ServerContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # 豁免判定必须在四步**之前**算好：下面只是「跳过第 2 步和第 4 步」，
        # 绝不能写成在 path 判定处提前 `await self.app(...); return` ——
        # 那样 Host 白名单、鉴权、队列准入会一起丢掉，health 就真成了免费的未鉴权探测端点。
        exempt = path in _EXEMPT_FROM_LIMITS

        # ---- 1. Host 白名单
        host = _header_value(scope, b"host")
        if not self.ctx.host_policy.allows(host):
            await send_json(send, 400, error_payload(
                "bad_host", "Host 头不在白名单内（DNS rebinding 防护）"))
            return

        # ---- 2. 限流（429）
        if not exempt:
            client = _client_ip(scope)
            decision = self.ctx.limiter.hit(client)
            if not decision.allowed:
                await send_json(
                    send, 429,
                    error_payload("rate_limited", f"请求过于频繁，请在 {decision.retry_after:.0f}s 后重试"),
                    [(b"retry-after", str(int(decision.retry_after) + 1).encode("ascii"))],
                )
                return

        # ---- 3. 鉴权（401）
        token_hdr = _header_value(scope, b"authorization")
        if not self.ctx.token.accepts(token_hdr):
            await send_json(send, 401, error_payload(
                "unauthorized", "缺少或错误的 token（请带 Authorization: Bearer <token>）"))
            return

        # ---- 4. 队列准入（503）
        entered = False
        if not exempt:
            position = self.ctx.gate.try_enter()
            if position is None:
                await send_json(send, 503, error_payload(
                    "queue_full", f"队列已满（上限 {self.ctx.gate.max_pending}），请稍后重试"))
                return
            entered = True
            # 把「队列位置」塞进 scope，路由用来回给前端
            scope["nputweb_queue_position"] = position

        try:
            await self.app(scope, receive, send)
        finally:
            # 只对**真的进了场**的请求 leave。豁免路径根本没占位置，
            # 无条件 leave 会让计数偏小（leave 内部有 max(0, ...) 兜底不会为负，但语义必须是配对的）。
            if entered:
                self.ctx.gate.leave()


class AccessLogMiddleware:
    """脱敏的 access log，**只在 --debug 时记录**。

    记什么：方法 / 路径（**不含 query**）/ 状态码 / 耗时。
    不记什么：query（可能带 token）、请求体（可能是一整段待翻译的原文 —— 用户隐私）。
    """

    def __init__(self, app: Any, ctx: ServerContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.ctx.security.debug:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_holder: list[int] = []

        async def send_capture(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder.append(message["status"])
            await send(message)

        await self.app(scope, receive, send_capture)
        elapsed = time.perf_counter() - start
        # path 在这里是路由之外的原始路径；刻意不拼 querystring
        logger.info("%s %s -> %s %.0fms", scope.get("method", "?"),
                    scope.get("path", "?"), status_holder[0] if status_holder else "?",
                    elapsed * 1000)


# ---------------------------------------------------------------- ASGI 小工具
def _header_value(scope: dict, name: bytes) -> str:
    """从 ASGI scope 里取一个请求头（大小写不敏感）。"""
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _client_ip(scope: dict) -> str:
    """取来源 IP。

    ⚠️ **刻意不看 `X-Forwarded-For`**：它是一个可以被客户端伪造的头，
    相信它等于把限流的 key 交给攻击者自己选（换一个头就换一个配额）。
    本机单人工具不需要反向代理那一套。
    """
    client = scope.get("client")
    if client and isinstance(client, (tuple, list)) and client[0]:
        return str(client[0])
    return "unknown"
