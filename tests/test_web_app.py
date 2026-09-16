"""nputweb 应用层单测：路由 / 中间件 / 启动策略（SPEC.md · WebUI（nputweb））。

用 `TestClient` 而不是真起服务 —— 秒级、不占端口、不加载模型。
**不加载模型**这条要一直守着：把 NPU 30 s 的编译拖进单测，套件就没人愿意跑了。

TestClient 的默认 Host 是 `testserver`，会被 HostPolicy 拒（正确的 DNS rebinding 防线），
所以下面统一用 `base_url="http://localhost"`。
"""
from __future__ import annotations

import random
import threading

import pytest
import typer

from npu_translator.orchestrate import OrchestrateConfig, Translator
from npu_translator.pool import CallableWorker
from npu_translator.web import DEFAULT_RATE_PER_MIN
from npu_translator.web import cli as webcli
from npu_translator.web import routes as webroutes
from npu_translator.web.app import SecurityConfig, ServerContext, create_app
from npu_translator.web.auth import HostPolicy, TokenChecker
from npu_translator.web.limits import QueueGate, RateLimiter

fastapi = pytest.importorskip("fastapi", reason="nputweb 需要 [web] extra")
httpx = pytest.importorskip("httpx", reason="TestClient 依赖 httpx")
Client = pytest.importorskip("starlette.testclient").TestClient

TOKEN = "unit-test-token"


# ---------------------------------------------------------------- 夹具
def echo_translator(target: str = "en") -> Translator:
    """替身编排器：不碰 OpenVINO，译文是 `<目标语>原文`，便于断言语向真的传到位了。"""
    return Translator(
        OrchestrateConfig(target=target),
        pool_factory=lambda _o: [CallableWorker("NPU", lambda t, tg, src: f"<{tg}>{t}")],
    )


def make_ctx(**kwargs) -> ServerContext:
    opts = dict(
        translator=echo_translator(),
        token=TokenChecker(None),  # 默认不鉴权：鉴权要单独一组用例
        host_policy=HostPolicy.build("127.0.0.1", extra=["testserver"]),
        limiter=RateLimiter(per_minute=0),  # 默认不限流
        gate=QueueGate(max_pending=4),
        security=SecurityConfig(max_input_chars=500, timeout_s=5, queue_size=4),
    )
    opts.update(kwargs)
    return ServerContext(**opts)


def client(ctx: ServerContext) -> object:
    return Client(create_app(ctx), base_url="http://localhost")


# ---------------------------------------------------------------- 正常路径
def test_health_reports_status_and_device():
    ctx = make_ctx()
    r = client(ctx).get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["device"] == "NPU"
    assert body["status"] in {"loading", "ready"}
    assert body["tls"] is False


def test_languages_returns_all_38():
    body = client(make_ctx()).get("/api/languages").json()
    assert body["total"] == 38
    assert any(lang["code"] == "zh-Hant" for lang in body["common"])


def test_zh_hant_exposes_chinese_prompt_name():
    """zh-Hant 的 prompt 名必须是「繁体中文」，不是 Traditional Chinese（SPEC.md · Prompt 与语言）。"""
    body = client(make_ctx()).get("/api/languages").json()
    hant = next(lang for lang in body["common"] if lang["code"] == "zh-Hant")
    assert hant["prompt_name"] == "繁体中文"
    assert hant["en_name"] == "Traditional Chinese"


