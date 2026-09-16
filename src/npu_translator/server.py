"""`nputserve`：只挂 `/v1/*` 的翻译服务（SPEC.md · WebUI（nputweb））。

## 为什么是独立命令、独立进程（不挂进 nputweb）

五条互相独立的理由，任何一条都足以否掉合并：

1. **限流语义互斥**：`/api/health` 豁免限流的**唯一理由**是前端心跳节奏；
   程序化调用没有这个节奏，共享一个 `RateLimiter` 桶必然互相抢配额。
2. **静态挂载互斥**：nputweb 无条件 mount `web/static`；服务不需要也不该有静态面
   （多一个面就多一个探测点）。
3. **OpenAPI 策略互斥**：`/v1/openapi.json` 默认开（给第三方程序读），
   `/docs` / `/redoc` 默认关。同一进程做不到"一个开一个关"。
4. **生命周期与崩溃隔离**：WebUI 挂着浏览器与常驻心跳；服务被脚本调。
   一个崩了不该带走另一个。
5. **鉴权面分离**：可以给一枚独立 token，撤销时不影响 WebUI 会话。

## 复用了什么（**不重写**）

- 安全中间件栈 / app 工厂 → `web.app.create_app`（含 Host 白名单、限流、鉴权、队列准入）
- 鉴权与强度判定 → `web.auth`
- 限流 / 队列 → `web.limits`
- 证书三态 → `web.tls`
- 路径脱敏 / 断开判定 → `web.routes`
- D6 / D7 / D11 的绑定判定与退出码 → `web.cli.resolve_binding` / `check_port_free`
- **编排** → `orchestrate.Translator`（本文件不写第二份，见 `service.py` 的 docstring）

## `--allow-host`

与 `nputweb` 同义同字段（两个 Options 是同名同义的两个 dataclass，见下面的 ignore 注释）。
**默认是空的**：没显式声明的名字一律不放行，与「输错 IP」结果一致 ——
本机主机名 / FQDN / `.local` 一个都不自动推导。理由见 `web.auth.HostPolicy` 的 docstring。

注意本文件的 `api_prefix="/"`：全站受检，**没有** nputweb 那种静态资源免检通道，
所以连 `/v1/health` 与 `/v1/openapi.json` 都要过 Host 白名单 —— 这里配错的影响面更大。

## 本文件是**唯一**允许在模块级 import fastapi 的新文件

`import npu_translator` 不得拉起 OpenVINO；`import npu_translator.web` 不得拉起 fastapi。
本模块两者都不违反：`service.py` 走延迟 import，本模块只被 console script 引用，
**不被 `npu_translator/__init__.py` 也不被 `web/` 引用**。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, List, Optional

import typer
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

from . import __version__
from . import config as cfg
from .orchestrate import OrchestrateConfig, Translator
from .pool import cpu_pipeline_props
from .service import (
    DEFAULT_MAX_STREAM_CHARS,
    ApiError,
    ServiceConfig,
    ServiceRuntime,
    TranslatorService,
)
from .web import DEFAULT_HOST, DEFAULT_MAX_INPUT_CHARS, DEFAULT_QUEUE_SIZE, DEFAULT_RATE_PER_MIN, DEFAULT_TIMEOUT_S
from .web.app import SecurityConfig, SecurityMiddleware, ServerContext, create_app
from .web.auth import HostPolicy, TokenChecker, is_loopback, parse_hosts
from .web.cli import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_STARTUP,
    EXIT_USAGE,
    check_port_free,
    resolve_binding,
    resolve_listen_host,
)
from .web.limits import QueueGate, RateLimiter, TooLarge, check_body_size
from .web.routes import _is_client_gone, _safe_error_message, max_body_bytes, scrub_paths
from .web.tls import TlsError, resolve_tls

__all__ = [
    "DEFAULT_EXEMPT_FROM_QUEUE",
    "DEFAULT_SERVE_PORT",
    "Options",
    "build_router",
    "create_service_app",
    "main",
    "main_entry",
    "service_security",
]

# nputweb 用 8765；服务默认让一个端口，因为两个命令**会**同时起
# （崩溃隔离的意义就在于此），默认撞车会让第二个直接启动失败。
DEFAULT_SERVE_PORT = 8766

# 优雅 shutdown 的上限，超时就硬退（NPU 那段 generate 可能卡在原生调用里）
_GRACE_SECONDS = 3.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 之类反向代理的响应缓冲 —— 否则 SSE 会被攒着一次性发
    "X-Accel-Buffering": "no",
}


# ================================================================ 路由
def build_router(runtime: ServiceRuntime,
                 service: TranslatorService | None = None) -> Any:
    """构造 `/v1/*` 路由。鉴权 / 限流 / 队列都在中间件层，这里只管业务语义。

    ⚠️ 两个 Starlette 陷阱，都踩过（详见 `web.routes.build_router` 的 docstring）：

    1. `Request` 必须**在模块级 import**：本文件有 `from __future__ import annotations`，
       注解会变成字符串，FastAPI 运行时按**模块 globals** 解析。挪进函数体的话
       `request` 会被当成查询参数，所有 POST 一律 422，而错误信息里完全不提路由函数。
    2. `JSONResponse` 第一个位置参数是 **content** 不是 status_code → 一律写关键字参数。
    """
    router = APIRouter()
    svc = service if service is not None else TranslatorService(runtime)
    sec = runtime.security

    def err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
        # 一切可能回显的文本都要过脱敏：异常消息里常带本机绝对路径（含用户名）
        return JSONResponse(status_code=status,
                            content={"error": {"code": code, "message": scrub_paths(message)}},
                            headers=headers)

    def api_error(exc: ApiError) -> JSONResponse:
        return err(exc.status, exc.code, exc.message, exc.headers or None)

    async def read_payload(request: Request) -> tuple[Any, JSONResponse | None]:
        """读 body。**先验声明长度再读**，别让 10 GB 慢慢流进内存。"""
        cap = max_body_bytes(sec.max_input_chars)
        try:
            check_body_size(request.headers.get("content-length"), cap)
        except TooLarge as exc:
            return None, err(413, "payload_too_large", str(exc))
        try:
            body = await request.body()
        except (ClientDisconnect, OSError) as exc:
            # ★ 不接住的话异常会冒泡到 uvicorn，打出的 traceback 里带**本机绝对路径**
            #   （含用户名）—— 隐私高压线。
            #   为什么不捕 CancelledError：3.8 之后它是 BaseException，且关停时 uvicorn
            #   正是靠取消做优雅 shutdown，在这里吞掉会让服务关不干净。
            if not _is_client_gone(exc):
                raise
            # 499 不是 RFC 状态码，但 nginx 用了这么多年，含义大家认：客户端主动断开
            return None, err(499, "client_closed", "客户端已断开")
        # 二次校验：Content-Length 可以和实际不一致
        if len(body) > cap:
            return None, err(413, "payload_too_large",
                             f"请求体过大：{len(body)} 字节（字符上限 {sec.max_input_chars}）")
        try:
            return await request.json(), None
        except Exception:  # noqa: BLE001 - body 不是 JSON 是最常见的客户端错误
            return None, err(400, "bad_json", "请求体不是合法 JSON")

    # ------------------------------------------------------------ 翻译
    @router.post(
        "/v1/translate",
        summary="翻译一段文本",
        description="请求体与 nputweb 的 `/api/translate` 同形；"
                    "响应 = 编排层 `Outcome.to_dict()` 加 `newline` / `queue_position` / `request_id`。",
        responses={504: {"description": "超时。**底层推理不可取消**，"
                                        "结果不返回给调用方（可能仍写入缓存）；"
                                        "响应头 `X-NPUT-Orphan: 1` 标记产生了孤儿。"}},
    )
    async def translate(request: Request) -> Any:
        payload, bad = await read_payload(request)
        if bad is not None:
            return bad
        try:
            req = svc.parse_translate(payload, streaming=False)
        except ApiError as exc:
            return api_error(exc)
        try:
            position = int(request.scope.get(SecurityMiddleware.QUEUE_POSITION_KEY, 0) or 0)
            data = await svc.translate(req, queue_position=position)
        except ApiError as exc:
            return api_error(exc)
        except Exception as exc:  # noqa: BLE001 - 兜底：只回摘要，绝不回 traceback
            return err(500, "internal_error", _safe_error_message(exc))
        return data

    # ------------------------------------------------------------ 语种
    @router.get("/v1/languages", summary="支持的语种（38 = 33 主流 + 5 民族语/方言）")
    async def languages() -> dict:
        return svc.languages()

    # ------------------------------------------------------------ 健康
    @router.get(
        "/v1/health",
        summary="健康与排队状态",
        description="`active_device` 是当前生效的推理设备；`lane` 是谁占着推理通道；"
                    "`orphans` 非零表示有被放弃但仍在跑的推理（**预期行为**，不是 bug）。",
    )
    async def health() -> dict:
        return svc.health()

    # ------------------------------------------------------------ 流式
    @router.post(
        "/v1/translate/stream",
        summary="流式翻译（SSE）",
        description="`text/event-stream`。事件序列：`ready → token* → done`，"
                    "失败时产出 `error`。**流式不分段**，输入受 `max_stream_chars` 限制。\n\n"
                    "并发上限 `max_streams`（默认 1）：超了**不排队**，直接 503 `stream_busy`。",
        responses={503: {"description": "stream_busy（已有流在跑）或 lane_busy（等通道超时）"}},
    )
    async def translate_stream(request: Request) -> Any:
        payload, bad = await read_payload(request)
        if bad is not None:
            return bad
        try:
            req = svc.parse_translate(payload, streaming=True)
        except ApiError as exc:
            return api_error(exc)
        try:
            # ★ 必须在建 StreamingResponse **之前**抢槽位与通道：
            #   async generator 的第一行代码要等到 body 开始迭代才跑，
            #   那时响应头已经发出去了，503 就没法给了。
            session = await svc.acquire_stream(req)
        except ApiError as exc:
            return api_error(exc)
        return StreamingResponse(
            svc.stream_events(req, request.is_disconnected, session=session),
            media_type="text/event-stream; charset=utf-8",
            headers=SSE_HEADERS,
        )

    return router


# ================================================================ app 工厂
def create_service_app(ctx: ServerContext, runtime: ServiceRuntime) -> Any:
    """把 `ServerContext`（安全基线）与 `ServiceRuntime`（并发模型）合成一个 app。

    中间件栈与 nputweb **同一份**（`create_app`），只有三处不同由 `SecurityConfig` 表达：
    前缀 `/`、豁免集合、静态面与文档页。
    """
    return create_app(ctx, title="nputserve", version=__version__,
                      router=build_router(runtime))


# ================================================================ 安全配置
#: 队列豁免的默认集合。理由与「限流豁免」**不同**：
#: 限流豁免是因为「前端心跳节奏不受服务端控制」（nputweb 专有，程序化调用没有心跳）；
#: 队列豁免是因为「纯读不该占队列位置，会把排队的翻译挤成 503」（两边都成立）。
#: 合成一个集合必然会被迫在「两个都免」和「两个都不免」之间选一个错的。
DEFAULT_EXEMPT_FROM_QUEUE = frozenset({"/v1/health", "/v1/languages"})


def service_security(
    *,
    debug: bool = False,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    queue_size: int = DEFAULT_QUEUE_SIZE,
    rate_per_min: int = DEFAULT_RATE_PER_MIN,
    https: bool = False,
    ssl_certfile: str = "",
    ssl_keyfile: str = "",
) -> SecurityConfig:
    """`nputserve` 的 `SecurityConfig`（三处与 nputweb 不同，其余继承安全基线）。

    单独抽成函数是为了让 CLI 与单测用**同一份**默认值 —— 两边各写一遍的话，
    「测试通过但真机行为不同」这种事迟早发生。
    """
    return SecurityConfig(
        debug=debug,
        max_input_chars=max_input_chars,
        timeout_s=timeout_s,
        queue_size=queue_size,
        rate_per_min=rate_per_min,
        https=https,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        # 全站受检：未知路径也要先过 Host 白名单与鉴权才拿 404，
        # 否则 `/随便什么` 就是一条不需要凭据的探测面
        api_prefix="/",
        exempt_from_rate=frozenset(),
        exempt_from_queue=frozenset(DEFAULT_EXEMPT_FROM_QUEUE),
        mount_static=False,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/v1/openapi.json",
        # ★ openapi.json **不**受 --debug gate：它是给第三方程序读的契约文件
        openapi_requires_debug=False,
    )


# ================================================================ CLI
@dataclass
class Options:
    """`nputserve` 的选项。**与 nputweb 同名的字段语义相同**（便于 `resolve_binding` 复用）。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_SERVE_PORT
    # 额外放行的 Host 名字（`--allow-host`，可重复给）。默认空 = 不放行。
    # 语义与 nputweb 的同名字段一致；理由见 `web.auth.HostPolicy` 的 docstring。
    allow_hosts: tuple[str, ...] = ()
    tls: str = "auto"
    cert: Optional[str] = None
    key: Optional[str] = None
    token: Optional[str] = None
    no_auth: bool = False
    allow_insecure: bool = False
    allow_no_auth: bool = False
    device: str = cfg.DEVICE
    newline: str = "soft"
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS
    timeout: float = DEFAULT_TIMEOUT_S
    queue_size: int = DEFAULT_QUEUE_SIZE
    rate: int = DEFAULT_RATE_PER_MIN
    debug: bool = False
    no_warmup: bool = False
    # ---- 服务层专有
    max_streams: int = 1
    lane_wait: float = 10.0
    max_stream_chars: int = DEFAULT_MAX_STREAM_CHARS


