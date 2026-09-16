"""`nputweb` 命令行入口（SPEC.md · WebUI（nputweb））。

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
| 上面三条 + `--allow-no-auth` | 一律**降级为警告**放行（测试 / 可信局域网逃生舱） |

第 3 条是本项目最现实的事故：手滑把明文服务开到局域网。宁可让用户多敲一个参数。
第 4 条是同一个事故的弱口令版本：`--token 1234` 绑到局域网，等于给整层楼发 PIN。
回环场景一律只警告不拦 —— 那儿的攻击者得先能在本机跑代码。

## 逃生舱 `--allow-no-auth`

联调 / 压测 / 家庭可信局域网下，上面三条硬拦每一次都要多敲一个参数，很烦，
于是给一枚**一把全解**的开关：加了它，非回环下的三道闸全部从「拒绝启动」降级为「警告」。

- 它**不**自动关鉴权：`--no-auth` 仍然是「我要关鉴权」这个意图的唯一表达方式。
  只给 `--allow-no-auth` 得到的是「网络可信，但 token 照旧」—— 这本身就是个合理组合。
- 全解是用户**显式选的**（就是要最省事的那一档），所以横幅与 stderr 都必须红字写清
  当前是什么姿态：宁可啰嗦，也不能让人在不知情的状态下开着无鉴权的服务。

## `--allow-host`：给「按域名访问」留的唯一口子

本机可以被叫成千上万种名字（短名 / FQDN / `xxx.local` / `hosts` 里的别名），
**一个都不自动推导** —— 推导等于把「谁被允许」交给当时的 DNS 配置
（含 DHCP 下发的搜索后缀）。要按某个名字访问，就显式声明它；
不声明的结果与「输错 IP」完全一致：直接拒，不给任何提示以外的东西。

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
from typing import List, Optional

import typer

from .. import __version__
from .. import config as cfg
from ..orchestrate import OrchestrateConfig, Translator
from ..pool import cpu_pipeline_props
from . import DEFAULT_HOST, DEFAULT_MAX_INPUT_CHARS, DEFAULT_PORT, DEFAULT_QUEUE_SIZE, DEFAULT_RATE_PER_MIN, DEFAULT_TIMEOUT_S
from .auth import (
    HostPolicy,
    TokenChecker,
    generate_token,
    has_non_loopback,
    host_list,
    is_loopback,
    network_addresses,
    parse_hosts,
)
from .limits import QueueGate, RateLimiter
from .tls import TlsError, resolve_tls

# 退出码沿用 `SPEC.md · CLI 管道契约` 的语义
EXIT_OK = 0
EXIT_USAGE = 2     # 参数错误：证书缺一半、非法 host、非回环 + 明文
EXIT_STARTUP = 3   # 启动失败：端口占用、证书加载失败、引擎加载失败
EXIT_INTERRUPTED = 130

_GRACE_SECONDS = 3.0  # 优雅 shutdown 的上限，超时就硬退

#: 通配绑定地址。**不是**可以点进去访问的地址 —— 横幅里必须展开成本机地址
#: （`https://0.0.0.0:8765` 这种写法对用户毫无意义）。
WILDCARDS = frozenset({"0.0.0.0", "::", "*", ""})

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    rich_markup_mode=None,  # 与 nputr 同理：rich 的可选依赖未必装全
    help="nputweb：本地离线翻译的 Web 界面（独立命令，HTTPS 默认自签证书）",
)


@dataclass
class Options:
    # ---- 绑定地址
    # `hosts` 是**唯一真源**；`host` 降级成「第一个」的兼容别名。
    #
    # 为什么不直接把 `host` 改成 tuple：两个 CLI 与约 20 处既有单测都是
    # `Options(host="127.0.0.1")` 的写法，全量改是纯噪音；保留 `str` 别名后
    # 旧调用一行不动，新代码一律读 `hosts`。别名由 `sync_hosts()` 单向派生，
    # 不存在「两个字段各自漂移」的窗口 —— 写 `host` 的人下一次 sync 就会被覆盖。
    host: str = DEFAULT_HOST
    hosts: tuple[str, ...] = ()
    port: int = DEFAULT_PORT
    # 横幅是否展开虚拟网卡 / 点对点地址。默认折叠：实测本机 12 个 IPv4 里
    # 有 8 个在 VMware / WSL / Hyper-V / VPN 隧道上，全打出来是纯噪音，
    # 而且会诱导用户把 `10.8.0.x/30` 这种隧道地址发给同事。
    show_all_addresses: bool = False
    # 额外放行的 Host 名字（`--allow-host`，可重复给）。
    # 默认空 —— 没声明就是**不放行**，结果与「输错 IP」完全一致。
    # 刻意**不**自动推导本机主机名 / FQDN / `.local`：理由见 `HostPolicy` 的 docstring。
    allow_hosts: tuple[str, ...] = ()
    tls: str = "auto"
    cert: Optional[str] = None
    key: Optional[str] = None
    token: Optional[str] = None
    no_auth: bool = False
    allow_insecure: bool = False
    allow_no_auth: bool = False
    open_browser: bool = True
    device: str = cfg.DEVICE
    newline: str = "soft"
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS
    timeout: float = DEFAULT_TIMEOUT_S
    queue_size: int = DEFAULT_QUEUE_SIZE
    rate: int = DEFAULT_RATE_PER_MIN
    debug: bool = False
    no_warmup: bool = False

    def sync_hosts(self) -> None:
        """把 `host` / `hosts` 归一化成一致状态。

        ⚠️ `merge()` 是 `setattr`，**不会**触发 `__post_init__`，
        所以命令行合并完之后必须再调一次（两个 CLI 的 `main()` 都调了，
        `start_server()` 入口再兜一次底 —— 外部调用者可能直接构造 Options）。

        归一化规则：`hosts` 有值就以它为准，否则从 `host` 派生；
        逗号串在两种写法里都切分。然后 `host = hosts[0]`。
        """
        hosts = host_list(self.hosts or (self.host,))
        self.hosts = hosts or (DEFAULT_HOST,)
        self.host = self.hosts[0]

    def __post_init__(self) -> None:
        self.sync_hosts()


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
        show_all_addresses=_bool("NPT_WEB_SHOW_ALL_ADDRESSES", False),
        allow_hosts=parse_hosts(os.getenv("NPT_WEB_ALLOWED_HOSTS")),
        tls=_env_str("NPT_WEB_TLS", "auto"),
        cert=os.getenv("NPT_WEB_CERT") or None,
        key=os.getenv("NPT_WEB_KEY") or None,
        token=os.getenv("NPT_WEB_TOKEN") or None,
        no_auth=_bool("NPT_WEB_NO_AUTH", False),
        allow_insecure=_bool("NPT_WEB_ALLOW_INSECURE", False),
        allow_no_auth=_bool("NPT_WEB_ALLOW_NO_AUTH", False),
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
    """按 D6 / D7 / D11 把「绑定地址 + TLS + 认证」这条三角关系定下来。

    `--allow-no-auth` 是唯一能同时解开三道闸的开关（测试 / 可信局域网逃生舱）：
    它把「拒绝启动」降级为「警告」，但**不**替用户表达 `--no-auth` 这个意图 ——
    只给逃生舱而没给 `--no-auth`，拿到的仍是「网络可信，token 照旧」。

    :return: (scheme, TokenChecker, 是否已启用 TLS)
    """
    from .tls import TlsMode, parse_mode

    # ---- TLS 三态
    try:
        mode = parse_mode(opts.tls)
    except TlsError as exc:
        raise SystemExit(f"参数错误: {exc}") from exc
    tls_enabled = mode is not TlsMode.OFF

    # 🔴 多地址绑定后「是否非回环」必须取 **any**（暴露面是并集不是交集）。
    #    取第一个的话，`--host 127.0.0.1,192.168.1.5` 会被判成纯回环：
    #    不强制 token、不禁明文、不禁弱 token —— 等于在局域网上开一个裸服务。
    #    这是本次改动里唯一能造成真实事故的地方。
    hosts = host_list(getattr(opts, "hosts", None) or opts.host)
    loopback = not has_non_loopback(hosts)
    # ---- 逃生舱：`--allow-no-auth` = 「这段网络我负责」。
    # 用 getattr 取值：nputserve 的 Options 是同名同义的另一个 dataclass（鸭子类型传参），
    # 万一调用方还没这个字段，按 False 处理而不是 AttributeError。
    trusted = bool(getattr(opts, "allow_no_auth", False))
    if trusted and not loopback:
        typer.secho(
            "⚠️ 已启用 --allow-no-auth：非回环下的「强制 token / 拒绝明文 / 拒绝弱 token」"
            "三道闸全部降级为警告。\n"
            "  只用于测试或你确实信任的局域网 —— 同一网段里任何人都能直接用你的 NPU。",
            err=True, fg=typer.colors.YELLOW,
        )

    # ---- D7：非回环 + 明文 → 拒绝启动（除非显式放行，或逃生舱已开）
    if not loopback and not tls_enabled and not opts.allow_insecure and not trusted:
        typer.secho(
            "拒绝启动：把明文 HTTP 绑到非回环地址会把翻译服务暴露给整个局域网。\n"
            "  要么 --tls auto/on（推荐），要么确认风险后加 --allow-insecure，\n"
            "  要么加 --allow-no-auth 一次性解除这条与另外两条硬拦。",
            err=True, fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_STARTUP)

    # ---- D6：非回环或启用 TLS → 强制 token
    # ★ `--no-auth` 是显式意图，**无条件尊重**：旧实现的判据是 `no_auth and not must_auth`，
    #   于是「回环 + 启 TLS + --no-auth」会落到 else 分支自动发一枚 token，
    #   把用户的 --no-auth 静默吞掉 —— 而 `--tls auto` 是默认值，等于 --no-auth 从来没生效过
    #   （2026-09-16 实测：`Options(host="127.0.0.1", tls="auto", no_auth=True)` →
    #   checker.enabled = True）。回环的威胁模型里 TLS 只防嗅探、不防本机浏览器发请求，
    #   鉴权开不开不该由它决定。
    must_auth = (not loopback) or tls_enabled
    if must_auth and opts.no_auth and not loopback and not trusted:
        typer.secho(
            "参数错误: 非回环绑定不允许 --no-auth。\n"
            "  确认这台机器所在的网段可信，请加 --allow-no-auth。",
            err=True, fg=typer.colors.RED,
        )
        raise typer.Exit(code=EXIT_USAGE)

    if opts.no_auth:
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
        # 逃生舱只把「拒绝启动」降级为警告，判定本身照跑 —— 用户仍要看见这枚 token 是弱的
        if loopback or trusted:
            reason = ("  本机回环访问暂且放行，但别把它用在跨机 / 公网场景。"
                      if loopback else
                      "  --allow-no-auth 已放行：请确认所在网段可信。")
            typer.secho(
                f"警告: --token 强度不足（{checker.weak_reason}）。\n" + reason,
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


def resolve_listen_host(host: object) -> str:
    """把通配地址解析成**一个**可访问的具体地址。保留给旧调用方与旧单测。

    新代码一律用 `resolve_listen_addresses()`：**只给一个地址正是本次要修的毛病**。
    旧实现用「UDP connect 一个外部地址」挑出网那张网卡，实测是错的选择 ——
    本机开着 VPN 时它返回的是隧道地址 `10.8.0.x/30`（点对点，别人连不上），
    而真正能分享的 `192.168.1.x/24` 被排在第二位。「出网」与「可被访问」是两件事。

    现在优先挑 `shareable`（内网 / 公网、非虚拟网卡、非点对点）的第一个。
    """
    hosts = host_list(host)
    if not hosts:
        return "127.0.0.1"
    if hosts[0] not in WILDCARDS:
        return hosts[0]
    table = network_addresses()
    for info in table:
        if info.shareable:
            return info.ip
    return table[0].ip if table else "127.0.0.1"


def resolve_listen_addresses(hosts: object, *, show_all: bool = False
                             ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """算出横幅要打印的地址。返回 `(逐行打印的 (地址, 标注), 被折叠的 (地址, 标注))`。

    ## 为什么只打印「bind 集合 ∩ 本机地址」

    无脑枚举全部网卡是错的：实测本机 12 个 IPv4 里 6 个在未启用网卡上、
    4 个在 VMware / WSL / Hyper-V 上、1 个是 /30 隧道。全打出来既吵，
    又会诱导用户把连不上的地址发给同事。**连不上的地址不算"可访问地址"。**

    - 绑了具体地址 → 就是那几个（用户点名的，一律打印，但仍标注虚拟/点对点）
    - 绑了通配地址 → 展开本机**已启用**网卡上非回环、非链路本地的地址
    - 折叠规则只对通配展开生效：`shareable` 之外的（虚拟网卡 / 点对点）
      默认折成一行计数，`show_all` 时全展开
    """
    binds = host_list(hosts)
    wild = any(h in WILDCARDS for h in binds)
    if wild:
        pool = [a.ip for a in network_addresses()]
    else:
        pool = list(binds)

    table = {a.ip: a for a in network_addresses(include_loopback=True)}
    shown: list[tuple[str, str]] = []
    folded: list[tuple[str, str]] = []
    for ip in pool:
        if is_loopback(ip):
            continue
        info = table.get(ip)
        note = _address_note(info)
        # 用户点名的地址无条件打印；通配展开的才折叠
        if (not wild) or show_all or info is None or info.shareable:
            shown.append((ip, note))
        else:
            folded.append((ip, note))
    return shown, folded


def _address_note(info: object) -> str:
    """一个地址的横幅标注：`网卡名 · 公网 / 点对点 / 虚拟网卡`。

    带网卡名是因为用户看 `192.168.1.5` 分辨不出这是 WLAN 还是虚拟机网卡；
    带性质是因为「能连上」不等于「该发出去」。
    """
    if info is None:
        return ""
    bits: list[str] = []
    if getattr(info, "iface", ""):
        bits.append(info.iface)
    kind = getattr(info, "kind", "")
    if kind == "public":
        bits.append("公网")
    elif kind == "point_to_point":
        bits.append("点对点")
    if getattr(info, "virtual", False):
        bits.append("虚拟网卡")
    return " · ".join(bits)


def _address_family(host: str) -> int:
    """按地址选地址族。

    ⚠️ 旧实现**硬编码 `AF_INET`**，于是 `--host ::1` 会抛
    `gaierror: getaddrinfo failed`，被上层当成「端口已被占用」报出来 ——
    一条完全指错方向的诊断（2026-09-17 实测）。多地址绑定必然混入 IPv6，必须修。
    """
    return socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET


def _prepare_listener(sock: "socket.socket") -> None:
    """bind 之前的 socket 选项。Windows 与 POSIX 在这里**必须分道扬镳**。

    实测依据（2026-09-17，Windows 11）：两个都设了 `SO_REUSEADDR` 的 socket，
    后一个**可以成功 bind** 到前一个正在监听的同一个 `addr:port`。
    也就是说旧代码用 `SO_REUSEADDR` 探测端口，在 Windows 上根本测不出冲突 ——
    真撞车时会变成两个进程静默共存、请求随机分流，比直接报错糟得多。

    - Windows：用专有的 `SO_EXCLUSIVEADDRUSE`，别的进程（哪怕它也设了
      `SO_REUSEADDR`）抢不走。这正是服务端要的语义。
    - POSIX：`SO_REUSEADDR` 的含义是「允许 bind 处于 TIME_WAIT 的地址」，
      继续用它 —— 重启服务时不会卡在 TIME_WAIT 上。
    """
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _bind_one(host: str, port: int, backlog: int = 2048) -> "socket.socket":
    """绑一个监听 socket。失败时**自己关掉**再抛，别把半截 socket 丢给调用方。"""
    sock = socket.socket(_address_family(host), socket.SOCK_STREAM)
    try:
        _prepare_listener(sock)
        sock.bind((host, port))
        sock.listen(backlog)
        return sock
    except OSError:
        sock.close()
        raise


def _bind_error(host: str, port: int, exc: OSError) -> SystemExit:
    """把 bind 失败翻译成人话。

    两种失败的原因完全不同，但 Windows 给的错误码都不直白：
    端口被占用（WSAEADDRINUSE）与「这个地址不在本机任何网卡上」
    （WSAEADDRNOTAVAIL）在用户看来都是「起不来」，所以两种都要写出来，
    让用户自己对照 —— 只写「端口被占用」会让 `--host` 打错的人去查端口。
    """
    return SystemExit(
        f"无法绑定 {host}:{port}（{exc.strerror or exc}）。\n"
        f"  端口被占用 → 换一个 --port，或先关掉占用它的进程；\n"
        f"  提示地址无效 → 这个地址不在本机任何已启用的网卡上（--host 给错了）。"
    )


def bind_listeners(hosts: object, port: int,
                   backlog: int = 2048) -> tuple[list, int]:
    """给**每个**绑定地址各绑一个监听 socket，返回 `(sockets, 实际端口)`。

    返回实际端口是因为 `--port 0`：随机端口要 bind 之后才读得到，
    而 N 个 socket 各自 bind 会拿到 **N 个不同端口** —— 那不是
    「一个服务监听多个地址」，那是 N 个互不相干的服务。所以第一个
    socket 拿到端口后，其余复用同一个端口号。

    顺带把「探测端口」和「真正监听」合成一步：旧代码先探测再让 uvicorn 绑，
    中间有 TOCTOU 窗口，而且在 Windows 上因为 `SO_REUSEADDR` 的语义，
    那个探测压根测不出冲突（见 `_prepare_listener`）。
    """
    binds = host_list(hosts) or (DEFAULT_HOST,)
    opened: list = []
    effective = port
    try:
        for host in binds:
            sock = _bind_one(host, effective, backlog)
            opened.append(sock)
            if effective == 0:
                effective = int(sock.getsockname()[1])
    except OSError as exc:
        for sock in opened:
            sock.close()
        raise _bind_error(host, port, exc) from exc
    return opened, effective


def check_port_free(hosts: object, port: int) -> None:
    """端口 / 地址能不能绑 → **明确报错**（退出码 3）。

    绝对不要悄悄改成 port+1：用户会在旧实例上找半天"我刚才启动的服务呢"，
    而旧实例可能跑着完全不同的配置。端口冲突必须让用户知道。

    现在接受多个地址并逐个试绑 —— 只试第一个的话，第二个地址冲突会在
    uvicorn 内部炸出来，错误信息是英文的 traceback。
    """
    if port == 0:
        return  # 系统随机分配，无从冲突
    for host in host_list(hosts) or (DEFAULT_HOST,):
        try:
            _bind_one(host, port).close()
        except OSError as exc:
            raise _bind_error(host, port, exc) from exc


# ---------------------------------------------------------------- 服务器
def build_context(opts: Options) -> tuple[object, TokenChecker, list[str], object]:
    """构造运行时上下文。返回 `(ServerContext, TokenChecker, devices, TlsPlan)`。"""
    from .app import SecurityConfig, ServerContext

    try:
        # ★ extra_hosts 必须和 HostPolicy 用同一份：只补白名单不补 SAN 的话，
        #   请求会先过白名单再撞浏览器那个「证书名字不匹配」——把一道
        #   看不懂的错换成另一道看不懂的错。
        # ★ 两个消费点必须用**同一份**绑定地址列表：只补白名单不补 SAN 的话，
        #   请求会先过白名单再撞浏览器那个「证书名字不匹配」—— 把一道
        #   看不懂的错换成另一道看不懂的错。
        tls_plan = resolve_tls(opts.tls, opts.cert, opts.key,
                               bind_host=opts.hosts, extra_hosts=opts.allow_hosts)
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
        host_policy=HostPolicy.build(opts.hosts, extra=opts.allow_hosts),
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


def _url(scheme: str, host: str, port: int, query: str = "") -> str:
    """拼一个**能点进去**的地址。IPv6 必须带方括号，`http://::1:8765` 是非法 URL。"""
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{shown}:{port}/{query}"


def print_banner(opts: Options, display_hosts: object, port: int, scheme: str,
                 checker: TokenChecker, devices: list[str], fingerprint: str) -> None:
    """启动横幅：**必须**告诉用户「在哪个端口上」（原始需求明确要求）。

    `display_hosts` 可以是单个地址也可以是序列。单个是旧调用方的写法；
    新调用方一律传 `opts.hosts` —— 只打印一个地址正是本次要修的毛病。

    ## 「网络」而不是「局域网」

    本机地址横跨 WLAN、VMware、WSL、Hyper-V 与 VPN 隧道，统称「局域网」不准确。
    但**警告文案里不跟着改**：那里要表达的是「同一网段里的其它设备」这个精确含义，
    换成「网络」会被读成「互联网」，反而稀释警告。
    """
    query = f"?token={checker.token}" if checker.enabled else ""
    TyperColors = typer.colors
    binds = host_list(display_hosts)
    # 只绑了 192.168.x.x 时写 127.0.0.1 是骗人的 —— 那个地址根本连不上。
    # 绑了通配就一定有回环可用，所以这时候回落到 127.0.0.1 是对的。
    if not binds or any(h in WILDCARDS for h in binds):
        local = "127.0.0.1"
    else:
        local = next((h for h in binds if is_loopback(h)), binds[0])
    shown, folded = resolve_listen_addresses(
        binds, show_all=getattr(opts, "show_all_addresses", False))

    typer.secho("")
    typer.secho("nputweb 已就绪（一键停止：Ctrl+C）", fg=TyperColors.GREEN)
    typer.secho(f"  本地：    {_url(scheme, local, port, query)}")
    for index, (ip, note) in enumerate(shown):
        label = "网络：    " if index == 0 else "          "
        typer.secho(f"  {label}{_url(scheme, ip, port, query)}"
                    + (f"（{note}）" if note else ""))
    if folded:
        typer.secho(f"           （另有 {len(folded)} 个虚拟网卡 / 点对点地址未显示，"
                    "加 --show-all-addresses 展开）", fg=TyperColors.BRIGHT_BLACK)
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
    if not checker.enabled and has_non_loopback(opts.hosts):
        typer.secho("  鉴权：    已关闭 + 非回环 —— 同一网段内任何人都能用你的 NPU",
                    fg=TyperColors.RED)
    chain = " → ".join(devices) if devices else "?"
    # 只有一个设备时没有"链"可言，别把「NPU」硬说成「回退链 NPU」——那是误导
    if len(devices) > 1:
        device_line = f"  设备：    {devices[0]}（回退链 {chain}）"
    else:
        device_line = f"  设备：    {chain}"
    typer.secho(f"{device_line}  引擎：加载中（/api/health 会显示 loading → ready）")
    typer.secho("  日志：    只记录请求长度与耗时，**不记录原文**", fg=TyperColors.BRIGHT_BLACK)
    if opts.allow_hosts:
        typer.secho(f"  放行 Host：{', '.join(opts.allow_hosts)}"
                    "（此外只认 localhost 与本机 IP）", fg=TyperColors.BRIGHT_BLACK)
    typer.secho("")


async def _serve(ctx: object, opts: Options, listeners: list | None = None) -> None:
    """起 uvicorn。engine 的预热在主线程另一个 thread 里做，不阻塞监听。

    :param listeners: **预先绑好的**监听 socket。多地址绑定必须走这条路 ——
        `uvicorn.Config` 只认一个 `host`，而 `Server.serve(sockets=[...])` 会逐个
        `loop.create_server(sock=...)`，得到的是「一个 lifespan、一个 app、N 个 listener」。
        起 N 个 `uvicorn.Server` 是错的：lifespan 会跑 N 次，模型加载 N 遍。
        传 `None` 时退回旧行为（uvicorn 自己按 `config.host` 绑一个）。
    """
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
    # 空列表要转成 None：uvicorn 见到 `sockets=[]` 会**一个 listener 都不建**，
    # 服务照样"启动成功"，只是谁都连不上 —— 那种失败静默得可怕。
    await server.serve(sockets=listeners or None)


def run_server(ctx: object, opts: Options, listeners: list | None = None) -> int:
    """在**后台线程**里跑 uvicorn，主线程专职等 Ctrl+C。

    为什么不直接在主线程 `asyncio.run`：这里要精确控制「优雅 → 超时 → 硬退」这条链。
    主线程收到 KeyboardInterrupt（信号只进主线程）后给 uvicorn 打 `should_exit`，
    等 `_GRACE_SECONDS`；还活着就直接 `os._exit` —— NPU 那段 generate 可能卡在
    原生调用里（R9），等它等于永远关不掉。
    """
    import asyncio

    def worker() -> None:
        try:
            asyncio.run(_serve(ctx, opts, listeners))
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
    （SPEC.md · 踩坑记录）。用 `None` 当"我没给"的信号，绕开整个问题。
    """
    for key, value in cli_values.items():
        if value is None:
            continue
        # ★ `host` 只是 `hosts` 的首元素别名。直接 `setattr(opts, "host", ...)`
        #   会让它与 `hosts` 失同步，而随后 `sync_hosts()` 以 `hosts` 为准 ——
        #   结果命令行给的 --host 被静默吞掉（2026-09-17 实测）。
        #   所以命令行写进来的一律落到 `hosts`，`host` 由 sync 派生。
        if key == "host":
            opts.hosts = host_list(value)
        else:
            setattr(opts, key, value)
    return opts


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(
        None, "--host",
        help="绑定地址，逗号分隔可绑多个（默认 127.0.0.1；0.0.0.0 = 全部网卡；任一个非回环就强制 token）"),
    port: Optional[int] = typer.Option(None, "--port", help="端口（默认 8765；被占用则报错，0 = 系统分配）"),
    show_all_addresses: bool = typer.Option(
        False, "--show-all-addresses",
        help="横幅展开虚拟网卡与点对点地址（默认折叠，只留一行计数）"),
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
        help="额外放行的 Host 名（可重复给；也可设 NPT_WEB_ALLOWED_HOSTS，逗号分隔）。"
             "不声明就不放行，与输错地址一样直接拒"),
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
    测试 / 可信局域网可用 `--allow-no-auth` 一次性解除这三条硬拦（降级为警告）。
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
    # 列表型：命令行显式给了就用命令行的；空 / None 都算「没给」，保留 env 的值
    if allow_host:
        opts.allow_hosts = tuple(allow_host)
    # bool 型：命令行开关只能"加"，env 只能"减"。用 or 合并，`False` 不会覆盖 env 的 True
    opts.no_auth = opts.no_auth or no_auth
    opts.show_all_addresses = opts.show_all_addresses or show_all_addresses
    # merge() 是 setattr，不触发 __post_init__ → 合并完必须手动同步 host / hosts
    opts.sync_hosts()
    opts.allow_insecure = opts.allow_insecure or allow_insecure
    opts.allow_no_auth = opts.allow_no_auth or allow_no_auth
    opts.debug = opts.debug or debug
    opts.no_warmup = opts.no_warmup or no_warmup
    if no_open:
        opts.open_browser = False

    code = start_server(opts)
    if code:
        raise typer.Exit(code=code)


