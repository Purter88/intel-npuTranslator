"""nputweb 的纯逻辑单测：证书三态 / 鉴权 / 限流队列（SPEC.md · WebUI（nputweb））。

这几块的共同点是「不需要起服务就能验证」—— 它们也是整个 WebUI 里最不该出错的部分
（错了就是安全漏洞），所以单独一个测试文件，不和应用层混在一起。

延迟依赖：cryptography 在本机的 venv 里已装（F1），但它属于 optional dependencies。
本文件里的用例在缺 cryptography 时会 skip 而不是 fail —— 主依赖装上就该能用 nputr。
"""
from __future__ import annotations

import ipaddress
import secrets

import pytest

from npu_translator.web import auth
from npu_translator.web import limits as lim
from npu_translator.web import tls as tlsmod

pytest.importorskip("cryptography", reason="nputweb 的自签证书依赖 cryptography")


# ================================================================ TLS
def test_self_signed_is_reused_across_calls(tmp_path):
    """★ 不复用 = 浏览器每次重启都要重新信任 = 用户学会忽略警告 = TLS 白做。"""
    cert1, key1 = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    cert2, key2 = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    assert (cert1, key1) == (cert2, key2)
    assert tlsmod.cert_fingerprint(cert1) == tlsmod.cert_fingerprint(cert2)


def test_different_bind_host_gets_its_own_cert(tmp_path):
    """换绑定地址要换证书（SAN 不同），但不能因此把原来那份也弄脏。"""
    cert_local, _ = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    cert_lan, _ = tlsmod.ensure_self_signed(tmp_path, bind_host="192.168.1.5")
    assert cert_local != cert_lan
    # 两份都得还在，且各自可用
    assert tlsmod.cert_fingerprint(cert_local)
    assert tlsmod.cert_fingerprint(cert_lan)


def test_certificate_is_loadable_by_ssl(tmp_path):
    """能被 ssl 加载 = 真的能在 uvicorn 里用起来，而不只是文件存在。"""
    cert, key = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)


