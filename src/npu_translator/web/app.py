"""FastAPI 工厂：中间件装配（SPEC.md · WebUI（nputweb））。

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
自己记一条**不带 query** 的行（见 SPEC.md · WebUI（nputweb））。

## `/docs` `/openapi.json` 为什么默认 404

它们会列出全部接口签名，等于给想探测的人一份地图。默认全关，`--debug` 才开（第 6 条安全措施）。

## 为什么这一层要参数化（M3）

`nputserve`（`/v1/*`）与 `nputweb`（`/api/*`）要共用**同一份**中间件栈 —— 安全基线只有一份实现，
复制一份必然漂移（Host 白名单 / 鉴权 / 脱敏 / 中间件装配顺序全是踩过坑的）。
两者只有三处不同：**受保护前缀**、**豁免集合**、**要不要挂静态面与文档页**。

所以把这三处提出来放进 `SecurityConfig`，**默认值逐字保持 nputweb 现状** ——
`nputweb` 侧一行调用代码都不用改，行为也不变；`nputserve` 传不同的值即可。

先验红线：前缀判定**只**决定「这个路径要不要走四步检查」，
**绝不**是「豁免」的另一种写法。豁免只跳过第 2 步（限流）与第 4 步（队列准入），
第 1 步（Host 白名单）与第 3 步（鉴权）一步不少。详见 `SecurityMiddleware`。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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
#
# ⚠️ M3 之后这里是**两个**集合的默认值来源（`SecurityConfig.exempt_from_rate` /
# `exempt_from_queue`）。保留这个名字是因为它记录了「nputweb 的既有行为」，
# 也是既有单测与注释的引用点；真正生效的是那两个字段。
_EXEMPT_FROM_LIMITS = frozenset({"/api/health"})

StatusCode = int
Headers = list[tuple[bytes, bytes]]


@dataclass
class SecurityConfig:
    """安全相关开关。**默认值就是章：`prepare()`安全生产 checklist 的执行形态。

    ## 后 8 个字段是 M3 加的，默认值逐字等于 nputweb 的既有行为

    加它们的唯一目的是让 `nputserve` 复用同一份中间件栈（安全基线不能有两份实现）。
    因此**每一个新字段的默认值都必须让 `nputweb` 与改造前一模一样** ——
    这条不靠人记，靠 `tests/test_web_app.py::test_security_config_defaults_match_nputweb` 锁死。
    """

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

    # ---- M3：让同一份中间件栈服务两个前缀。以下默认值 = nputweb 现状，勿随手改。
    #
    # 受保护前缀。nputweb 是 "/api/"（静态资源免检）；nputserve 取 "/"（全站受检 ——
    # 未知路径也要先过 Host 白名单与鉴权才拿 404，否则 `/随便什么` 就是一条
    # 不需要凭据的探测面）。
    api_prefix: str = "/api/"
    # 跳过**第 2 步限流**的路径。理由只有一条：前端心跳节奏不受服务端控制。
    # 这是 nputweb 专有的；`/v1/*` 是程序化调用，没有心跳，一律不免。
    exempt_from_rate: frozenset[str] = field(
        default_factory=lambda: frozenset(_EXEMPT_FROM_LIMITS))
    # 跳过**第 4 步队列准入**的路径。理由是「只读状态不该占位」，
    # 这与「限流豁免」的理由**不同**，所以是两个集合而不是一个 ——
    # 合成一个必然会在「两个都免」和「两个都不免」之间被迫选一个错的。
    exempt_from_queue: frozenset[str] = field(
        default_factory=lambda: frozenset(_EXEMPT_FROM_LIMITS))
    # nputserve 没有静态面（多一个面就多一个探测点）
    mount_static: bool = True
    # None = 用 `web/static`；留这个口子是为了单测能注入临时目录
    static_dir: Optional[Path] = None
    # 三个文档端点的**目标路径**。默认值 = 改造前 nputweb 硬编码的那三个，
    # 这样 `debug=True` 时的行为与改造前**逐字一致**（见 create_app 里的 gate）。
    docs_url: Optional[str] = "/docs"
    redoc_url: Optional[str] = "/redoc"
    openapi_url: Optional[str] = "/openapi.json"
    # openapi.json 是否**也**受 `--debug` gate。
    #   nputweb = True：它的 openapi 只给浏览器里的 /docs 用，默认必须 404。
    #   nputserve = False：它是给第三方程序读的**契约文件**，默认就该开着
    #   （关掉等于让调用方只能猜接口）。
    # 为什么要多这一个字段而不是把默认值设成 None：`docs_url` 那两条是靠
    # 「`None` 当关闭」表达的，openapi 却需要一个「给了路径、但要不要 gate」的独立开关；
    # 少它就只能在「nputweb 的 --debug 失效」和「nputserve 默认关掉契约文件」之间二选一。
    openapi_requires_debug: bool = True


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
    # ★ 只有 1 个 worker：NPU 是单流设备，多开只会抢锁 + OOM（SPEC.md · NPU 实现要点）
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


def create_app(
    ctx: ServerContext,
    *,
    title: str = "nputweb",
    version: str = "0.1.0",
    router: Any | None = None,
) -> Any:
    """构造 app。所有依赖通过 `ctx` 注入，**不读全局状态**。

    :param title / version: 给 FastAPI 的元信息（`nputserve` 用另一个标题与版本号）
    :param router: 路由对象；`None` = 用 `web.routes.build_router(ctx)`（nputweb 现状）。
        `nputserve` 传自己的 `/v1/*` 路由 —— 它不挂 `/api/*`，但中间件栈必须同一份。
    """
    from fastapi import FastAPI
    from starlette.staticfiles import StaticFiles

    from .routes import build_router

    sec = ctx.security
    app = FastAPI(
        title=title,
        version=version,
        # 第 6 条安全措施：docs / redoc 默认是给攻击者看的地图，一律默认 404。
        # ⚠️ 这两条**始终**受 debug gate：交互式页面 = 一份可点的地图，
        #    哪怕 nputserve 配了 docs_url，不给 --debug 也不开。
        docs_url=sec.docs_url if sec.debug else None,
        redoc_url=sec.redoc_url if sec.debug else None,
        # openapi.json 是否受 debug gate 由 `openapi_requires_debug` 决定：
        # nputweb 受（默认 404），nputserve 不受（它是给第三方程序读的契约文件）。
        openapi_url=sec.openapi_url if (sec.debug or not sec.openapi_requires_debug) else None,
    )

    # ⚠️ Starlette 的中间件栈是「先 add 的在外层」（add_middleware 追加到列表，
    # build_middleware_stack 从列表尾部往回包）。想要上面的嵌套关系，就得**倒着 add**：
    # 想让 SecurityHeaders 最外 → 它要第一个 add。顺序写反的话，被 Security 拒掉的 400
    # 就不带安全头了 —— 那种响应恰恰最需要它们。
    app.add_middleware(SecurityMiddleware, ctx=ctx)
    app.add_middleware(AccessLogMiddleware, ctx=ctx)
    app.add_middleware(SecurityHeadersMiddleware, https=sec.https)

    app.include_router(router if router is not None else build_router(ctx))

    # ★ 静态目录必须最后挂：StaticFiles 会把所有它下面的路径都吃掉，
    #   先挂的话 /api/* 会被当成静态文件请求返回 404。
    # nputserve 关掉了它（mount_static=False）：服务没有页面，多一个面就多一个探测点。
    if sec.mount_static:
        static_dir = sec.static_dir or _STATIC_DIR
        if static_dir.is_dir():
            app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


# ---------------------------------------------------------------- 错误体
def error_payload(code: str, message: str) -> dict:
    """统一错误体。**绝不回传 traceback** —— 那会把本机绝对路径写进 HTTP 响应，
    既是信息泄漏，也违反 Git 约定（见 SPEC.md · WebUI（nputweb））。"""
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

    ## M3 参数化：前缀与豁免

    - `sec.api_prefix`：前缀**外**的路径直接放行（nputweb 的静态资源继续免检）。
      nputserve 把它设成 `"/"`，于是全站受检 —— 未知路径也要先过 Host 白名单与鉴权才拿 404。
    - `sec.exempt_from_rate` / `sec.exempt_from_queue`：**两个**集合，理由不同所以生命周期不同。
      - 限流豁免的理由 = 「前端心跳节奏不受服务端控制」→ nputweb 专有，`/v1/*` 该为空集。
      - 队列豁免的理由 = 「只读状态不该占队列位置」→ 对 `/v1/health` 同样成立。

    🔴 **红线（评审阶段有两位成员在这里判错过）**：豁免判定必须在四步**之前**算好，
    下面只是「跳过第 2 步」「跳过第 4 步」两件事。**绝不能**写成在 path 判定处
    `await self.app(...); return` —— 那样 Host 白名单与鉴权会一起丢掉，
    health 就真成了免费的未鉴权探测端点。
    """

    #: 中间件塞进 scope 的键名（nputweb 前端的「排队中，第 N 位」读它）。
    QUEUE_POSITION_KEY = "nputweb_queue_position"

    def __init__(self, app: Any, ctx: ServerContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sec = self.ctx.security
        path: str = scope.get("path", "")

        # 前缀外的路径**整体**不受这套准入管（nputweb 的静态资源靠这条免检）。
        # 注意这里 return 掉的是「前缀之外」，不是「豁免」—— 两者语义完全不同，别混。
        if not path.startswith(sec.api_prefix):
            await self.app(scope, receive, send)
            return

        # 豁免必须在四步之前算好；下面只跳第 2 步与第 4 步。
        skip_rate = path in sec.exempt_from_rate
        skip_queue = path in sec.exempt_from_queue

        # ---- 1. Host 白名单（一步不少，豁免也不跳过）
        host = _header_value(scope, b"host")
        if not self.ctx.host_policy.allows(host):
            await send_json(send, 400, error_payload(
                "bad_host", "Host 头不在白名单内（DNS rebinding 防护）"))
            return

        # ---- 2. 限流（429）
        if not skip_rate:
            client = _client_ip(scope)
            decision = self.ctx.limiter.hit(client)
            if not decision.allowed:
                await send_json(
                    send, 429,
                    error_payload("rate_limited", f"请求过于频繁，请在 {decision.retry_after:.0f}s 后重试"),
                    [(b"retry-after", str(int(decision.retry_after) + 1).encode("ascii"))],
                )
                return

        # ---- 3. 鉴权（401，一步不少）
        token_hdr = _header_value(scope, b"authorization")
        if not self.ctx.token.accepts(token_hdr):
            await send_json(send, 401, error_payload(
                "unauthorized", "缺少或错误的 token（请带 Authorization: Bearer <token>）"))
            return

        # ---- 4. 队列准入（503）
        entered = False
        if not skip_queue:
            position = self.ctx.gate.try_enter()
            if position is None:
                await send_json(send, 503, error_payload(
                    "queue_full", f"队列已满（上限 {self.ctx.gate.max_pending}），请稍后重试"))
                return
            entered = True
            # 把「队列位置」塞进 scope：路由据此给前端回「排队中，前面还有 N 位」的提示
            scope[self.QUEUE_POSITION_KEY] = position

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