def _env_str(name: str, default: str) -> str:
    return os.getenv(name) or default


def options_from_env() -> Options:
    """读 `NPT_SERVE_*` 作为**默认值**（命令行显式给值则覆盖）。

    另沿用 `NPT_DEVICE` / `NPT_MODEL` 这两个全局环境变量。
    """
    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def _bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    return Options(
        host=_env_str("NPT_SERVE_HOST", DEFAULT_HOST),
        port=_int("NPT_SERVE_PORT", DEFAULT_SERVE_PORT),
        allow_hosts=parse_hosts(os.getenv("NPT_SERVE_ALLOWED_HOSTS")),
        tls=_env_str("NPT_SERVE_TLS", "auto"),
        cert=os.getenv("NPT_SERVE_CERT") or None,
        key=os.getenv("NPT_SERVE_KEY") or None,
        token=os.getenv("NPT_SERVE_TOKEN") or None,
        no_auth=_bool("NPT_SERVE_NO_AUTH", False),
        allow_insecure=_bool("NPT_SERVE_ALLOW_INSECURE", False),
        allow_no_auth=_bool("NPT_SERVE_ALLOW_NO_AUTH", False),
        device=_env_str("NPT_DEVICE", cfg.DEVICE),
        max_input_chars=_int("NPT_SERVE_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS),
        timeout=_float("NPT_SERVE_TIMEOUT", DEFAULT_TIMEOUT_S),
        queue_size=_int("NPT_SERVE_QUEUE", DEFAULT_QUEUE_SIZE),
        rate=_int("NPT_SERVE_RATE", DEFAULT_RATE_PER_MIN),
        debug=_bool("NPT_SERVE_DEBUG", False),
        max_streams=_int("NPT_SERVE_MAX_STREAMS", 1),
        lane_wait=_float("NPT_SERVE_LANE_WAIT", 10.0),
        max_stream_chars=_int("NPT_SERVE_MAX_STREAM_CHARS", DEFAULT_MAX_STREAM_CHARS),
    )


def build_runtime(opts: Options) -> tuple[ServerContext, ServiceRuntime, TokenChecker, list[str], Any]:
    """构造运行时。返回 `(ServerContext, ServiceRuntime, TokenChecker, devices, TlsPlan)`。

    D6 / D7 / D11 的判定全部交给 `web.cli.resolve_binding` —— 那份实现是踩过坑的，
    **不要在这里重写一遍**（重写必漂移）。
    """
    try:
        # extra_hosts 与 HostPolicy 用同一份（不同步会把白名单的 400 换成证书名不匹配）
        tls_plan = resolve_tls(opts.tls, opts.cert, opts.key,
                               bind_host=opts.host, extra_hosts=opts.allow_hosts)
    except TlsError as exc:
        typer.secho(f"参数错误: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_USAGE) from exc

    # resolve_binding 会因 D7 直接终止进程，所以必须在建 context 之前跑。
    #
    # ⚠️ 这里的 `type: ignore[arg-type]` 是**刻意的取舍，不要删**：
    # `resolve_binding()` 只读 opts 的七个属性（host/tls/cert/key/no_auth/allow_insecure/token），
    # 本文件的 `Options` 与 `web.cli.Options` 是**同名同义的两个 dataclass**，
    # 鸭子类型传参即可，不必为了让 mypy 闭嘴去重构 nputweb（那会动到 300 项既有单测的地基）。
    # 保留这条 ignore 是为了把「新增 mypy 告警」这个信号压回 0 —— 基线那 19 条已经污染了信噪比，
    # 只要"新增必须为 0"是可达的，将来就能拿它当门禁。删掉它就会多一条常驻噪音。
    _scheme, checker, tls_enabled = resolve_binding(opts)  # type: ignore[arg-type]

    translator = Translator(
        OrchestrateConfig(
            target="en", device=opts.device, newline=opts.newline,
            cpu_props=cpu_pipeline_props("0", "any", None),
        ),
        on_degrade=lambda name, err: typer.secho(
            f"设备 {name} 不可用，已降级：{err}", err=True, fg=typer.colors.YELLOW),
    )

    security = service_security(
        debug=opts.debug,
        max_input_chars=opts.max_input_chars,
        timeout_s=opts.timeout,
        queue_size=opts.queue_size,
        rate_per_min=opts.rate,
        https=tls_enabled,
        ssl_certfile=tls_plan.certfile,
        ssl_keyfile=tls_plan.keyfile,
    )
    ctx = ServerContext(
        translator=translator,
        token=checker,
        host_policy=HostPolicy.build(opts.host, extra=opts.allow_hosts),
        limiter=RateLimiter(per_minute=opts.rate),
        gate=QueueGate(max_pending=opts.queue_size),
        security=security,
    )
    runtime = ServiceRuntime(
        translator=translator,
        security=security,
        config=ServiceConfig(
            lane_wait_timeout_s=opts.lane_wait,
            max_streams=opts.max_streams,
            max_stream_chars=opts.max_stream_chars,
        ),
        # ★ 复用 ctx 的 executor 与 gate：队列位置必须两边看同一个对象，
        #   否则中间件放的是 A、health 读的是 B，`queue` 字段就永远对不上。
        executor=ctx.executor,
        gate=ctx.gate,
    )
    return ctx, runtime, checker, translator.devices, tls_plan


def print_banner(port: int, scheme: str, checker: TokenChecker, devices: list[str],
                 fingerprint: str, display_host: str, cfg_limits: dict,
                 allow_hosts: tuple[str, ...] = ()) -> None:
    """启动横幅：**必须**告诉用户「在哪个端口上 / token 是什么 / 设备是谁」。"""
    Green, Yellow, Dim = typer.colors.GREEN, typer.colors.YELLOW, typer.colors.BRIGHT_BLACK
    typer.secho("")
    typer.secho("nputserve 已就绪（一键停止：Ctrl+C）", fg=Green)
    typer.secho(f"  本地：    {scheme}://127.0.0.1:{port}/v1/health")
    if display_host not in {"127.0.0.1", "localhost"}:
        typer.secho(f"  局域网：  {scheme}://{display_host}:{port}/v1/health")
    if checker.enabled:
        typer.secho(f"  鉴权：    Authorization: Bearer {checker.token}", fg=Yellow)
        typer.secho("            （只打印这一次，请现在复制保存）", fg=Dim)
    else:
        typer.secho("  鉴权：    已关闭（--no-auth；非回环下需 --allow-no-auth）", fg=Yellow)
    if scheme == "https":
        typer.secho(f"  证书：    自签发 · SHA-256 指纹 {fingerprint}")
        typer.secho("            （请核对与首次一致；别把它加入系统信任库）", fg=Dim)
    else:
        typer.secho("  明文 HTTP：未加密 —— 本机回环访问尚可，请勿跨机使用", fg=Yellow)
    typer.secho(f"  设备：    {' → '.join(devices) if devices else '?'}"
                "  引擎：加载中（/v1/health 会显示 loading → ready）")
    # 这两个 f-string 里的 `·` 是分隔符不是占位符，没有 `{}` 就别带 f 前缀（ruff F541）
    typer.secho("  接口：    POST /v1/translate · POST /v1/translate/stream · "
                "GET /v1/languages · GET /v1/health")
    typer.secho(f"  文档：    {scheme}://127.0.0.1:{port}/v1/openapi.json"
                f"（/docs 需 --debug）", fg=Dim)
    if allow_hosts:
        typer.secho(f"  放行 Host：{', '.join(allow_hosts)}"
                    "（此外只认 localhost 与本机 IP）", fg=Dim)
    typer.secho(f"  限制：    输入 {cfg_limits['max_input_chars']} 字符 · "
                f"流式 {cfg_limits['max_stream_chars']} 字符 · "
                f"超时 {cfg_limits['timeout_s']:.0f}s · 队列 {cfg_limits['queue_size']} · "
                f"限流 {cfg_limits['rate_per_min']}/min", fg=Dim)
    typer.secho("")


async def _serve(ctx: ServerContext, runtime: ServiceRuntime, opts: Options) -> None:
    import uvicorn

    sec = ctx.security
    server = uvicorn.Server(uvicorn.Config(
        create_service_app(ctx, runtime),
        host=opts.host,
        port=opts.port,
        ssl_keyfile=sec.ssl_keyfile or None,
        ssl_certfile=sec.ssl_certfile or None,
        # ★ Windows 没有 uvloop；别写死 loop 类型
        access_log=False,                  # 它会记完整 URL（可能带 token）
        log_level="debug" if opts.debug else "warning",
        server_header=False,               # 少一个指纹
        date_header=False,
    ))
    ctx._server = server  # type: ignore[attr-defined]
    await server.serve()


def run_server(ctx: ServerContext, runtime: ServiceRuntime, opts: Options) -> int:
    """在**后台线程**里跑 uvicorn，主线程专职等 Ctrl+C。

    为什么不直接在主线程 `asyncio.run`：要精确控制「优雅 → 超时 → 硬退」这条链。
    NPU 那段 generate 可能卡在原生调用里，等它等于永远关不掉 —— 所以既要给优雅的机会，
    也要有硬退兜底。
    """
    def worker() -> None:
        try:
            asyncio.run(_serve(ctx, runtime, opts))
        except Exception as exc:  # noqa: BLE001 - 启动失败要让用户看见
            typer.secho(f"服务启动失败: {type(exc).__name__}: {exc}", err=True,
                        fg=typer.colors.RED)

    thread = threading.Thread(target=worker, daemon=True, name="nputserve-server")
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        typer.secho("\n正在停止服务…", err=True, fg=typer.colors.YELLOW)
        server = getattr(ctx, "_server", None)
        if server is not None:
            server.should_exit = True  # type: ignore[union-attr]
        thread.join(_GRACE_SECONDS)
        if thread.is_alive():
            typer.secho(f"优雅关停超过 {_GRACE_SECONDS:.0f}s，强制退出", err=True,
                        fg=typer.colors.YELLOW)
        try:
            ctx.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - 关停阶段没有可恢复动作
            pass
        return EXIT_INTERRUPTED
    return EXIT_OK


def _warmup(translator: Translator) -> None:
    """预热翻译引擎。失败**不能**让服务起不来 —— 降级为「每次请求才加载」。"""
    try:
        translator.prepare()
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"引擎预热失败，将在首次请求时重试: {type(exc).__name__}: {exc}",
                    err=True, fg=typer.colors.YELLOW)


app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    rich_markup_mode=None,   # 与 nputr / nputweb 同理：rich 的可选依赖未必装全
    help="nputserve：本地离线翻译的 /v1 HTTP 服务（独立进程，与 nputweb 并列）",
)


