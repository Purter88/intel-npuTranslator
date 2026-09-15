"""`nputweb` 命令行入口（WebUI（nputweb））。

与 `nputr` 的关系：**并列的两个命令**，共用同一套 engine / orchestrate，
但 IO 模型完全不同 —— `nputr` 是一次性管道工具（stdout 只有译文），
`nputweb` 是常驻服务（stdout 是给人看的启动信息）。

## 三条默认就收紧的规则（D6 / D7）

| 情形 | 处理 |
|---|---|
| 绑定非回环地址 | **强制 token**（没给就自动生成并打印一次） |
| 启用了 TLS | **强制 token**（自签证书只防嗅探，不防冒充） |
| 非回环 + 明文 | **拒绝启动**（退出码 3），除非显式 `--allow-insecure` |
| 非回环 + 弱 token | **拒绝启动**（退出码 2），见 D11 |

第 3 条是本项目最现实的事故：手滑把明文服务开到局域网。宁可让用户多敲一个参数。
第 4 条是同一个事故的弱口令版本：`--token 1234` 绑到局域网，等于给整层楼发 PIN。
回环场景一律只警告不拦 —— 那儿的攻击者得先能在本机跑代码。

## 退出路径为什么和 CLI 相反

CLI 是「译文打完立刻 `os._exit`」（关停阶段会卡在 OpenVINO 的 daemon 线程上，见踩坑记录）。
常驻服务不能这么干 —— 至少要给 uvicorn 一个优雅 shutdown 的机会（关连接、停线程池）。
但**也不能无限等**：NPU 那段 `generate()` 可能卡在原生调用里（R9），
所以「优雅 → 超时 3 s → `os._exit` 兜底」。两条容错都要有，缺一条都是 bug。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from dataclasses import dataclass
from typing import Optional

import typer

from .. import __version__
from .. import config as cfg
from ..orchestrate import OrchestrateConfig, Translator
from ..pool import cpu_pipeline_props
from . import DEFAULT_HOST, DEFAULT_MAX_INPUT_CHARS, DEFAULT_PORT, DEFAULT_QUEUE_SIZE, DEFAULT_RATE_PER_MIN, DEFAULT_TIMEOUT_S
from .auth import HostPolicy, TokenChecker, generate_token, is_loopback
from .limits import QueueGate, RateLimiter
from .tls import TlsError, resolve_tls

# 退出码沿用 CLI 管道契约 的语义
EXIT_OK = 0
EXIT_USAGE = 2     # 参数错误：证书缺一半、非法 host、非回环 + 明文
EXIT_STARTUP = 3   # 启动失败：端口占用、证书加载失败、引擎加载失败
EXIT_INTERRUPTED = 130

_GRACE_SECONDS = 3.0  # 优雅 shutdown 的上限，超时就硬退

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    rich_markup_mode=None,  # 与 nputr 同理：rich 的可选依赖未必装全
    help="nputweb：本地离线翻译的 Web 界面（独立命令，HTTPS 默认自签证书）",
)


@dataclass
class Options:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    tls: str = "auto"
    cert: Optional[str] = None
    key: Optional[str] = None
    token: Optional[str] = None
    no_auth: bool = False
    allow_insecure: bool = False
    open_browser: bool = True
    device: str = cfg.DEVICE
    newline: str = "soft"
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS
    timeout: float = DEFAULT_TIMEOUT_S
    queue_size: int = DEFAULT_QUEUE_SIZE
    rate: int = DEFAULT_RATE_PER_MIN
    debug: bool = False
    no_warmup: bool = False


def _env_str(name: str, default: str) -> str:
    return os.getenv(name) or default


def options_from_env() -> Options:
    """读 `NPT_WEB_*` 环境变量作为**默认值**（命令行显式给值则覆盖）。"""
    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    return Options(
        host=_env_str("NPT_WEB_HOST", DEFAULT_HOST),
        port=_int("NPT_WEB_PORT", DEFAULT_PORT),
        tls=_env_str("NPT_WEB_TLS", "auto"),
        cert=os.getenv("NPT_WEB_CERT") or None,
        key=os.getenv("NPT_WEB_KEY") or None,
        token=os.getenv("NPT_WEB_TOKEN") or None,
        no_auth=_bool("NPT_WEB_NO_AUTH", False),
        allow_insecure=_bool("NPT_WEB_ALLOW_INSECURE", False),
        # NPT_WEB_OPEN=0 表示不自动开浏览器
        open_browser=_bool("NPT_WEB_OPEN", True),
        device=_env_str("NPT_DEVICE", cfg.DEVICE),
        max_input_chars=_int("NPT_WEB_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS),
        timeout=float(_int("NPT_WEB_TIMEOUT", DEFAULT_TIMEOUT_S)),
        queue_size=_int("NPT_WEB_QUEUE", DEFAULT_QUEUE_SIZE),
        rate=_int("NPT_WEB_RATE", DEFAULT_RATE_PER_MIN),
        debug=_bool("NPT_WEB_DEBUG", False),
    )


# ---------------------------------------------------------------- 校验与解析
def resolve_binding(opts: Options) -> tuple[str, TokenChecker, bool]:
    """按 D6 / D7 把「绑定地址 + TLS + 认证」这条三角关系定下来。

    :return: (scheme, TokenChecker, 是否已启用 TLS)
    """
    from .tls import TlsMode, parse_mode

    # ---- TLS 三态
    try:
        mode = parse_mode(opts.tls)
    except TlsError as exc:
        raise SystemExit(f"参数错误: {exc}") from exc
    tls_enabled = mode is not TlsMode.OFF

    # ---- D7：非回环 + 明文 → 拒绝启动（除非显式放行）
    if not is_loopback(opts.host) and not tls_enabled and not opts.allow_insecure:
        typer.secho(
            "拒绝启动：把明文 HTTP 绑到非回环地址会把翻译服务暴露给整个局域网。\n"
            "  要么 --tls auto/on（推荐），要么确认风险后加 --allow-insecure。",
            err=True, fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_STARTUP)

    # ---- D6：非回环或启用 TLS → 强制 token
    must_auth = (not is_loopback(opts.host)) or tls_enabled
    if must_auth and opts.no_auth and not is_loopback(opts.host):
        typer.secho("参数错误: 非回环绑定不允许 --no-auth", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_USAGE)

    if opts.no_auth and not must_auth:
        checker = TokenChecker(None)
    else:
        given = opts.token or None
        checker = TokenChecker(given or generate_token())
        if not given:
            # 只打印这一次 —— 拿不到别的途径再问它要了
            typer.secho(f"已自动生成访问 token：{checker.token}", fg=typer.colors.YELLOW)
            typer.secho("（它不会再出现第二次，请从上面的链接里复制保存）", fg=typer.colors.BRIGHT_BLACK)

    # ---- D11：强度校验挂在**最终生效的 checker** 上，而不是挂在「token 从哪来」上。
    # 为什么挪到这里、且不再判断 `if opts.token:`：
    #   1. 安全性不该依赖「调用方记得调」—— 按来源判断的话，将来多一个 token 来源
    #      （配置文件 / stdin / 别的入口）就会**静默绕过**强度校验，这种漏法 review 极难发现。
    #      现在无论 token 是手输的、环境变量来的、还是自动生成的，只要它最终生效就必过这一关。
    #   2. 自动生成的是 256 bit 随机串，`assess_token` 判强 → 这里天然不触发，不会误伤。
    #   3. 顺带修掉一个噪音：旧写法在 `--no-auth --token 1234`（回环明文）下会警告一枚
    #      **根本不会被使用**的 token。现在 `checker.enabled` 为假，直接跳过。
    # 放在 D7 之后：D7 的「非回环 + 明文」退出码（3）不能被这里的 2 抢先。
    if checker.enabled and checker.weak:
        if is_loopback(opts.host):
            typer.secho(
                f"警告: --token 强度不足（{checker.weak_reason}）。\n"
                "  本机回环访问暂且放行，但别把它用在跨机 / 公网场景。",
                err=True, fg=typer.colors.YELLOW,
            )
        else:
            typer.secho(
                f"拒绝启动：--token 强度不足（{checker.weak_reason}），而绑定地址不是回环。\n"
                "  局域网里的任何人都能试着猜它 —— 请换一个 16 位以上、"
                "混合大小写/数字/符号的令牌，\n"
                "  或者干脆不给 --token：会自动生成一枚 256 bit 的随机 token。",
                err=True, fg=typer.colors.RED,
            )
            raise typer.Exit(code=EXIT_USAGE)

    return ("https" if tls_enabled else "http"), checker, tls_enabled


def resolve_listen_host(host: str) -> str:
    """把 `0.0.0.0` 之类解析成一个**可访问的**具体地址用于打印。

    给用户的横幅里写 `https://0.0.0.0:8765` 是没有意义的 ——
    那不是可以点进去的地址，用户还得自己查本机 IP。
    """
    if host in {"0.0.0.0", "::", "*", ""}:
        try:
            ip = _primary_ip()
        except OSError:
            return "127.0.0.1"
        return ip or "127.0.0.1"
    return host


def _primary_ip() -> str:
    """出网那张网卡的 IP。**不发任何包**（UDP connect 只是让内核填路由表）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def check_port_free(host: str, port: int) -> None:
    """端口被占用 → **明确报错**（退出码 3）。

    绝对不要悄悄改成 port+1：用户会在旧实例上找半天"我刚才启动的服务呢"，
    而旧实例可能跑着完全不同的配置。端口冲突必须让用户知道。
    """
    if port == 0:
        return  # 系统随机分配，无从冲突
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host if host not in {"0.0.0.0", "::", "*"} else "", port))
    except OSError as exc:
        raise SystemExit(
            f"端口 {port} 已被占用（{exc.strerror or exc}）。"
            f"换一个 --port，或者先关掉占用它的进程。"
        ) from exc
    finally:
        sock.close()


