"""鉴权与 Host 白名单（SPEC.md · WebUI（nputweb））。

## 为什么有 token —— 以及为什么「只绑 127.0.0.1」不算安全

恶意网页里的 JS 可以直接请求 `http://127.0.0.1:8765`（DNS rebinding 只是一个变种）。
浏览器只会放行目标清单上的 127.0.0.1**，同源策略拦不住**这种请求 —— 它拦的是「读响应」，
而『发请求』本身早就发出去了。所以：

- 只绑回环 ≠ 只有你能用
- 自签 TLS 只防嗅探，**不防冒充**：局域网里任何人点一下「继续访问」就能用你的 NPU

→ D6 的规则因此成立：**非回环绑定 或 启用 TLS → 强制 token**；纯回环明文才可能免认证。

## token 为什么走 `Authorization` header 而不是 cookie

1. cookie 会被浏览器自动带上 → 恶意页能直接带着用户的身份发请求（CSRF）
2. header 必须页面自己显式设置 → 恶意页拿不到 `sessionStorage`（跨源隔离），也就拼不出这个头

顺带：不用 cookie 就天然免 CSRF，不需要再搞 CSRF token。

## 为什么不接受 URL 里的 `?token=`

URL 会进浏览器历史、会进 uvicorn 的 access log、会进 Referer。
启动横幅里的 `?token=` 只是为了让「自动开浏览器」这一步能把 token 送到页面上，
页面拿到后**立刻** `history.replaceState` 抹掉，之后一律走 header。
服务端因此**不读** query —— 少一个泄漏面，也少一条兼容路径。

## 为什么要给**用户自己给**的 token 做强度校验（D11）

自动生成的 token 是 `secrets.token_urlsafe(32)`（43 字符 / 256 bit），爆破无望。
但 `--token 1234` 是手输的 —— 人手输得出的东西几乎必然落在低熵空间里
（`1234` / `abc123` / 生日 / 手机号）。回环地址上这还能忍（攻击面是本机浏览器），
非回环地址上这是把翻译服务直接挂到局域网里让人猜 PIN。
**所以：弱 token 只拦非回环，回环只警告。**
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import socket
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "AddressInfo",
    "MIN_TOKEN_LENGTH",
    "TokenChecker",
    "assess_token",
    "generate_token",
    "has_non_loopback",
    "host_allows",
    "host_list",
    "is_loopback",
    "is_weak_token",
    "local_ip_candidates",
    "network_addresses",
    "parse_hosts",
    "safe_token_equal",
    "strip_port",
]

# 每次猜中的先验概率 enough：32 字节 ≈ 256 bit，URL-safe base64 后 43 字符
DEFAULT_TOKEN_BYTES = 32

# ---- 弱 token 判据（D11）。阈值取「人肯手输」与「不可穷举」的交点，逐条说理由：
#
# 1. 长度 < 16 → 弱。
#    16 位混合字符 ≈ 95 bit；而 8 位纯数字只有 10^8 ≈ 27 bit —— 局域网里不限速的话
#    几分钟就能撞完，限流只是缓解不是根治。
#    取 16 而不是 20/32：再长就没人肯手输了，用户会去抄便利贴，反而更不安全。
#    自动生成的 43 字符远高于此，这条**误伤不到**它。
MIN_TOKEN_LENGTH = 16

# 2. 去重后字符种类 < 5 → 弱。
#    `aaaaaaaaaaaaaaaa` / `abababababababab` 长度是够的，熵却只有 1~2 bit/字符。
#    5 是「看起来像随机串」的下限（十六进制串 16 种，base64 64 种）。
_MIN_DISTINCT_CHARS = 5

# 3. 全数字 → 弱。
#    字符集只有 10 种，而人挑的数字串高度集中在 123456 / 000000 / 手机号 / 生日，
#    **实际分布**远不是均匀的 —— 攻击者会先试字典，不是先穷举。
#
# 4. 只用到一种字符类（全小写 / 全大写 / 全数字 / 全符号）→ 弱。
#    16 位全小写理论上有 75 bit，但「全小写 + 人自己想」基本等价于一个英文单词
#    或拼音串，字典攻击比穷举便宜得多。要求**至少两类**字符，是把「人想出来的词」
#    挡在门外最省事的办法。
#
# 这些判据只用来**拦非回环绑定**；回环一律只警告不拦 —— 那儿的攻击者得先能在本机
# 跑代码，token 强度已经不是主要防线了，硬拦只会徒增摩擦。


def _char_classes(token: str) -> int:
    """用到了几类字符：小写 / 大写 / 数字 / 其它（符号）。"""
    classes = 0
    if any(c.islower() for c in token):
        classes += 1
    if any(c.isupper() for c in token):
        classes += 1
    if any(c.isdigit() for c in token):
        classes += 1
    if any(not c.isalnum() for c in token):
        classes += 1
    return classes


def assess_token(token: str) -> tuple[bool, str]:
    """评估 token 强度。返回 `(是否够强, 不够强的原因)` —— 原因要能直接打印给用户看。

    刻意写成纯函数：不看配置、不碰 IO，方便单测，也方便 CLI 在任何阶段调用。
    """
    if not token:
        return False, "token 为空"
    if len(token) < MIN_TOKEN_LENGTH:
        return False, f"只有 {len(token)} 位，低于 {MIN_TOKEN_LENGTH} 位的下限"
    if token.isdigit():
        return False, "全为数字（123456 / 生日这类，字典一试就中）"
    distinct = len(set(token))
    if distinct < _MIN_DISTINCT_CHARS:
        return False, f"只用到 {distinct} 种不同字符（形如 aaaa… / abab…）"
    if _char_classes(token) < 2:
        return False, "只用了一类字符（如全小写）—— 人手挑的这种串多半是个词"
    return True, ""


def is_weak_token(token: str) -> bool:
    """`assess_token` 的布尔视图（给只关心是/否的调用方）。"""
    return not assess_token(token)[0]


_HEADER_SCHEME = "Bearer"


def generate_token(nbytes: int = DEFAULT_TOKEN_BYTES) -> str:
    """生成一个 URL-safe 的随机 token（`secrets` 用它自身的 CSPRNG）。"""
    return secrets.token_urlsafe(max(8, nbytes))


def safe_token_equal(provided: str | None, expected: str | None) -> bool:
    """常量时间比较，防时序侧信道。

    ⚠️ 必须是防线而不是装饰：`==` 在第一个不同字节就返回，观察响应时间就能逐字节试出 token。
    `hmac.compare_digest` 保证耗时只与长度有关。

    两边都为空视为**不匹配**（None 表示没启用 token 或没带凭据，交给上层判断）。
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