def test_certificate_covers_loopback_names(tmp_path):
    """SAN 必须覆盖 localhost / 127.0.0.1，否则浏览器第一眼就是证书不匹配。"""
    from cryptography import x509

    cert, _ = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    san = x509.load_pem_x509_certificate(
        open(cert, "rb").read()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    # 用 cryptography 自己的取值 API，别自己 isinstance 判断类型
    dns_names = san.get_values_for_type(x509.DNSName)
    ipaddrs = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ipaddrs
    assert "::1" in ipaddrs


def test_private_key_is_private(tmp_path):
    cert, key = tlsmod.ensure_self_signed(tmp_path)
    import os
    import stat

    mode = stat.S_IMODE(os.stat(key).st_mode)
    # Windows 上 chmod 语义有限，所以只断言权限位的「其他用户可读」被关掉
    if os.name != "nt":
        assert mode & 0o077 == 0, oct(mode)


def test_parse_mode_is_lenient_and_strict():
    assert tlsmod.parse_mode("AUTO") is tlsmod.TlsMode.AUTO
    assert tlsmod.parse_mode(" off ") is tlsmod.TlsMode.OFF
    assert tlsmod.parse_mode(None) is tlsmod.TlsMode.AUTO
    with pytest.raises(tlsmod.TlsError):
        tlsmod.parse_mode("sometimes")


def test_resolve_off_rejects_cert_args():
    with pytest.raises(tlsmod.TlsError, match="自相矛盾"):
        tlsmod.resolve_tls("off", cert="a.pem")


def test_resolve_on_requires_both_halves(tmp_path):
    # 先做一份真证书，用来验证「只给一半」是被拦下的
    cert, key = tlsmod.ensure_self_signed(tmp_path)
    with pytest.raises(tlsmod.TlsError, match="--key"):
        tlsmod.resolve_tls("on", cert=cert)
    with pytest.raises(tlsmod.TlsError, match="--cert"):
        tlsmod.resolve_tls("on", key=key)


def test_resolve_on_missing_file_is_usage_error(tmp_path):
    with pytest.raises(tlsmod.TlsError, match="不存在"):
        tlsmod.resolve_tls("on", cert=str(tmp_path / "nope.pem"), key=str(tmp_path / "nope.key"))


def test_resolve_on_with_real_pair(tmp_path):
    cert, key = tlsmod.ensure_self_signed(tmp_path)
    plan = tlsmod.resolve_tls("on", cert=cert, key=key)
    assert plan.enabled and plan.source == "user"
    assert plan.as_uvicorn_kwargs()["ssl_certfile"] == cert


def test_resolve_off_has_no_ssl_kwargs():
    plan = tlsmod.resolve_tls("off")
    assert plan.enabled is False
    assert plan.as_uvicorn_kwargs() == {}


def test_fingerprint_is_sha256_colon_form(tmp_path):
    cert, _ = tlsmod.ensure_self_signed(tmp_path)
    fp = tlsmod.cert_fingerprint(cert)
    parts = fp.split(":")
    assert len(parts) == 32, "SHA-256 应当 32 字节"
    assert all(len(p) == 2 and all(c in "0123456789ABCDEF" for c in p) for p in parts)


def test_default_dir_is_under_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("USERPROFILE", "/home/someone")
    path = tlsmod.default_dir()
    # 断言「在家目录下」而不是断言具体字符串 —— 断言具体内容会把用户名写进测试
    assert path.name == ".nputweb"
    assert str(path.parent).replace("\\", "/").endswith("someone")


# ================================================================ 鉴权
def test_generated_tokens_are_unique_and_urlsafe():
    a, b = auth.generate_token(), auth.generate_token()
    assert a != b
    assert len(a) >= 43, "32 字节经 urlsafe base64 后至少 43 字符"
    assert all(c.isalnum() or c in "-_" for c in a)


def test_constant_time_compare():
    tok = "s3cr3t"
    assert auth.safe_token_equal(tok, tok) is True
    assert auth.safe_token_equal(tok, "s3cr3t ") is False
    assert auth.safe_token_equal(None, tok) is False, "空凭据必须判不匹配"
    assert auth.safe_token_equal("", "") is False


def test_token_checker_accepts_bearer_and_bare():
    tok = secrets.token_urlsafe(16)
    checker = auth.TokenChecker(tok)
    assert checker.enabled
    assert checker.accepts(f"Bearer {tok}")
    assert checker.accepts(tok), "不带 Bearer 前缀也要认（少一种踩坑方式）"
    assert checker.accepts("bearer " + tok)
    assert not checker.accepts("Bearer wrong")
    assert not checker.accepts(None)


def test_disabled_checker_lets_everything_through():
    checker = auth.TokenChecker(None)
    assert not checker.enabled
    assert checker.accepts(None), "未启用鉴权时放行（仅回环明文允许这样配）"


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("", True),
     ("192.168.1.5", False), ("example.com", False)],
)
def test_is_loopback(host, expected):
    assert auth.is_loopback(host) is expected


def test_host_policy_rejects_unknown_host():
    """DNS rebinding 的核心防线：攻击者域名的 Host 头必须被拒。"""
    policy = auth.HostPolicy.build("127.0.0.1")
    assert policy.allows("127.0.0.1:8765")
    assert policy.allows("localhost:8765")
    assert not policy.allows("attacker.example.com")
    assert not policy.allows(None)


def test_host_policy_includes_bind_host_and_ipv6_brackets():
    policy = auth.HostPolicy.build("192.168.1.5")
    assert policy.allows("192.168.1.5:8765")
    assert policy.allows("[::1]:8765")


def test_host_policy_allows_lan_ip_when_bound_to_wildcard(monkeypatch):
    monkeypatch.setattr(auth, "local_ip_candidates", lambda: ["192.168.1.77"])
    policy = auth.HostPolicy.build("0.0.0.0")
    # 绑通配时无从推导，必须补上局域网 IP，否则横幅上那个地址连过去就是 400
    assert policy.allows("192.168.1.77:8765")


def test_local_ip_candidates_excludes_loopback():
    for ip in auth.local_ip_candidates():
        assert not ipaddress.ip_address(ip).is_loopback


# ================================================================ 限流与队列
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rater_limiter_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    rl = lim.RateLimiter(per_minute=3, window_s=60, clock=clock)
    for _ in range(3):
        assert rl.hit("1.2.3.4").allowed
    decision = rl.hit("1.2.3.4")
    assert not decision.allowed
    assert decision.retry_after > 0


def test_rater_limiter_slides_after_window():
    """滑动窗口（不是固定计数）：窗口过后名额恢复。"""
    clock = FakeClock()
    rl = lim.RateLimiter(per_minute=2, window_s=60, clock=clock)
    rl.hit("k")
    rl.hit("k")
    assert not rl.hit("k").allowed
    clock.now = 61
    assert rl.hit("k").allowed