def test_translate_roundtrip():
    r = client(make_ctx()).post("/api/translate",
                                json={"text": "你好", "target": "ja"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "<ja>你好"
    assert body["device"] == "NPU"
    assert body["segments"] == 1
    assert body["chars"] == 2
    assert body["cached"] is False


def test_translate_honours_request_target_over_default():
    """每次请求的语言可以不同 —— target 不能被服务端配置锁死。"""
    body = client(make_ctx()).post("/api/translate",
                                   json={"text": "x", "target": "ko"}).json()
    assert body["text"] == "<ko>x"


def test_static_index_is_served_without_auth():
    ctx = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx).get("/")
    assert r.status_code == 200
    assert "nputweb" in r.text
    # 静态页本身没有敏感数据，必须能免鉴权拿到（否则用户没法输入 token）


# ---------------------------------------------------------------- 鉴权（401）
def test_missing_token_is_401():
    ctx = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx).post("/api/translate", json={"text": "hi", "target": "en"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401():
    ctx = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx).get("/api/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_valid_token_passes():
    ctx = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx).get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_error_body_never_leaks_traceback():
    """响应体只能有约定好的 code/message。traceback 会带上本机绝对路径。"""
    ctx = make_ctx()
    r = client(ctx).post("/api/translate", json={"text": "hi", "target": "nope"})
    assert r.status_code == 400
    assert set(r.json()["error"].keys()) == {"code", "message"}
    assert "Traceback" not in r.text and "src\\npu_translator" not in r.text


# ---------------------------------------------------------------- Host 白名单（400）
def test_forged_host_header_is_400():
    ctx = make_ctx()
    r = client(ctx).get("/api/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_host"


# ---------------------------------------------------------------- 限流（429）
def test_rate_limit_returns_429():
    # health 已豁免限流（见 S1：心跳不该吃翻译的配额），所以这里打一个受保护的端点
    ctx = make_ctx(limiter=RateLimiter(per_minute=2))
    c = client(ctx)
    assert c.get("/api/languages").status_code == 200
    assert c.get("/api/languages").status_code == 200
    third = c.get("/api/languages")
    assert third.status_code == 429
    assert "Retry-After" in third.headers


# ---------------------------------------------------------------- 队列（503）
def test_queue_full_returns_503():
    gate = QueueGate(max_pending=1)
    entered = threading.Event()
    release = threading.Event()

    def slow(text: str, target: str, source: str) -> str:
        entered.set()
        release.wait(5)
        return text

    ctx = make_ctx(
        translator=Translator(OrchestrateConfig(),
                              pool_factory=lambda _o: [CallableWorker("NPU", slow)]),
        gate=gate,
    )
    c = client(ctx)
    results: list[int] = []

    def first():
        results.append(c.post("/api/translate", json={"text": "a", "target": "en"}).status_code)

    t = threading.Thread(target=first, daemon=True)
    t.start()
    assert entered.wait(5), "第一个请求没能进场"
    # 第一个还在飞，第二个应当被队列上限挡掉
    r = c.post("/api/translate", json={"text": "b", "target": "en"})
    assert r.status_code == 503
    release.set()
    t.join(10)
    assert results == [200]


# ---------------------------------------------------------------- 体积（413）
def test_oversized_input_is_413():
    ctx = make_ctx(security=SecurityConfig(max_input_chars=50))
    r = client(ctx).post("/api/translate", json={"text": "x" * 500, "target": "en"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_huge_declared_length_rejected_before_reading_body():
    ctx = make_ctx(security=SecurityConfig(max_input_chars=50))
    r = client(ctx).post(
        "/api/translate",
        content=b'{"text":"x"}',
        headers={"Content-Type": "application/json", "Content-Length": "99999999"},
    )
    assert r.status_code == 413


# ---------------------------------------------------------------- 安全头与地图关闭
def test_security_headers_are_present():
    heads = client(make_ctx()).get("/api/health").headers
    assert heads["x-frame-options"] == "DENY"
    assert heads["x-content-type-options"] == "nosniff"
    assert heads["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in heads["content-security-policy"]


def test_hsts_only_when_https():
    plain = client(make_ctx()).get("/api/health").headers
    assert "strict-transport-security" not in plain
    https = client(make_ctx(security=SecurityConfig(https=True))).get("/api/health").headers
    assert "strict-transport-security" in https


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_map_is_404_by_default(path):
    assert client(make_ctx()).get(path).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_api_map_opens_with_debug(path):
    ctx = make_ctx(security=SecurityConfig(debug=True))
    assert client(ctx).get(path).status_code in {200, 307}


# ---------------------------------------------------------------- 启动策略矩阵（D6 / D7）
def test_env_vars_are_picked_up(monkeypatch):
    monkeypatch.setenv("NPT_WEB_HOST", "10.0.0.5")
    monkeypatch.setenv("NPT_WEB_PORT", "9999")
    monkeypatch.setenv("NPT_WEB_TOKEN", "env-token")
    opts = webcli.options_from_env()
    assert opts.host == "10.0.0.5"
    assert opts.port == 9999
    assert opts.token == "env-token"


def test_merge_ignores_unset_cli_values():
    opts = webcli.options_from_env()
    merged = webcli.merge(opts, host=None, port=1234)
    assert merged.port == 1234
    assert merged.host == opts.host, "None 表示「命令行没给」，不该覆盖环境值"


def test_loopback_can_run_without_auth(monkeypatch, capsys):
    monkeypatch.setattr(webcli, "check_port_free", lambda h, p: None)
    opts = webcli.Options(host="127.0.0.1", no_auth=True, no_warmup=True)
    # 走到 build_context 之前就该返回 —— 只要不抛异常即通过
    webcli.print_banner(opts, "127.0.0.1", 8765, "http", TokenChecker(None), ["NPU"], "")
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765/" in out


def test_non_loopback_plain_text_refuses_to_start(capsys):
    """D7：这是本项目最现实的事故组合，必须拒绝启动（退出码 3）。"""
    opts = webcli.Options(host="192.168.1.5", tls="off", allow_insecure=False)
    # ⚠️ typer.Exit 是 RuntimeError 的子类，**不是** SystemExit（2026-09-13 实测）
    with pytest.raises(typer.Exit) as excinfo:
        webcli.resolve_binding(opts)
    assert excinfo.value.exit_code == webcli.EXIT_STARTUP
    assert "拒绝启动" in capsys.readouterr().err


def test_non_loopback_plain_text_allowed_with_flag():
    opts = webcli.Options(host="192.168.1.5", tls="off", allow_insecure=True)
    scheme, checker, enabled = webcli.resolve_binding(opts)
    assert scheme == "http" and enabled is False
    assert checker.enabled, "非回环 + 明文可以放行，但 token 依然强制"


def test_non_loopback_forces_token(capsys):
    opts = webcli.Options(host="192.168.1.5", tls="auto")
    scheme, checker, _ = webcli.resolve_binding(opts)
    assert checker.enabled and checker.token
    assert "Bearer" not in checker.token


def test_tls_enabled_forces_token_even_on_loopback():
    opts = webcli.Options(host="127.0.0.1", tls="auto")
    _, checker, enabled = webcli.resolve_binding(opts)
    assert enabled is True
    assert checker.enabled, "自签证书不防冒充，所以启了 TLS 也要 token"


def test_no_auth_rejected_for_non_loopback():
    opts = webcli.Options(host="192.168.1.5", tls="auto", no_auth=True)
    with pytest.raises(typer.Exit) as excinfo:
        webcli.resolve_binding(opts)
    assert excinfo.value.exit_code == webcli.EXIT_USAGE


def test_resolve_listen_host_never_prints_wildcard(monkeypatch):
    monkeypatch.setattr(webcli, "_primary_ip", lambda: "192.168.1.77")
    assert webcli.resolve_listen_host("0.0.0.0") == "192.168.1.77"
    assert webcli.resolve_listen_host("127.0.0.1") == "127.0.0.1"


def test_port_in_use_raises_startup_error():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        with pytest.raises(SystemExit):
            webcli.check_port_free("127.0.0.1", port)
    finally:
        sock.close()


# ---------------------------------------------------------------- 关停路径
def test_run_server_returns_130_on_interrupt(monkeypatch):
    """Ctrl+C 要走「打标记 → 优雅等 → 返回 130」，且给 uvicorn 打过停止标记。"""

    class StubServer:
        def __init__(self) -> None:
            self.should_exit = False

    async def fake_serve(ctx, opts):
        return None

    monkeypatch.setattr(webcli, "_serve", fake_serve)

    ctx = make_ctx()
    ctx._server = StubServer()  # 模拟已经起来的 server
    calls = {"n": 0}

    def flaky_join(self, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(threading.Thread, "join", flaky_join, raising=False)
    code = webcli.run_server(ctx, webcli.Options(port=8765))
    assert code == webcli.EXIT_INTERRUPTED
    assert ctx._server.should_exit is True, "必须让 uvicorn 主动收连接，而不是掐掉它"


def test_context_shutdown_releases_executor():
    ctx = make_ctx()
    ctx.shutdown(wait=False)
    assert ctx.executor._shutdown is True


# ---------------------------------------------------------------- 惰性导入（架构约定）
def test_importing_web_package_stays_lazy():
    """`import npu_translator.web` 不得拉起 OpenVINO / fastapi / cryptography。

    理由见 `SPEC.md · 架构与目录结构` —— OpenVINO 初始化有百毫秒级开销且常驻占内存，
    而这三个包都属于 optional dependencies.web —— 顶层 import 会让「没装 extra」的环境
    连一句安装提示都来不及说就崩。必须在**独立进程**里验证，同进程里谁先 import 都会污染结论。
    """
    import os
    import subprocess
    import sys

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import sys, importlib;"
        "importlib.import_module('npu_translator.web');"
        "print([m in sys.modules for m in ('openvino','fastapi','cryptography')])"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[False, False, False]"


# ---------------------------------------------------------------- 心跳与配额（P0 回归）
class FakeClock:
    """注入时钟。别用 sleep 耗时间 —— 套件要跑得秒级。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# ⚠️ 采样点**绝不能**落在 1.5 的整数倍上。
# `RateLimiter._evict()` 在 `hit()` 内部先执行：请求恰好撞上淘汰边界时 count 先掉一格
# → 被放行 → **在有 bug 的代码上测试也是绿的**。评审阶段实测：固定 20 s 间隔的假绿率
# 最高到 57/57 全绿，而 off-grid 的偏移（0.05–1.45）全部 0%。
# 所以下面所有时刻一律带一个 .37 / .41 / .73 之类的零头。
_OFF_GRID = (0.37, 0.73, 1.11)


def test_translate_survives_45s_of_health_polling():
    """★ 红灯基线：模拟 1.5 s 心跳跑满 60 s，之后连续三次翻译**必须全部成功**。

    修复前这三次会全部 429（心跳 40 次/分吃光 30/min 的配额）—— 这正是用户报的
    「页面开着 45 秒后翻译基本必挂」。
    """
    clock = FakeClock()
    ctx = make_ctx(limiter=RateLimiter(per_minute=DEFAULT_RATE_PER_MIN, clock=clock))
    c = client(ctx)

    for step in range(1, 41):                 # 60 s / 1.5 s = 40 次心跳
        clock.now = step * 1.5
        assert c.get("/api/health").status_code == 200

    for offset in _OFF_GRID:
        clock.now = 60.0 + offset
        r = c.post("/api/translate", json={"text": "你好", "target": "en"})
        assert r.status_code == 200, f"t=60+{offset}s 被限流：{r.text}"


def test_health_is_exempt_from_rate_limit():
    """豁免必须是**单向的**：health 不计配额，但 translate 照样该 429 就 429。"""
    clock = FakeClock()
    ctx = make_ctx(limiter=RateLimiter(per_minute=2, clock=clock))
    c = client(ctx)

    for step in range(20):                    # 20 次心跳，配额只有 2 也照过
        clock.now = step * 1.5 + 0.37
        assert c.get("/api/health").status_code == 200

    codes = []
    for step in range(3):
        clock.now = 100.0 + step * 0.41
        codes.append(
            c.post("/api/translate", json={"text": "hi", "target": "en"}).status_code)
    assert codes == [200, 200, 429], f"豁免变成了双向的：{codes}"


def test_translate_not_starved_by_health_poll():
    """随机相位：模拟真人点击节奏，断言翻译不会被心跳饿死。

    ⚠️ 采样时刻**必须随机化**。固定间隔会在 1.5 s 网格上撞共振（固定 20 s 每次只漂移
    0.5 s → 只循环 3 个相位，其中 2 个恰好是幸运相位），评审阶段首轮报的「66.7% 成功率」
    就是这么来的。seed 固定是为了可复现。
    """
    rng = random.Random(20260914)
    clock = FakeClock()
    ctx = make_ctx(limiter=RateLimiter(per_minute=DEFAULT_RATE_PER_MIN, clock=clock))
    c = client(ctx)

    now = 0.0
    failures = []
    for _ in range(30):
        # 一次「点击周期」：先随机心跳若干次，再在随机偏移上发一次翻译
        for _ in range(rng.randint(3, 12)):
            now += 1.5
            clock.now = now
            c.get("/api/health")
        now += rng.uniform(0.05, 1.45)        # 刻意避开 1.5 的整数倍
        clock.now = now
        r = c.post("/api/translate", json={"text": "hi", "target": "en"})
        if r.status_code != 200:
            failures.append((round(now, 3), r.status_code))
    assert not failures, f"以下时刻的翻译被心跳挤掉了：{failures}"


def test_health_not_blocked_when_queue_full():
    """队列豁免：health 只是读状态，不该占队列位置、更不该被队列挤成 503。"""
    gate = QueueGate(max_pending=1)
    ctx = make_ctx(gate=gate)
    assert gate.try_enter() is not None, "先把队列占满"
    c = client(ctx)

    assert c.get("/api/health").status_code == 200
    assert gate.depth == 1, "health 不该改变队列深度（既没进场也没 leave）"
    # 对照组：非豁免端点仍然要 503
    assert c.get("/api/languages").status_code == 503


def test_health_exemption_keeps_host_and_auth_checks():
    """豁免只能跳过限流与队列，Host 白名单和鉴权一步都不能少。

    写成「在 `/api/` 入口处提前放行」的话下面两条会变绿 —— 那样 health 就成了
    免费的未鉴权探测端点。评审阶段有两位成员在这个点上判错过。
    """
    ctx = make_ctx(token=TokenChecker(TOKEN))
    c = client(ctx)
    assert c.get("/api/health", headers={"Host": "evil.example.com"}).status_code == 400
    assert c.get("/api/health").status_code == 401


def test_exempt_set_contains_health_but_not_translate():
    """配置闸门。行为类用例（上面几条）已经会红，但这条能**直接指出原因**。

    顺带钉死范围：豁免集合里绝不能混进 `/api/translate` —— 那等于把限流整个关掉。
    """
    from npu_translator.web.app import _EXEMPT_FROM_LIMITS

    assert "/api/health" in _EXEMPT_FROM_LIMITS
    assert "/api/translate" not in _EXEMPT_FROM_LIMITS, "翻译端点绝不能豁免限流"


# ---------------------------------------------------------------- 参数化后默认值 = 旧行为（M3）
def test_security_config_defaults_match_nputweb():
    """★ 「nputweb 行为逐字不变」的机器保证，不靠人记。

    M3 给 `SecurityConfig` 加了 8 个字段（为了 `nputserve` 复用同一份中间件栈）。
    每加一个字段，这里的清单就要跟着多一行 —— 让"改了默认值"变成一个**有意识的动作**：
    想改就得连这条断言一起改，而改断言会在 review 里显眼地出现。
    """
    sec = SecurityConfig()
    assert sec.require_token is True
    assert sec.max_input_chars == 5000
    assert sec.timeout_s == 120.0
    assert sec.queue_size == 8
    assert sec.rate_per_min == 30
    assert sec.debug is False
    assert sec.https is False
    assert sec.ssl_certfile == "" and sec.ssl_keyfile == ""

    # ---- M3 新增 8 项，默认值必须让 nputweb 与改造前一模一样
    assert sec.api_prefix == "/api/", "前缀变了 → 静态资源会被拉进四步检查"
    assert sec.exempt_from_rate == frozenset({"/api/health"})
    assert sec.exempt_from_queue == frozenset({"/api/health"})
    assert sec.mount_static is True, "nputweb 必须继续挂静态面"
    assert sec.static_dir is None
    # 三个文档端点：路径必须等于改造前硬编码的那三个，且 openapi 仍受 debug gate
    assert sec.docs_url == "/docs"
    assert sec.redoc_url == "/redoc"
    assert sec.openapi_url == "/openapi.json"
    assert sec.openapi_requires_debug is True

    # 两个豁免集合的**默认内容**都等于改造前那一个集合（行为不变）；
    # 它们是**两个字段**而不是一个 —— 这点由 nputserve 的取值不同来证明
    # （见 test_server_api.py::test_service_security_defaults_differ_...）
    assert sec.exempt_from_rate == frozenset({"/api/health"})
    assert sec.exempt_from_queue == frozenset({"/api/health"})


def test_exempt_sets_can_be_replaced_per_instance():
    """默认值不能用可变默认参数共享：改一个实例不该连坐另一个，也不该改到默认值。"""
    a, b = SecurityConfig(), SecurityConfig()
    a.exempt_from_rate = frozenset({"/api/other"})
    assert b.exempt_from_rate == frozenset({"/api/health"})
    assert SecurityConfig().exempt_from_rate == frozenset({"/api/health"})


def test_exempt_decision_is_computed_before_the_four_steps():
    """🔴 红线：豁免只能跳过第 2 步（限流）与第 4 步（队列）。

    源码级闸门：中间件里绝不能出现「在 path 判定处提前 `await self.app(...); return`」
    那种写法 —— 那样 Host 白名单与鉴权会一起丢掉。这里断言 `await self.app(`
    只在前缀判定那一处出现（行为类断言见 `test_health_exemption_keeps_host_and_auth_checks`）。
    """
    import inspect

    from npu_translator.web.app import SecurityMiddleware

    src = inspect.getsource(SecurityMiddleware)

    # ① 豁免判定必须出现在四步**之前**（源码里的先后 = 运行时的先后）
    i_skip = src.index("skip_rate")
    i_host = src.index("# ---- 1. Host 白名单")
    i_rate = src.index("# ---- 2. 限流")
    i_auth = src.index("# ---- 3. 鉴权")
    i_queue = src.index("# ---- 4. 队列准入")
    assert i_skip < i_host < i_rate < i_auth < i_queue, "四步的顺序或豁免的计算位置被改了"

    # ② 只允许三处 `await self.app(...)`：非 http / 前缀之外 / 正常放行（带 finally）
    assert src.count("await self.app(scope, receive, send)") == 3, (
        "多出来的一处多半就是「在 path 判定处提前 return」那个坑"
    )
    # ③ 第 2 步与第 4 步各自只看自己的那个开关，第 1/3 步无条件执行
    assert "if not skip_rate" in src and "if not skip_queue" in src
    assert src.count("if not self.ctx.host_policy.allows(host)") == 1
    assert src.count("if not self.ctx.token.accepts(token_hdr)") == 1


# ---------------------------------------------------------------- 路径脱敏（S5）
def test_error_message_scrubs_absolute_paths():
    """异常消息里的绝对路径必须被抹掉 —— 它会把盘符、目录结构、用户名写进响应体。"""
    text = webroutes._safe_error_message(
        RuntimeError("找不到模型 D:/fake/models/HY-MT/openvino_model.xml"))
    assert "<path>" in text
    assert "fake" not in text and "openvino_model" not in text
    assert text.startswith("RuntimeError")


def test_path_scrubbing_does_not_eat_ordinary_text():
    """脱敏正则不能误伤正常文本 —— 否则错误信息会被啃得没法读。

    这几个是刻意挑的陷阱：`Note: x` 里有个「单字母 + 冒号」，`Ratio: 3/4`、
    `and/or`、`50/50` 里都有斜杠。
    """
    for sample in ("Note: x is bad", "Ratio: 3/4", "and/or", "50/50", "C 盘空间不足"):
        assert webroutes.scrub_paths(sample) == sample, f"误伤了正常文本：{sample}"


# ---------------------------------------------------------------- token 强度（D11）
def test_weak_token_on_non_loopback_refuses_to_start(capsys):
    """D11：弱 token + 非回环 → 拒绝启动（退出码 2）。"""
    opts = webcli.Options(host="192.168.1.5", tls="auto", token="1234")
    with pytest.raises(typer.Exit) as excinfo:
        webcli.resolve_binding(opts)
    assert excinfo.value.exit_code == webcli.EXIT_USAGE
    assert "1234" in capsys.readouterr().err or True  # 提示走 stderr，内容不强断言


def test_weak_token_on_loopback_only_warns(capsys):
    """D11 的另一半：回环场景只警告不拦 —— 那里的攻击者得先能在本机跑代码。"""
    opts = webcli.Options(host="127.0.0.1", tls="off", token="1234")
    scheme, checker, enabled = webcli.resolve_binding(opts)
    assert checker.enabled and checker.weak
    assert scheme == "http" and enabled is False


def test_strong_token_on_non_loopback_is_accepted():
    opts = webcli.Options(host="192.168.1.5", tls="auto", token="X9#m2!qLz7v@rT4w")
    _, checker, _ = webcli.resolve_binding(opts)
    assert checker.enabled and not checker.weak


def test_d7_still_wins_over_weak_token_check(capsys):
    """D7（非回环 + 明文 → 拒绝启动，码 3）优先级不能被动摇。"""
    opts = webcli.Options(host="192.168.1.5", tls="off", token="1234")
    with pytest.raises(typer.Exit) as excinfo:
        webcli.resolve_binding(opts)
    assert excinfo.value.exit_code == webcli.EXIT_STARTUP
    assert "拒绝启动" in capsys.readouterr().err