@dataclass
class TokenChecker:
    """token 校验器。

    :param token: 期望值；`None` / 空 = **不启用鉴权**（只有纯回环明文才允许这样配）
    """

    token: str | None = None
    # 强度在**构造时**算好并缓存：调用方（CLI 的 D11 分支、将来的任何新入口）只需要
    # 读 `weak`，不必自己记得调 `assess_token` —— 「有没有做强度校验」因此不依赖
    # token 是**从哪来的**（手输 / 环境变量 / 自动生成 / 将来可能有的配置文件）。
    # 这是有意的：靠「来源」决定要不要校验，等于把安全性押在「调用方记得调」上，
    # 新增一个 token 来源就会静默绕过 —— 那种漏法在 code review 里极难发现。
    _weak: bool = field(default=False, init=False, repr=False, compare=False)
    _weak_reason: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        strong, why = assess_token(self.token or "")
        self._weak = self.enabled and not strong
        self._weak_reason = why

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @property
    def weak(self) -> bool:
        """**生效中的**这枚 token 是不是弱 token。未启用鉴权时为 `False`（没东西可弱）。"""
        return self._weak

    @property
    def weak_reason(self) -> str:
        """`weak` 为 True 时的原因，可直接打印给用户。"""
        return self._weak_reason

    def accepts(self, authorization: str | None) -> bool:
        """校验 `Authorization` 头（大小写 + Bearer 前缀容错）。未启用时恒 True。"""
        if not self.enabled:
            return True
        return safe_token_equal(_extract_bearer(authorization), self.token)


def _extract_bearer(header: str | None) -> str:
    """从 `Authorization: Bearer xxx` 里取出 xxx。没有前缀也接受（少一种用户踩坑方式）。"""
    raw = (header or "").strip()
    if not raw:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == _HEADER_SCHEME.lower():
        return parts[1].strip()
    return raw


# ---------------------------------------------------------------- 回环与 Host 白名单
def is_loopback(host: str) -> bool:
    """host 是不是回环地址（127.0.0.0/8 · ::1 · localhost）。空串按「是」处理。"""
    host = (host or "").strip().strip("[]")
    if not host or host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_list(value: object) -> tuple[str, ...]:
    """把「绑定地址」的各种写法统一成去重后的元组。

    三种输入都收，因为三个来源的形状本来就不一样：

    - `None` → 空元组（"没给"）
    - `"127.0.0.1"` / `"a.a.a.a,b.b.b.b"` → 逗号切分
    - `("a", "b")` → 逐个再按逗号切一遍（元素是逗号串时也成立）

    去重**保持原顺序**：第一个元素要当 `Options.host` 的兼容别名用，
    排序会让它指向一个用户没写的地址。
    """
    if value is None:
        return ()
    items: list[str] = []
    for raw in ([value] if isinstance(value, str) else list(value)):
        for piece in str(raw).split(","):
            host = piece.strip().strip("[]")
            if host and host not in items:
                items.append(host)
    return tuple(items)