# ---------------------------------------------------------------- 服务器
def build_context(opts: Options) -> tuple[object, TokenChecker, list[str], object]:
    """构造运行时上下文。返回 `(ServerContext, TokenChecker, devices, TlsPlan)`。"""
    from .app import SecurityConfig, ServerContext

    try:
        tls_plan = resolve_tls(opts.tls, opts.cert, opts.key, bind_host=opts.host)
    except TlsError as exc:
        typer.secho(f"参数错误: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=EXIT_USAGE) from exc

    # resolve_binding 会因为 D7 直接终止进程，所以它必须跑在建 context **之前**

    scheme, checker, tls_enabled = resolve_binding(opts)

    translator = Translator(
        OrchestrateConfig(
            target="en", device=opts.device, newline=opts.newline,
            cpu_props=cpu_pipeline_props("0", "any", None),
        ),
        on_degrade=lambda name, err: typer.secho(
            f"设备 {name} 不可用，已降级：{err}", err=True, fg=typer.colors.YELLOW),
    )

    ctx = ServerContext(
        translator=translator,
        token=checker,
        host_policy=HostPolicy.build(opts.host),
        limiter=RateLimiter(per_minute=opts.rate),
        gate=QueueGate(max_pending=opts.queue_size),
        security=SecurityConfig(
            debug=opts.debug,
            max_input_chars=opts.max_input_chars,
            timeout_s=opts.timeout,
            queue_size=opts.queue_size,
            rate_per_min=opts.rate,
            https=tls_enabled,
            ssl_certfile=tls_plan.certfile,
            ssl_keyfile=tls_plan.keyfile,
        ),
    )
    return ctx, checker, translator.devices, tls_plan


def print_banner(opts: Options, display_host: str, port: int, scheme: str,
                 checker: TokenChecker, devices: list[str], fingerprint: str) -> None:
    """启动横幅：**必须**告诉用户「在哪个端口上」（原始需求明确要求）。"""
    query = f"?token={checker.token}" if checker.enabled else ""
    TyperColors = typer.colors

    typer.secho("")
    typer.secho("nputweb 已就绪（一键停止：Ctrl+C）", fg=TyperColors.GREEN)
    typer.secho(f"  本地：    {scheme}://127.0.0.1:{port}/{query}")
    if display_host not in {"127.0.0.1", "localhost"}:
        typer.secho(f"  局域网：  {scheme}://{display_host}:{port}/{query}")
    if scheme == "https":
        typer.secho(f"  证书：    自签发 · SHA-256 指纹 {fingerprint}")
        typer.secho("            （请核对与首次一致，不一致说明有中间人）",
                    fg=TyperColors.BRIGHT_BLACK)
        # 为什么要特意提这一句：自签证书**只该在本机浏览器里点「继续访问」放行**。
        # 一旦被加进系统信任库，它就成了用户机器上的信任锚 —— 哪怕是普通服务端证书，
        # 留在信任库里也是个长期的后门面（删掉时几乎没人会想起来）。指纹核对才是正解。
        typer.secho("            （别把它加入系统信任库：卸掉时不会有人想起它）",
                    fg=TyperColors.BRIGHT_BLACK)
    else:
        typer.secho("  明文 HTTP：未加密 —— 本机回环访问尚可，请勿跨机使用",
                    fg=TyperColors.YELLOW)
    chain = " → ".join(devices) if devices else "?"
    # 只有一个设备时没有"链"可言，别把「NPU」硬说成「回退链 NPU」——那是误导
    if len(devices) > 1:
        device_line = f"  设备：    {devices[0]}（回退链 {chain}）"
    else:
        device_line = f"  设备：    {chain}"
    typer.secho(f"{device_line}  引擎：加载中（/api/health 会显示 loading → ready）")
    typer.secho("  日志：    只记录请求长度与耗时，**不记录原文**", fg=TyperColors.BRIGHT_BLACK)
    typer.secho("")


async def _serve(ctx: object, opts: Options) -> None:
    """起 uvicorn。engine 的预热在主线程另一个 thread 里做，不阻塞监听。"""
    import uvicorn

    from .app import create_app

    sec = ctx.security  # type: ignore[attr-defined]
    config = uvicorn.Config(
        create_app(ctx),                       # type: ignore[arg-type]
        host=opts.host,
        port=opts.port,
        ssl_keyfile=sec.ssl_keyfile or None,
        ssl_certfile=sec.ssl_certfile or None,
        # ★ Windows 没有 uvloop；别在这里写死 loop 类型，让 uvicorn 自己挑
        access_log=False,                      # 默认关：它会记完整 URL（含 token）
        log_level="debug" if opts.debug else "warning",
        server_header=False,                   # 少一个指纹信息
        date_header=False,
    )
    server = uvicorn.Server(config)
    ctx._server = server  # type: ignore[attr-defined]
    await server.serve()


def run_server(ctx: object, opts: Options) -> int:
    """在**后台线程**里跑 uvicorn，主线程专职等 Ctrl+C。

    为什么不直接在主线程 `asyncio.run`：这里要精确控制「优雅 → 超时 → 硬退」这条链。
    主线程收到 KeyboardInterrupt（信号只进主线程）后给 uvicorn 打 `should_exit`，
    等 `_GRACE_SECONDS`；还活着就直接 `os._exit` —— NPU 那段 generate 可能卡在
    原生调用里（R9），等它等于永远关不掉。
    """
    import asyncio

    def worker() -> None:
        try:
            asyncio.run(_serve(ctx, opts))
        except Exception as exc:  # noqa: BLE001 - 启动失败要让用户看见，而不是静默退出
            typer.secho(f"服务启动失败: {type(exc).__name__}: {exc}", err=True,
                        fg=typer.colors.RED)

    thread = threading.Thread(target=worker, daemon=True, name="nputweb-server")
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
            ctx.shutdown(wait=False)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 关停阶段没有可恢复动作
            pass
        return EXIT_INTERRUPTED
    return EXIT_OK


# ---------------------------------------------------------------- typer 绑定
def merge(opts: Options, **cli_values: object) -> Options:
    """把命令行给出的值合并进环境默认值。

    ⚠️ 刻意**不用** `ctx.get_parameter_source()`：typer 0.27 自带一份 click，
    `ParameterSource` 与真 click 的那份是两个枚举类，`==` 恒为 False
    （踩坑记录）。用 `None` 当"我没给"的信号，绕开整个问题。
    """
    for key, value in cli_values.items():
        if value is not None:
            setattr(opts, key, value)
    return opts


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", help="绑定地址（默认 127.0.0.1；非回环会强制 token）"),
    port: Optional[int] = typer.Option(None, "--port", help="端口（默认 8765；被占用则报错，0 = 系统分配）"),
    tls: Optional[str] = typer.Option(None, "--tls", help="auto=自签（默认）| on=用 --cert/--key | off=明文"),
    cert: Optional[str] = typer.Option(None, "--cert", help="证书路径（--tls on 时必填）"),
    key: Optional[str] = typer.Option(None, "--key", help="私钥路径（--tls on 时必填）"),
    token: Optional[str] = typer.Option(None, "--token", help="访问令牌；不给则自动生成并打印一次"),
    no_auth: bool = typer.Option(False, "--no-auth", help="关闭鉴权（**仅回环地址允许**）"),
    allow_insecure: bool = typer.Option(
        False, "--allow-insecure", help="放行「非回环 + 明文」这一危险组合"),
    no_open: bool = typer.Option(False, "--no-open", help="不自动打开浏览器"),
    device: Optional[str] = typer.Option(None, "--device", "-d", help="npu | cpu | gpu | auto | hetero"),
    newline: Optional[str] = typer.Option(None, "--newline", help="soft | hard | auto（语义同 nputr）"),
    max_input_chars: Optional[int] = typer.Option(None, "--max-input-chars", help="单次输入字符上限"),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="单请求超时秒数"),
    queue_size: Optional[int] = typer.Option(None, "--queue-size", help="队列上限，超出返回 503"),
    rate: Optional[int] = typer.Option(None, "--rate", help="单 IP 每分钟请求上限，超出返回 429"),
    debug: bool = typer.Option(False, "--debug", help="开启 /docs 与脱敏 access log"),
    no_warmup: bool = typer.Option(False, "--no-warmup", help="跳过启动预热（首次请求会更慢）"),
    version: bool = typer.Option(False, "--version", help="显示版本后退出"),
) -> None:
    """启动 Web 界面：默认 https://127.0.0.1:8765（自签证书）。

    **安全性 > 稳定性 > 效率**：非回环绑定或启用 TLS 一律强制 token；
    「非回环 + 明文」默认拒绝启动（除非 --allow-insecure）。
    """
    if ctx.invoked_subcommand is not None:
        return
    if version:
        typer.echo(f"nputweb {__version__}")
        return

    opts = merge(
        options_from_env(),
        host=host, port=port, tls=tls, cert=cert, key=key, token=token,
        device=device, newline=newline, max_input_chars=max_input_chars,
        timeout=timeout, queue_size=queue_size, rate=rate,
    )
    # bool 型：命令行开关只能"加"，env 只能"减"。用 or 合并，`False` 不会覆盖 env 的 True
    opts.no_auth = opts.no_auth or no_auth
    opts.allow_insecure = opts.allow_insecure or allow_insecure
    opts.debug = opts.debug or debug
    opts.no_warmup = opts.no_warmup or no_warmup
    if no_open:
        opts.open_browser = False

    code = start_server(opts)
    if code:
        raise typer.Exit(code=code)