def test_rater_limiter_is_per_key():
    clock = FakeClock()
    rl = lim.RateLimiter(per_minute=1, clock=clock)
    assert rl.hit("a").allowed
    assert not rl.hit("a").allowed
    assert rl.hit("b").allowed, "一个 IP 触顶不该连坐其他来源"


def test_rater_limiter_zero_means_unlimited():
    rl = lim.RateLimiter(per_minute=0)
    for _ in range(500):
        assert rl.hit("x").allowed


def test_check_does_not_consume_quota():
    clock = FakeClock()
    rl = lim.RateLimiter(per_minute=1, clock=clock)
    assert rl.check("z").allowed
    assert rl.check("z").allowed
    assert rl.hit("z").allowed
    assert not rl.hit("z").allowed


def test_queue_gate_admission_and_depth():
    gate = lim.QueueGate(max_pending=2)
    assert gate.try_enter() == 1, "第一个进场的应当是正在执行的那个位置"
    assert gate.try_enter() == 2
    assert gate.try_enter() is None, "超出上限必须给 None（上层映射成 503）"
    assert gate.depth == 2
    assert gate.waiting == 1
    gate.leave()
    assert gate.try_enter() == 2, "释放后应当能再进一个"


def test_queue_gate_never_goes_negative():
    gate = lim.QueueGate(max_pending=2)
    gate.leave()  # 漏 balancing 的调用不该把计数打到负数
    assert gate.depth == 0


def test_check_body_size():
    assert lim.check_body_size("1024", 2048) == 1024
    assert lim.check_body_size(None, 2048) == 0
    assert lim.check_body_size("garbage", 2048) == 0, "非法 Content-Length 按 0 处理"
    with pytest.raises(lim.TooLarge):
        lim.check_body_size("99999", 2048)


# ================================================================
# 心跳与配额的行为验证**不在这里** —— 见 `test_web_app.py` 的同名用例。
#
# 这里一度有一条「纯逻辑模拟：心跳 600 s 不吃光配额」的用例，后来删了：
# 它自己在测试里手写「health 不调 hit()」的语义，压根没经过中间件，
# 把豁免改回去（frozenset 清空）它照样是绿的 —— 红灯验证时暴露了这一点。
# 建模出来的绿灯比没有绿灯更危险，所以配额相关的断言一律走 TestClient 打真实栈。


def test_rejected_hit_does_not_extend_window():
    """被拒的那次不能把窗口往后推 —— 否则「一直打」就能把别人永久锁在外面。"""
    clock = FakeClock()
    rl = lim.RateLimiter(per_minute=1, clock=clock)
    assert rl.hit("k").allowed
    clock.now = 10
    assert not rl.hit("k").allowed
    clock.now = 20
    assert not rl.hit("k").allowed
    clock.now = 61
    assert rl.hit("k").allowed, "窗口必须从**第一次命中**算起，而不是从被拒那次"


# ================================================================ token 强度（D11）
@pytest.mark.parametrize(
    ("token", "weak"),
    [
        ("1234", True),                     # 太短
        ("1234567890123456", True),         # 长度够但全数字
        ("aaaaaaaaaaaaaaaa", True),         # 只用到 1 种字符
        ("abababababababab", True),         # 只用到 2 种字符
        ("abcdefghijklmnop", True),         # 长度够，但全小写 = 只用一类字符
        ("abcdefghijklmnop1", False),       # 小写 + 数字 = 两类
        ("X9#m2!qLz7v@rT4w", False),        # 随手敲的强 token
    ],
)
def test_assess_token_judges_strength(token, weak):
    strong, why = auth.assess_token(token)
    assert strong is (not weak), f"{token!r} 判成 strong={strong}，原因：{why}"
    if weak:
        assert why, "弱 token 必须给出能直接打印给用户看的原因"


def test_generated_tokens_are_never_weak():
    """★ 最要紧的一条：自动生成的 256 bit token **绝不能**被判弱。

    误伤的后果是默认启动路径自己把自己拦下来（`resolve_binding` 会拒绝启动）。
    """
    for _ in range(20):
        token = auth.generate_token()
        strong, why = auth.assess_token(token)
        assert strong, f"自动生成的 token 被误判为弱：{why}"
        assert auth.TokenChecker(token).weak is False