def has_non_loopback(hosts: object) -> bool:
    """绑定集合里**任一**地址非回环 → True。

    🔴 多地址绑定后这里必须取 **any**，不能取 first、也不能取 all：

    - 取 first：`--host 127.0.0.1,192.168.1.5` 会被判成纯回环
      → 不强制 token / 不禁明文 / 不禁弱 token，等于在局域网上开一个裸服务。
      这是本次改动里唯一能造成真实事故的地方。
    - 取 all：`--host 127.0.0.1,192.168.1.5` 会被判成"有回环就不是非回环"，
      同样漏。any 是唯一自洽的语义：暴露面取**并集**，不取交集。

    空集合按回环处理（没有绑定 = 没有暴露），与 `Options` 默认值一致。
    """
    return any(not is_loopback(h) for h in host_list(hosts))


def local_ip_candidates() -> list[str]:
    """本机在**已启用**网卡上的 IPv4（给绑通配地址时补 Host 白名单用）。

    2026-09-17 重写：旧实现是「UDP connect 一个外部地址 + `gethostname` 解析」，
    两条都有实测出来的毛病 ——

    1. **UDP connect 拿到的是"出网"那张网卡，不是"别人能连进来"那张。**
       实测本机（有 VPN 隧道时）它返回的是 某条 VPN 隧道网卡上的 `10.8.0.x/30`，
       一条点对点隧道地址；真正能分享出去的 `192.168.1.x/24`（WLAN）被排在第二位。
       「出网」和「可被访问」是两件事，旧实现把它们当成一件。
    2. `gethostname` 的解析结果取决于**当时的 DNS / NetBIOS 配置**，
       而且会把未启用网卡上的地址也带回来（实测本机 12 个 IPv4 里 6 个在未启用网卡上）。

    现在统一走 `network_addresses()`：只看 up 的网卡，排除回环与链路本地。
    只收 IPv4 —— 与旧行为一致（`HostPolicy` 的白名单比对是纯字符串，
    多塞 IPv6 只会让 `--allow-host` 的排查更吵，没有收益）。
    """
    # 刻意**不**排除虚拟网卡与点对点地址：白名单宁可宽一点也不该漏，
    # 漏掉的后果是用户从自己的 WSL / 虚拟机里访问拿到 400，
    # 而多放行一个本机地址并不扩大攻击面（能连到它的前提是能路由到这台机器）。
    return [a.ip for a in network_addresses() if not _is_ipv6(a.ip)]


# ---------------------------------------------------------------- 本机地址枚举
# 虚拟网卡名启发式。**只用于横幅提示，绝不参与任何放行判定** ——
# 网卡名是驱动与用户随便起的，拿它做安全决策等于把判定权交给一个字符串。
_VIRTUAL_IFACE_HINTS = (
    "vmware", "virtualbox", "vbox", "hyper-v", "vethernet", "wsl", "docker",
    "container", "veth", "br-", "tun", "tap", "tailscale", "zerotier", "wireguard",
    "pseudo-interface", "loopback",
)

# 点对点链路的前缀下限：IPv4 /30 只有 2 个可用地址，IPv6 /126 同理。
# 这类接口（VPN 隧道 / 串行链路）的地址拿给别人用是没意义的。
_P2P_PREFIX_V4 = 30
_P2P_PREFIX_V6 = 126


@dataclass(frozen=True)
class AddressInfo:
    """一张网卡上的一个地址。

    `kind` 只看**地址本身的属性**，不做任何 DNS 解析或反查（理由同
    `_normalize_host`：一旦判定依赖一次解析，就等于把结果交给当时的 DNS）：

    | kind | 判据 | 该不该分享给别人 |
    |---|---|---|
    | `loopback` | `is_loopback` | 否，只有本机 |
    | `link_local` | `is_link_local` | 否，169.254.x / fe80:: 不可路由 |
    | `point_to_point` | 前缀 >= /30（v4）/ /126（v6） | 否，隧道对端而已 |
    | `network` | `is_private` | **是**，同网段设备可用 |
    | `public` | 其余 | 谨慎，它是全球可路由的 |
    """

    ip: str
    iface: str = ""
    prefixlen: int = 0
    is_up: bool = True
    kind: str = "network"
    virtual: bool = False

    @property
    def shareable(self) -> bool:
        """适合发给同网段其它设备：内网或公网地址、非虚拟网卡、非点对点。"""
        return self.kind in {"network", "public"} and not self.virtual