def start_server(opts: Options) -> int:
    """把服务跑起来并阻塞到退出。返回退出码（测试与外部调用者用得着）。"""
    # ① 端口先查：默认的 8765 被别的实例占着是最常见的情况，早点说清楚
    try:
        check_port_free(opts.host, opts.port)
    except SystemExit as exc:
        typer.secho(f"启动失败: {exc}", err=True, fg=typer.colors.RED)
        return EXIT_STARTUP

    # ② 证书 + 绑定规则的合法性（含 D7 的拒绝启动）
    try:
        build = build_context(opts)
    except typer.Exit as exc:
        return exc.exit_code if isinstance(exc.exit_code, int) else EXIT_USAGE
    server_ctx, checker, devices, tls_plan = build

    display_host = resolve_listen_host(opts.host)
    scheme = "https" if getattr(server_ctx, "https", False) else "http"
    real_port = opts.port

    # ③ 后台预热：NPU 首次编译约 30 s，不能拖住监听（否则用户以为启动失败）
    if not opts.no_warmup:
        translator = server_ctx.translator  # type: ignore[attr-defined]
        threading.Thread(target=_warmup, args=(translator,), daemon=True,
                         name="nputweb-warmup").start()

    print_banner(opts, display_host, real_port, scheme, checker,
                 devices, getattr(tls_plan, "fingerprint", "") or "")

    # ④ 自动开浏览器：只在回环地址时默认开（远程开浏览器没意义）
    if opts.open_browser and is_loopback(display_host):
        threading.Timer(1.0, _open_browser,
                        args=(f"{scheme}://127.0.0.1:{real_port}/"
                              f"{'?token=' + checker.token if checker.enabled else ''}",)
                        ).start()

    if scheme == "http" and not is_loopback(opts.host):
        typer.secho("⚠️ 当前是**明文 HTTP + 非回环**：局域网内任何人都能用你的 NPU",
                    err=True, fg=typer.colors.RED)

    return run_server(server_ctx, opts)


def _warmup(translator: object) -> None:
    """预热翻译引擎。失败也**不能**让服务起不来 —— 降级为「每次请求才加载」。"""
    try:
        translator.prepare()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"引擎预热失败，将在首次请求时重试: {type(exc).__name__}: {exc}",
                    err=True, fg=typer.colors.YELLOW)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 开不了浏览器不影响服务本身
        pass


def _cli_main() -> int:
    """CLI 主逻辑：只返回退出码（与 nputr 的约定一致，便于子进程测试）。"""
    from ..encoding import configure_stdio

    configure_stdio()
    cmd = typer.main.get_command(app)
    cmd.allow_interspersed_args = True
    try:
        cmd(prog_name="nputweb")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


def main_entry() -> None:
    """console script 入口。

    为什么这里也 `os._exit`：卡住关停的往往不是正在翻译这件事，而是 uvicorn /
    OpenVINO 留下的线程 join 不上（踩坑记录）。优雅阶段已经在
    `run_server` 里给过了，走到这里再卡就是白白浪费用户的时间。
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