def test_weak_flag_is_source_independent():
    """D11 的加固点：强度挂在 checker 上，**与 token 从哪来无关**。

    改成挂在「用户有没有显式给」上的话，将来多一个入口（配置文件 / stdin）
    就会静默绕过 —— 那种漏法在 code review 里极难发现。
    """
    assert auth.TokenChecker(None).weak is False, "未启用鉴权时谈不上有东西可弱"
    assert auth.TokenChecker("1234").weak is True
    assert auth.TokenChecker("1234").weak_reason, "弱 token 必须带上原因"
    assert auth.TokenChecker(auth.generate_token()).weak is False


# ================================================================ 自签证书不是 CA
def test_self_signed_cert_is_not_a_ca(tmp_path):
    """★ 签成 CA 的风险远高于普通自签服务端证书。

    用户一旦把一张自签 CA 加进系统信任库，它就能给**任意域名**签证书；
    而这类证书在卸载时几乎不会有人想起要一起删掉。
    """
    from cryptography import x509

    cert, _ = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1")
    parsed = x509.load_pem_x509_certificate(open(cert, "rb").read())

    basic = parsed.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca is False

    usage = parsed.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign is False, "不能拿来签别的证书"
    assert usage.digital_signature is True

    eku = parsed.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_cert_layout_version_is_baked_into_the_filename():
    """证书文件名必须带上 `_CERT_LAYOUT` 版本后缀。

    否则 `ensure_self_signed` 开头的 `_loadable_pem_cert()` 会直接复用旧证书 ——
    代码改了、磁盘上还是旧的，表现为「改了等于没改」，而且极难查。
    """
    cert_name, key_name = tlsmod._cert_names(["localhost"])
    assert tlsmod._CERT_LAYOUT in cert_name, "证书文件名没带版本后缀"
    assert tlsmod._CERT_LAYOUT in key_name
    # 同一批 SAN 必须派生出同一个文件名，否则「复用」这条保证就没了
    assert tlsmod._cert_names(["localhost"]) == (cert_name, key_name)

# ================================================================ --allow-host（Host 白名单）
def test_parse_hosts_splits_on_comma_and_drops_empty():
    """逗号分隔 + 去空白 + 丢空段；没给 / 全是空 = 空元组（= 一个都不额外放行）。"""
    assert auth.parse_hosts("a.example,b.example") == ("a.example", "b.example")
    assert auth.parse_hosts(" a.example , , b.example ") == ("a.example", "b.example")
    assert auth.parse_hosts(None) == ()
    assert auth.parse_hosts("") == ()
    assert auth.parse_hosts(" , ") == ()


def test_allow_host_puts_name_into_whitelist():
    """`--allow-host` 的落点：名字**只有**被显式声明才进白名单。"""
    policy = auth.HostPolicy.build("127.0.0.1", extra=["npu.example.com"])
    assert policy.allows("npu.example.com:8765")
    # 大小写与 FQDN 结尾点都要认：浏览器怎么发不由用户控制
    assert policy.allows("NPU.Example.COM.:8765")
    # 声明了这一个，不等于放开了这一类
    assert not policy.allows("other.example.com:8765")


def test_machine_short_hostname_is_not_whitelisted_by_default():
    """★ 回归锁：**不**自动推导本机主机名 —— 这是 `--allow-host` 存在的理由。

    自动推导等于把「谁可以访问」交给当时的 DNS 配置（含 DHCP 下发的搜索后缀）：
    那个后缀是谁给的，就等于信任谁。
    """
    import socket

    hostname = socket.gethostname()
    if auth.is_loopback(hostname):  # 极端环境：主机名恰好是 localhost
        pytest.skip("本机主机名是回环名，这条用例没有意义")
    for bind in ("127.0.0.1", "0.0.0.0"):
        policy = auth.HostPolicy.build(bind)
        assert not policy.allows(f"{hostname}:8765"), f"绑 {bind} 时本机短名不该自动放行"
        assert not policy.allows(f"{hostname}.local:8765")


def test_self_signed_cert_covers_extra_hosts(tmp_path):
    """第二堵墙：只补白名单不补 SAN 的话，名字对了也还是吃浏览器的证书告警。"""
    from cryptography import x509

    cert, _ = tlsmod.ensure_self_signed(tmp_path, bind_host="127.0.0.1",
                                        extra_hosts=["npu.example.com"])
    san = x509.load_pem_x509_certificate(
        open(cert, "rb").read()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    assert "npu.example.com" in san.get_values_for_type(x509.DNSName)