def _is_ipv6(ip: str) -> bool:
    return ":" in ip


def _prefixlen(netmask: str, ip: str) -> int:
    """掩码 → 前缀长度。

    拿不到掩码时的兜底**不是** /32 与 /128 对称处理，实测出来的差别：

    - IPv4 无掩码 → /32（"只有这一个地址"，按点对点算，保守）
    - IPv6 无掩码 → **/64**。Windows 上 `psutil` 对 IPv6 一律报 `netmask=None`
      （2026-09-17 实测，全部 13 条 IPv6 记录都是 None）。若按 /128 兜底，
      本机的 `3ffe:...` 全球单播地址会被 `_classify` 判成 `point_to_point`
      —— 那是一个**可路由**的公网地址，判成隧道是严重误判。
      /64 是 SLAAC 与绝大多数真实 IPv6 网络的前缀长度。
    """
    if netmask:
        try:
            return ipaddress.ip_network(f"{ip}/{netmask}", strict=False).prefixlen
        except ValueError:
            pass
    return 64 if _is_ipv6(ip) else 32


def _classify(ip: str, prefixlen: int) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "network"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    limit = _P2P_PREFIX_V6 if addr.version == 6 else _P2P_PREFIX_V4
    if prefixlen >= limit:
        return "point_to_point"
    return "network" if addr.is_private else "public"


def _is_virtual(iface: str) -> bool:
    name = (iface or "").lower()
    return any(hint in name for hint in _VIRTUAL_IFACE_HINTS)


def _addr_sort_key(info: "AddressInfo") -> tuple:
    """可分享的排前面 → 非虚拟优先 → **IPv4 优先** → 地址序。

    IPv4 优先是实测逼出来的：只按地址字节排的话，本机的
    `3ffe:...`（IPv6 公网）会排在 `192.168.1.5`（IPv4 内网）前面，
    于是「挑一个地址打印」会挑中那个 IPv6 —— 而绝大多数用户要的是 v4。
    """
    try:
        parsed = ipaddress.ip_address(info.ip)
        version, packed = parsed.version, parsed.packed
    except ValueError:
        version, packed = 0, info.ip.encode("utf-8", "ignore")
    return (not info.shareable, info.virtual, version, packed)


def network_addresses(
    *,
    include_down: bool = False,
    include_loopback: bool = False,
    include_link_local: bool = False,
    ipv6: bool = True,
) -> list[AddressInfo]:
    """枚举本机网卡上的地址。**不发任何包**，只读内核的网卡表。

    用 `psutil` 而不是 `getaddrinfo` / UDP connect：前者能同时拿到
    **网卡名、掩码、启用状态**这三样 —— 横幅要标注"这是哪张网卡"、
    判断"这是不是点对点隧道"、以及"这块网卡到底启用没有"，缺一不可。
    `psutil` 是本项目核心依赖（见 requirements.txt / pyproject），不是新增负担。

    失败一律返回空表：横幅少几行可以接受，**启动失败不行**。
    """
    try:
        import psutil
    except ModuleNotFoundError:
        return []
    try:
        raw = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except OSError:
        return []

    out: list[AddressInfo] = []
    for iface, addrs in raw.items():
        stat = stats.get(iface)
        up = bool(stat.isup) if stat is not None else False
        for item in addrs:
            if item.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip = item.address or ""
            if not ipv6 and _is_ipv6(ip):
                continue
            prefixlen = _prefixlen(item.netmask or "", ip)
            kind = _classify(ip, prefixlen)
            if not include_down and not up:
                continue
            if kind == "loopback" and not include_loopback:
                continue
            if kind == "link_local" and not include_link_local:
                continue
            out.append(AddressInfo(
                ip=ip, iface=iface, prefixlen=prefixlen, is_up=up,
                kind=kind, virtual=_is_virtual(iface),
            ))
    out.sort(key=_addr_sort_key)
    return out


