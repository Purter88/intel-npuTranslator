"""TLS 证书三态（SPEC.md · WebUI（nputweb））。

三态语义：

| 模式 | 行为 | 退出码（CLI 层映射） |
|---|---|---|
| `auto`（默认） | 自签证书，落 `~/.nputweb/`，**跨重启复用** | — |
| `on` | 用用户自己的证书，`--cert` + `--key` 缺一不可 | 缺一半 → 2 |
| `off` | 明文 HTTP | 非回环时由 CLI 拦（D7），本模块不管 |

两个不做会后悔的决定：

1. **自签证书必须复用**（2026-09-13）：不复用 → 每次重启都是新证书 → 浏览器每次都要重新点
   「继续访问」，用户会直接学会忽略证书警告，TLS 就白做了。所以文件名里带 SAN 指纹，
   「同一批 SAN」命中同一份证书，「换了绑定地址」才生成新的。

2. **用 `cryptography` 而不是调 `openssl` 命令**：Windows 没自带 openssl，
   依赖外部命令 = 在最常见的目标平台上直接不可用。

纯逻辑：本模块可以在无网络、无终端的环境里单测（`data_dir` 可注入临时目录）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .auth import host_list

__all__ = [
    "TlsError",
    "TlsMode",
    "TlsPlan",
    "cert_fingerprint",
    "default_dir",
    "ensure_self_signed",
    "parse_mode",
    "resolve_tls",
]

# 证书有效期（天）。上限 825：Apple 的 TLS 策略会拒收有效期更长的证书，
# 超过这个天数在 macOS / Safari 上会直接不可信，签了等于白签。
DEFAULT_DAYS = 825
_DATA_DIR_NAME = ".nputweb"

# 证书「版式」版本号，参与文件名（见 `_cert_names`）。
# v2 = 从「自签 CA 证书」改成「普通自签服务端证书」：
#   - `BasicConstraints(ca=False)`
#   - 补 KeyUsage（digital_signature + key_encipherment）
#   - 补 EKU = serverAuth
#   - 补 SubjectKeyIdentifier
# 为什么必须改：旧版签的是 **CA** 证书。用户按浏览器提示「安装此证书」时，
# 一张自签 CA 会被当信任锚收进系统信任库 —— 从此它能给**任意域名**签证书，
# 整台机器的 HTTPS 都受它支配。而它对应的私钥就躺在一个没有额外保护的目录里。
# 普通服务端证书没有 `ca=True`，浏览器/操作系统不会拿它当信任锚，
# 装进信任库也不会获得签发能力。把能力收窄到「只做这一台机器这一个端口」。
_CERT_LAYOUT = "v2"


class TlsError(ValueError):
    """证书不可用的所有情形。继承 `ValueError`，CLI 一律映射成退出码 2（参数错误）。"""


class TlsMode(str, Enum):
    AUTO = "auto"
    ON = "on"
    OFF = "off"

    def __str__(self) -> str:  # 让 f-string 与 argparse 的错误信息好看
        return self.value


def parse_mode(raw: str | TlsMode | None) -> TlsMode:
    """把字符串解析成 `TlsMode`。大小写与空白都容忍，`None` 当 `auto`。

    ⚠️ 不要用 `str.islower()` 之类的字符判断来做「是否已是枚举」—— 那是另一个项目里
    踩过的坑（SPEC.md · Prompt 与语言），这里老实用 try/except。
    """
    if isinstance(raw, TlsMode):
        return raw
    key = str(raw or TlsMode.AUTO).strip().lower()
    try:
        return TlsMode(key)
    except ValueError:
        raise TlsError(f"--tls 必须是 auto / on / off 之一，收到 {raw!r}") from None


def default_dir() -> Path:
    """证书存放目录 `~/.nputweb/`。

    这里**只有**相对家目录的路径，不含任何机器绝对路径 —— 后者一旦进日志或文档
    就违反 Git 约定（本机用户名会跟着泄出去）。
    """
    home = Path(os.path.expanduser("~"))
    return home / _DATA_DIR_NAME


@dataclass(frozen=True)
class TlsPlan:
    """解析结果。CLI 拿它去配 uvicorn，同时决定了要不要打印证书指纹。"""

    mode: TlsMode
    enabled: bool
    certfile: str = ""
    keyfile: str = ""
    source: str = ""          # self-signed | user | ""
    fingerprint: str = ""     # SHA-256，冒号分组，供用户核对防中间人

    def as_uvicorn_kwargs(self) -> dict:
        """给 `uvicorn.run()` / `Config()` 的ssl_certfile参数。明文返回空字典。"""
        if not self.enabled:
            return {}
        return {"ssl_certfile": self.certfile, "ssl_keyfile": self.keyfile}


# ---------------------------------------------------------------- SAN 与文件名
def _san_hosts(bind_host: object = None, extra: Iterable[str] = ()) -> list[str]:
    """造出证书要覆盖的名字集合（决定文件名，也决定浏览器会不会报名字不匹配）。

    多地址绑定后**每一个**绑定地址都要进 SAN：漏掉一个，从那个地址访问就是
    `ERR_CERT_COMMON_NAME_INVALID` —— 浏览器给的是一句看不懂的错，用户只会以为
    「服务挂了」，不会想到证书。（与 `--allow-host` 同一类坑：只补白名单不补 SAN，
    等于把一道看不懂的错换成另一道看不懂的错。）

    通配地址（`0.0.0.0` / `::`）不进 SAN —— 它不是可以写进证书的名字。
    返回值**排序**：`_cert_names` 用排序后的集合派生文件名，所以
    `--host a,b` 与 `--host b,a` 命中同一份证书，不会因为顺序换一张。
    """
    hosts = {"localhost", "127.0.0.1", "::1"}
    for host in host_list(bind_host):
        if host not in {"0.0.0.0", "::", "*"}:
            hosts.add(host)
    hosts.update(h for h in extra if h)
    return sorted(hosts)


def _cert_names(sans: Iterable[str]) -> tuple[str, str]:
    """按 SAN 集合派生文件名 → 同一批 SAN 永远命中同一份证书（可复用、可信任）。

    ⚠️ **改证书结构（扩展、密钥类型、签发方式）必须同时改 `_CERT_LAYOUT` 版本后缀。**
    为什么：文件名不变的话，`ensure_self_signed` 开头的 `_loadable_pem_cert()`
    会判定「旧证书还能读」直接复用 —— 代码改了，签出来的却还是老样子，
    而且这种"改了没生效"在浏览器里表现为「还是那张旧证书」，极难发现。
    版本号进文件名 = 换结构必然换新证书，旧的自然作废。
    """
    digest = hashlib.sha256("\x00".join(sorted(sans)).encode("utf-8")).hexdigest()[:12]
    return f"nputweb-{_CERT_LAYOUT}-{digest}-cert.pem", f"nputweb-{_CERT_LAYOUT}-{digest}-key.pem"


def _write_private(path: Path, data: bytes) -> None:
    """落盘私钥，尽力收紧到 600。

    ⚠️ **Windows 上 `os.chmod` 基本是空操作**（实测 mode 仍是 0o666）：
    POSIX 权限位并不参与 NTFS 的访问控制。真正的保护来自用户配置目录本身的 ACL
    （通常只有该用户和管理员可读），所以这里是"尽力"而不是"已达成"。
    没有改用 `icacls` 只是权衡：那条命令写错反而可能把用户自己锁在外面，
    而配置目录原有的 ACL 已经够用。**这一条是有意保留的已知偏差，不是疏漏。**
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 部分文件系统不支持
        pass