def merge(opts: Options, **cli_values: object) -> Options:
    """把命令行给出的值合并进环境默认值。

    ⚠️ 刻意**不用** `ctx.get_parameter_source()`（typer 0.27 自带一份 click，
    `ParameterSource` 与真 click 的是两个枚举类，`==` 恒为 False，见 SPEC.md · 踩坑记录）。
    用 `None` 当"我没给"的信号，绕开整个问题。
    """
    for key, value in cli_values.items():
        if value is not None:
            setattr(opts, key, value)
    return opts


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", help="绑定地址（默认 127.0.0.1；非回环会强制 token）"),
    port: Optional[int] = typer.Option(None, "--port", help=f"端口（默认 {DEFAULT_SERVE_PORT}，与 nputweb 的 8765 错开）"),
    tls: Optional[str] = typer.Option(None, "--tls", help="auto=自签（默认）| on=用 --cert/--key | off=明文"),
    cert: Optional[str] = typer.Option(None, "--cert", help="证书路径（--tls on 时必填）"),
    key: Optional[str] = typer.Option(None, "--key", help="私钥路径（--tls on 时必填）"),
    token: Optional[str] = typer.Option(None, "--token", help="访问令牌；不给则自动生成并打印一次"),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="关闭鉴权（非回环地址需再加 --allow-no-auth）"),
    allow_insecure: bool = typer.Option(
        False, "--allow-insecure", help="放行「非回环 + 明文」这一危险组合"),
    allow_no_auth: bool = typer.Option(
        False, "--allow-no-auth",
        help="逃生舱：非回环下允许 --no-auth，并放行明文与弱 token（测试 / 可信局域网）"),
    allow_host: Optional[List[str]] = typer.Option(
        None, "--allow-host",
        help="额外放行的 Host 名（可重复给；也可设 NPT_SERVE_ALLOWED_HOSTS，逗号分隔）。"
             "不声明就不放行，与输错地址一样直接拒"),
    device: Optional[str] = typer.Option(None, "--device", "-d", help="npu | cpu | gpu | auto | hetero"),
    newline: Optional[str] = typer.Option(None, "--newline", help="soft | hard | auto（语义同 nputr）"),
    max_input_chars: Optional[int] = typer.Option(None, "--max-input-chars", help="单次输入字符上限"),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="单请求超时秒数"),
    queue_size: Optional[int] = typer.Option(None, "--queue-size", help="队列上限，超出返回 503"),
    rate: Optional[int] = typer.Option(None, "--rate", help="单 IP 每分钟请求上限，超出返回 429"),
    max_streams: Optional[int] = typer.Option(None, "--max-streams", help="并发流式上限（默认 1，超出 503 不排队）"),
    lane_wait: Optional[float] = typer.Option(None, "--lane-wait", help="等推理通道的秒数（0 = 无限等）"),
    max_stream_chars: Optional[int] = typer.Option(
        None, "--max-stream-chars",
        help=f"流式输入字符上限（默认 {DEFAULT_MAX_STREAM_CHARS}；流式不分段，超了会静默截断）"),
    debug: bool = typer.Option(False, "--debug", help="开启 /docs /redoc 与脱敏 access log"),
    no_warmup: bool = typer.Option(False, "--no-warmup", help="跳过启动预热（首次请求会更慢）"),
    version: bool = typer.Option(False, "--version", help="显示版本后退出"),
) -> None:
    """启动 `/v1` 翻译服务：默认 https://127.0.0.1:8766（自签证书）。

    **安全性 > 稳定性 > 效率**：非回环绑定或启用 TLS 一律强制 token；
    「非回环 + 明文」默认拒绝启动（除非 --allow-insecure）。
    测试 / 可信局域网可用 `--allow-no-auth` 一次性解除这三条硬拦（降级为警告）。
    """
    if ctx.invoked_subcommand is not None:
        return
    if version:
        typer.echo(f"nputserve {__version__}")
        return

    opts = merge(
        options_from_env(),
        host=host, port=port, tls=tls, cert=cert, key=key, token=token,
        device=device, newline=newline, max_input_chars=max_input_chars,
        timeout=timeout, queue_size=queue_size, rate=rate,
        max_streams=max_streams, lane_wait=lane_wait,
        max_stream_chars=max_stream_chars,
    )
    # 列表型：命令行显式给了就用命令行的；空 / None 都算「没给」，保留 env 的值
    if allow_host:
        opts.allow_hosts = tuple(allow_host)
    # bool 型：命令行开关只能"加"，env 只能"减"。用 or 合并，False 不会覆盖 env 的 True
    opts.no_auth = opts.no_auth or no_auth
    opts.allow_insecure = opts.allow_insecure or allow_insecure
    opts.allow_no_auth = opts.allow_no_auth or allow_no_auth
    opts.debug = opts.debug or debug
    opts.no_warmup = opts.no_warmup or no_warmup

    code = start_server(opts)
    if code:
        raise typer.Exit(code=code)