def start_server(opts: Options) -> int:
    """把服务跑起来并阻塞到退出。返回退出码（测试与外部调用者用得着）。"""
    opts.sync_hosts()  # 外部调用者可能直接构造 Options，这里兜底

    # ① 证书 + 绑定规则的合法性（含 D7 的拒绝启动）
    #    ★ 刻意放在 bind **之前**：D7 那条「非回环 + 明文」是可执行的提示，
    #      先绑端口的话用户会先撞上「端口被占用」，排查方向直接指错。
    try:
        build = build_context(opts)
    except typer.Exit as exc:
        return exc.exit_code if isinstance(exc.exit_code, int) else EXIT_USAGE
    server_ctx, checker, devices, tls_plan = build

    # ② 真正绑定（端口冲突检测也在这一步完成，见 bind_listeners）
    try:
        listeners, real_port = bind_listeners(opts.hosts, opts.port)
    except SystemExit as exc:
        typer.secho(f"启动失败: {exc}", err=True, fg=typer.colors.RED)
        return EXIT_STARTUP

    scheme = "https" if getattr(server_ctx, "https", False) else "http"

    # ③ 后台预热：NPU 首次编译约 30 s，不能拖住监听（否则用户以为启动失败）
    if not opts.no_warmup:
        translator = server_ctx.translator  # type: ignore[attr-defined]
        threading.Thread(target=_warmup, args=(translator,), daemon=True,
                         name="nputweb-warmup").start()

    print_banner(opts, opts.hosts, real_port, scheme, checker,
                 devices, getattr(tls_plan, "fingerprint", "") or "")

    # ④ 自动开浏览器：绑了回环才开（只绑远端地址时本地浏览器连不上）
    if opts.open_browser and any(is_loopback(h) for h in opts.hosts):
        threading.Timer(1.0, _open_browser,
                        args=(f"{scheme}://127.0.0.1:{real_port}/"
                              f"{'?token=' + checker.token if checker.enabled else ''}",)
                        ).start()

    if scheme == "http" and has_non_loopback(opts.hosts):
        typer.secho("⚠️ 当前是**明文 HTTP + 非回环**：同一网段内任何人都能用你的 NPU",
                    err=True, fg=typer.colors.RED)
    if not checker.enabled and has_non_loopback(opts.hosts):
        typer.secho("⚠️ 当前是**无鉴权 + 非回环**：同一网段任何人都能直接用你的 NPU，"
                    "连 token 都不用猜", err=True, fg=typer.colors.RED)

    return run_server(server_ctx, opts, listeners)


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
    OpenVINO 留下的线程 join 不上（SPEC.md · 踩坑记录）。优雅阶段已经在
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