def _loadable_pem_cert(path: Path) -> bool:
    """证书文件是否存在且能被解析。

    存在 ≠ 可用：上次生成被打断留下的半截 PEM 会让 ssl 模块直接拒收，
    而这种错误发生在 uvicorn 启动阶段，报错信息很难懂。所以这里先探一下。
    """
    if not path.is_file():
        return False
    try:
        from cryptography import x509

        x509.load_pem_x509_certificate(path.read_bytes())
        return True
    except Exception:  # noqa: BLE001 - 任何解析失败都判定为「不可用」，重新生成
        return False


def ensure_self_signed(
    directory: str | os.PathLike[str] | None = None,
    *,
    bind_host: object = None,
    extra_hosts: Iterable[str] = (),
    days: int = DEFAULT_DAYS,
) -> tuple[str, str]:
    """保证有一份可用的自签证书，返回 `(certfile, keyfile)`。幂等。

    复用规则见本模块 docstring 第 1 条：文件名由 SAN 派生，所以
    「同样绑定到 127.0.0.1」永远复用同一份，「改成绑 192.168.x.x」才会新签一张。
    多地址同理：`a,b` 与 `b,a` 是同一份，加了第三个地址才是新的一份。
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ModuleNotFoundError as exc:  # pragma: no cover - 缺依赖时的友好提示
        raise TlsError(
            f"缺少 cryptography，无法生成自签证书。请安装：pip install -e .[web]（{exc}）"
        ) from exc

    sans = _san_hosts(bind_host, extra_hosts)
    cert_name, key_name = _cert_names(sans)
    base = Path(directory) if directory else default_dir()
    cert_path, key_path = base / cert_name, base / key_name

    if _loadable_pem_cert(cert_path) and key_path.is_file():
        return str(cert_path), str(key_path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nputweb")])

    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        # 往前挪 5 分钟：容器 / 双系统的时钟偏一点，证书就会被判「尚未生效」
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([_as_general_name(x509, n) for n in sans]),
            critical=False,
        )
        # ★ 不是 CA（旧版是 `ca=True`，见 `_CERT_LAYOUT` 的注释）。
        #   一张自签 CA 一旦被用户装进系统信任库，就能给任意域名签证书；
        #   `ca=False` 让它**没有**这个能力，装了也只是张无害的叶子证书。
        #   `path_length` 对 `ca=False` 无意义，显式写 None 表明「不是忘了填」。
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # KeyUsage 必须 critical：这是给验证方读的硬约束，不认识就别放行。
        # 只给服务端 RSA 需要的两项：
        #   digital_signature —— ECDHE 握手里签 ServerKeyExchange
        #   key_encipherment  —— RSA 套件里加密 premaster secret
        # 明确**不给** `key_cert_sign` / `crl_sign`（那是 CA 的能力），
        # `encipher_only` / `decipher_only` 只对 key_agreement 有意义，RSA 下传 None。
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        # EKU 只需 serverAuth：这张证书只用来当 TLS 服务端，别无它用。
        # critical=False 是惯例 —— 老验证方不认识 EKU 时不至于把整张证书判废。
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        # SKI：证书自检/排错时靠它定位（也方便将来接 AKI 做链构建），
        # 对自签叶子证书不是必需，但零成本。
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    base.mkdir(parents=True, exist_ok=True)
    _write_private(key_path, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)


def _as_general_name(x509_mod: object, value: str) -> object:
    """把 SAN 条目转成 `DNSName` 或 `IPAddress`。"""
    import ipaddress

    try:
        return x509_mod.IPAddress(ipaddress.ip_address(value))  # type: ignore[attr-defined]
    except ValueError:
        return x509_mod.DNSName(value)  # type: ignore[attr-defined]


def cert_fingerprint(certfile: str | os.PathLike[str]) -> str:
    """证书 SHA-256 指纹（`AB:CD:...`）。启动横幅打印它，用户首次与之后逐字比对 = 防中间人。"""
    try:
        from cryptography import x509
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise TlsError(f"缺少 cryptography，无法计算证书指纹（{exc}）") from exc

    path = Path(certfile)
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - 启动前就该告诉用户「这张证书读不了」
        raise TlsError(f"无法读取证书 {path.name}: {type(exc).__name__}: {exc}") from exc

    digest = cert.fingerprint(hashes_sha256()).hex().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def hashes_sha256() -> object:  # pragma: no cover - 极薄的包装，为的是 import 集中
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


# ---------------------------------------------------------------- 解析入口
def resolve_tls(
    mode: str | TlsMode | None = TlsMode.AUTO,
    cert: str | None = None,
    key: str | None = None,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    bind_host: object = None,
    extra_hosts: Iterable[str] = (),
) -> TlsPlan:
    """把 `--tls / --cert / --key` 解析成一个可执行的方案。**不做任何 IO 以外的事情**。

    :raises TlsError: `on` 缺一半 / 给定的证书文件不存在 / 自签所需依赖缺失
    """
    parsed = parse_mode(mode)

    if parsed is TlsMode.OFF:
        if cert or key:
            raise TlsError("--tls off 时不要同时给 --cert / --key（自相矛盾）")
        return TlsPlan(mode=parsed, enabled=False)

    if parsed is TlsMode.ON:
        missing = [n for n, v in (("--cert", cert), ("--key", key)) if not v]
        if missing:
            raise TlsError(f"--tls on 必须同时给 --cert 与 --key，缺少 {' 和 '.join(missing)}")
        assert cert and key  # 上面已校验
        for label, path in (("--cert", cert), ("--key", key)):
            if not Path(path).is_file():
                raise TlsError(f"{label} 指向的文件不存在: {path}")
        return TlsPlan(
            mode=parsed, enabled=True, certfile=cert, keyfile=key,
            source="user", fingerprint=cert_fingerprint(cert),
        )

    # AUTO：自签 + 复用
    certfile, keyfile = ensure_self_signed(
        data_dir, bind_host=bind_host, extra_hosts=extra_hosts,
    )
    return TlsPlan(
        mode=parsed, enabled=True, certfile=certfile, keyfile=keyfile,
        source="self-signed", fingerprint=cert_fingerprint(certfile),
    )