@dataclass
class HostPolicy:
    """Host 头白名单。

    DNS rebinding 的唯一实用防线：浏览器会按 DNS 解析结果把请求发到本机，
    但 **Host 头仍然是攻击者那个域名** —— 除非他猜到了这里配置的 host。

    ## 为什么**名字**只能显式声明（`--allow-host`）

    同一台机器可以被叫很多名字：短主机名、FQDN、`xxx.local`、`hosts` 文件里的别名、
    DNS 里的 CNAME。服务端若自动把它们推导进来，就等于把「谁被允许」交给
    **当时的 DNS 配置**——包括 DHCP 下发的搜索后缀。在不可信的局域网里，
    那个后缀是谁给的，就等于信任谁。

    所以这里只放行三类东西：

    1. 常量 `localhost` / `127.0.0.1` / `::1` —— 不然自己访问自己都 400
    2. 显式绑定的那个地址
    3. `--allow-host` 逐个声明的名字

    ⚠️ **已知的既有偏差**（不是本次改动引入的）：绑通配地址时会把
    `local_ip_candidates()` 返回的本机 IP **全部**放行，虚拟网卡的也在里面。
    所以第 2 条实际上比字面条件宽。改成「通配也要求显式声明」会让所有
    `--host 0.0.0.0` 的用户一启动就 400，属于破坏性变更，暂不做。
    """

    allowed: set[str] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        bind_host: object = None,
        extra: Iterable[str] = (),
        *,
        include_lan: bool = True,
    ) -> "HostPolicy":
        """按绑定地址推导允许清单。

        `bind_host` 可以是单个地址、逗号串、或地址序列 —— 多地址绑定后
        **每一个**都要进白名单：少加一个，用户从那个地址访问就是 400，
        而报错还是「Host 头不在白名单内」，排查方向完全指错。

        - `127.0.0.1` / `localhost` / `::1` 恒允许（不然自己访问自己都 400）
        - 显式绑定了具体地址 → 逐个进白名单
        - **任一个**是通配地址（`0.0.0.0` / `::`）→ 无从推导，补上本机已启用网卡的 IP，
          否则用户从另一台机器按横幅上的 IP 访问会被 400
        """
        allowed = {"localhost", "127.0.0.1", "::1"}
        wild = False
        for host in host_list(bind_host):
            norm = host.lower()
            if norm in {"0.0.0.0", "::", "*"}:
                wild = True
            else:
                allowed.add(norm.strip("[]"))
        if wild and include_lan:
            allowed.update(local_ip_candidates())
        allowed.update(_normalize_host(h) for h in extra if h)
        return cls(allowed=allowed)

    def allows(self, host_header: str | None) -> bool:
        """Host 头（可能带端口）是否在白名单里。

        比对前先归一边（`_normalize_host`）：去掉方括号、统一大小写、剥掉 FQDN 的结尾点，
        这样 `--allow-host Foo.Example` 能配上浏览器发出的 `foo.example.`。
        """
        raw = (host_header or "").strip()
        if not raw:
            return False  # HTTP/1.0 且无 Host：这里没有虚拟主机需求，直接不放行
        host = _normalize_host(strip_port(raw))
        return host in {_normalize_host(a) for a in self.allowed}


def _normalize_host(host: str) -> str:
    """归一化一个主机名：去掉方括号 → 转小写 → 去掉 FQDN 的结尾点。

    刻意**只**做纯字符串变换 —— 不解析 DNS、不做反查。
    一旦「白名单放不放行」取决于一次解析的结果，这道防线就变成了
    「取决于当时 DNS 说什么」，而 DNS 正是 rebinding 攻击里攻击者唯一控制得了的东西。
    """
    return host.strip().strip("[]").lower().rstrip(".")


def parse_hosts(raw: str | None) -> tuple[str, ...]:
    """把环境变量里逗号分隔的 host 串切成元组（`a.com,b.com` → `("a.com","b.com")`）。

    放在**这里**而不是两个 CLI 里各写一份：名单的处理归名单模块，
    `nputweb` 与 `nputserve` 共用同一份切分规则，不会漂移。
    分隔符只认逗号 —— 主机名里不会出现逗号，含糊的中间状态不值得猜。
    """
    return tuple(h.strip() for h in (raw or "").split(",") if h.strip())


def strip_port(host: str) -> str:
    """去掉端口。IPv6 的 `[::1]:8765` 要留方括号里的地址，`a:b` 不是这种情况别误伤。

    对外可见（`web.app` 要在 400 里复述被拒的那个名字），所以不带下划线前缀。
    """
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1:end]
        return host
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_allows(host_header: str | None, bind_host: object = None, extra: Iterable[str] = ()) -> bool:
    """一次性判定（不想自己构造 `HostPolicy` 时用）。"""
    return HostPolicy.build(bind_host, extra).allows(host_header)