def start_server(opts: Options) -> int:
    """把服务跑起来并阻塞到退出。返回退出码（测试与外部调用者用得着）。"""
    # ① 端口先查：默认端口被别的实例占着是最常见的情况，早点说清楚
    try:
        check_port_free(opts.host, opts.port)
    except SystemExit as exc:
        typer.secho(f"启动失败: {exc}", err=True, fg=typer.colors.RED)
        return EXIT_STARTUP

    # ② 证书 + 绑定规则的合法性（含 D7 的拒绝启动与 D11 的弱 token）
    try:
        built = build_runtime(opts)
    except typer.Exit as exc:
        return exc.exit_code if isinstance(exc.exit_code, int) else EXIT_USAGE
    ctx, runtime, checker, devices, tls_plan = built

    display_host = resolve_listen_host(opts.host)
    scheme = "https" if ctx.https else "http"

    # ③ 后台预热：NPU 首次编译约 30 s，不能拖住监听（否则用户以为启动失败）
    if not opts.no_warmup:
        threading.Thread(target=_warmup, args=(ctx.translator,), daemon=True,
                         name="nputserve-warmup").start()

    print_banner(opts.port, scheme, checker, devices,
                 getattr(tls_plan, "fingerprint", "") or "", display_host,
                 {"max_input_chars": opts.max_input_chars,
                  "max_stream_chars": opts.max_stream_chars,
                  "timeout_s": opts.timeout,
                  "queue_size": opts.queue_size,
                  "rate_per_min": opts.rate}, opts.allow_hosts)

    if scheme == "http" and not is_loopback(opts.host):
        typer.secho("⚠️ 当前是**明文 HTTP + 非回环**：局域网内任何人都能用你的 NPU",
                    err=True, fg=typer.colors.RED)
    if not checker.enabled and not is_loopback(opts.host):
        typer.secho("⚠️ 当前是**无鉴权 + 非回环**：同一网段任何人都能直接用你的 NPU，"
                    "连 token 都不用猜", err=True, fg=typer.colors.RED)

    return run_server(ctx, runtime, opts)


def _cli_main() -> int:
    """CLI 主逻辑：只返回退出码（与 nputr / nputweb 的约定一致，便于子进程测试）。"""
    from .encoding import configure_stdio

    configure_stdio()
    cmd = typer.main.get_command(app)
    cmd.allow_interspersed_args = True
    try:
        cmd(prog_name="nputserve")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


def main_entry() -> None:
    """console script 入口。

    为什么这里也 `os._exit`：卡住关停的往往不是"正在翻译"，而是 uvicorn / OpenVINO
    留下的线程 join 不上（SPEC.md · 踩坑记录）。优雅阶段已经在 `run_server` 里给过了，
    走到这里再卡就是白白浪费用户的时间。
    """
    try:
        code = _cli_main()
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
    except SystemExit as exc:  # pragma: no cover
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


if __name__ == "__main__":  # pragma: no cover
    main_entry()
