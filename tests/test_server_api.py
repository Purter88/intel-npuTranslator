"""`nputserve` 的真实栈单测：用 `TestClient` 打**真中间件 + 真路由 + 真编排替身**。

## 为什么必须打真实栈

这个项目有一条血泪教训：**纯逻辑模拟的绿灯是假的**。
`test_web_security.py` 里一度有条「心跳 600 s 不吃光配额」的用例，它自己在测试里手写
「health 不调 hit()」的语义，压根没经过中间件 —— 把豁免集合清空它照样是绿的。
那条用例后来删了。所以本文件**一律**走 `TestClient`，
断言的是「从 HTTP 边界看过去」的行为，不是「我认为它应该这样」。

## 两个 TestClient 的使用要点

1. `base_url` 必须是 `http://localhost`：默认 `testserver` 会被 Host 白名单拒
   （那正是 DNS rebinding 防线在起作用）。
2. 需要并发的用例一律 `with Client(app) as c:` —— 只有作为上下文管理器使用时
   TestClient 才会复用同一个 blocking portal（= 同一个事件循环），
   否则每个请求各起一个 portal，两条请求根本不在同一个 loop 上。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from npu_translator.orchestrate import OrchestrateConfig, Translator
from npu_translator.pool import CallableWorker
from npu_translator.server import create_service_app, service_security
from npu_translator.service import ServiceConfig, ServiceRuntime
from npu_translator.web.app import SecurityConfig, ServerContext
from npu_translator.web.auth import HostPolicy, TokenChecker
from npu_translator.web.limits import QueueGate, RateLimiter

fastapi = pytest.importorskip("fastapi", reason="nputserve 需要 [server] extra")
httpx = pytest.importorskip("httpx", reason="TestClient 依赖 httpx")
Client = pytest.importorskip("starlette.testclient").TestClient

TOKEN = "unit-test-token"
BASE = "http://localhost"


# ---------------------------------------------------------------- 替身
class FakeEngine:
    """`Translator.stream()` 取 `worker.engine` 并调它的 `stream()`。"""

    def __init__(self, tokens=("Hel", "lo, ", "世界"), delay: float = 0.0):
        self._tokens = list(tokens)
        self._delay = delay
        self.started = threading.Event()

    def stream(self, text, target=None, source=None, max_new_tokens=None):
        self.started.set()
        for tok in self._tokens:
            if self._delay:
                time.sleep(self._delay)
            yield tok


def make_ctx(
    *,
    fn=None,
    engine: FakeEngine | None = None,
    max_input_chars: int = 500,
    timeout_s: float = 5.0,
    queue_size: int = 4,
    rate: int = 0,
    debug: bool = False,
    token: TokenChecker | None = None,
    config: ServiceConfig | None = None,
    gate: QueueGate | None = None,
) -> tuple[ServerContext, ServiceRuntime]:
    worker = CallableWorker("NPU", fn or (lambda t, tg, src: f"<{tg}>{t}"))
    if engine is not None:
        worker.engine = engine  # type: ignore[attr-defined]
    translator = Translator(OrchestrateConfig(target="en"),
                            pool_factory=lambda _o: [worker])
    sec = service_security(debug=debug, max_input_chars=max_input_chars,
                           timeout_s=timeout_s, queue_size=queue_size,
                           rate_per_min=rate)
    ctx = ServerContext(
        translator=translator,
        token=token if token is not None else TokenChecker(None),
        host_policy=HostPolicy.build("127.0.0.1"),
        limiter=RateLimiter(per_minute=rate),
        gate=gate or QueueGate(max_pending=queue_size),
        security=sec,
    )
    # ★ executor 与 gate 必须和 ctx 共用同一份，否则 health 的 queue 字段永远对不上
    runtime = ServiceRuntime(translator=translator, security=sec,
                             config=config or ServiceConfig(),
                             executor=ctx.executor, gate=ctx.gate)
    return ctx, runtime


def client(ctx: ServerContext, runtime: ServiceRuntime) -> object:
    return Client(create_service_app(ctx, runtime), base_url=BASE)


def auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# ================================================================ 正常路径
def test_health_exposes_active_device_and_lane():
    ctx, runtime = make_ctx()
    body = client(ctx, runtime).get("/v1/health").json()
    assert body["active_device"] == "NPU" == body["device"]
    assert body["lane"] == ""
    assert body["orphans"] == 0
    assert body["streaming"] is False
    assert body["status"] in {"loading", "ready"}
    assert body["limits"]["max_streams"] == 1


def test_languages_matches_webui_shape():
    body = client(*make_ctx()).get("/v1/languages").json()
    assert body["total"] == 38
    hant = next(lang for lang in body["common"] if lang["code"] == "zh-Hant")
    assert hant["prompt_name"] == "繁体中文", "prompt 名必须是中文，给英文名模型会回吐原文"


def test_translate_roundtrip():
    r = client(*make_ctx()).post("/v1/translate", json={"text": "你好", "target": "ja"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "<ja>你好"
    assert body["device"] == "NPU"
    assert body["newline"] == "soft"
    assert "request_id" in body and "queue_position" in body


def test_translate_honours_per_request_target():
    body = client(*make_ctx()).post("/v1/translate",
                                    json={"text": "x", "target": "ko"}).json()
    assert body["text"] == "<ko>x"


# ================================================================ 鉴权 / Host
def test_missing_token_is_401():
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx, runtime).post("/v1/translate", json={"text": "hi", "target": "en"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401():
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx, runtime).get("/v1/health", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_valid_token_passes():
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx, runtime).get("/v1/health", headers=auth())
    assert r.status_code == 200


def test_forged_host_is_400():
    ctx, runtime = make_ctx()
    r = client(ctx, runtime).get("/v1/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_host"


def test_health_exemption_keeps_host_and_auth_checks():
    """★ 豁免**不是**"全免"：health 跳过限流与队列，Host 白名单与鉴权一步不少。

    写成「在 `/v1/` 入口处提前放行」的话下面两条会变绿 —— 那样 health 就成了
    免费的未鉴权探测端点。评审阶段有两位成员在这个点上判错过。
    """
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    c = client(ctx, runtime)
    assert c.get("/v1/health", headers={"Host": "evil.example.com"}).status_code == 400
    assert c.get("/v1/health").status_code == 401


# ================================================================ 400 / 413
def test_bad_target_is_400():
    r = client(*make_ctx()).post("/v1/translate", json={"text": "hi", "target": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_target"


def test_empty_input_is_400():
    r = client(*make_ctx()).post("/v1/translate", json={"text": "   ", "target": "en"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_input"


def test_bad_json_is_400():
    r = client(*make_ctx()).post("/v1/translate", content=b"not-json",
                                 headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_json"


def test_oversized_input_is_413():
    ctx, runtime = make_ctx(max_input_chars=50)
    r = client(ctx, runtime).post("/v1/translate", json={"text": "x" * 500, "target": "en"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_huge_declared_length_rejected_before_reading_body():
    ctx, runtime = make_ctx(max_input_chars=50)
    r = client(ctx, runtime).post(
        "/v1/translate",
        content=b'{"text":"x"}',
        headers={"Content-Type": "application/json", "Content-Length": "99999999"},
    )
    assert r.status_code == 413


def test_stream_input_uses_the_stricter_stream_limit():
    """流式不分段，所以 200 字符在批量下合法、在流式下必须 413。"""
    ctx, runtime = make_ctx(max_input_chars=500,
                            config=ServiceConfig(max_stream_chars=160))
    c = client(ctx, runtime)
    assert c.post("/v1/translate", json={"text": "x" * 200, "target": "en"}).status_code == 200
    r = c.post("/v1/translate/stream", json={"text": "x" * 200, "target": "en"})
    assert r.status_code == 413
    assert "静默截断" in r.json()["error"]["message"]


def test_error_body_never_leaks_traceback():
    r = client(*make_ctx()).post("/v1/translate", json={"text": "hi", "target": "nope"})
    assert set(r.json()["error"]) == {"code", "message"}
    assert "Traceback" not in r.text and "npu_translator" not in r.text


# ================================================================ 429 / 503
def test_rate_limit_returns_429():
    ctx, runtime = make_ctx(rate=2)
    c = client(ctx, runtime)
    assert c.get("/v1/languages").status_code == 200
    assert c.get("/v1/languages").status_code == 200
    third = c.get("/v1/languages")
    assert third.status_code == 429
    assert "Retry-After" in third.headers


def test_health_is_not_exempt_from_rate_limit():
    """★ 与 nputweb 的关键差别：`/v1/health` **不**豁免限流。

    豁免限流的唯一理由是「前端心跳节奏不受服务端控制」—— 程序化调用没有这个节奏，
    免了就是给无限刷开一道口子。
    """
    ctx, runtime = make_ctx(rate=2)
    c = client(ctx, runtime)
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/health").status_code == 429


def test_queue_full_returns_503():
    entered = threading.Event()
    release = threading.Event()

    def slow(text, target, source):
        entered.set()
        release.wait(5)
        return text

    ctx, runtime = make_ctx(fn=slow, queue_size=1, gate=QueueGate(max_pending=1))
    c = client(ctx, runtime)
    results: list[int] = []
    t = threading.Thread(
        target=lambda: results.append(
            c.post("/v1/translate", json={"text": "a", "target": "en"}).status_code),
        daemon=True)
    t.start()
    assert entered.wait(5), "第一个请求没能进场"

    r = c.post("/v1/translate", json={"text": "b", "target": "en"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "queue_full"
    release.set()
    t.join(10)
    assert results == [200]


def test_health_and_languages_are_exempt_from_queue():
    """纯读不该占队列位置 —— 让它们占位会把排队的翻译请求挤成 503。"""
    ctx, runtime = make_ctx(gate=QueueGate(max_pending=1))
    assert runtime.gate.try_enter() is not None, "先把队列占满"
    c = client(ctx, runtime)
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/languages").status_code == 200
    assert runtime.gate.depth == 1, "豁免端点不该改变队列深度"
    # 对照组：翻译端点仍然要 503
    assert c.post("/v1/translate", json={"text": "x", "target": "en"}).status_code == 503


def test_lane_busy_returns_503_with_retry_after():
    entered = threading.Event()
    release = threading.Event()

    def slow(text, target, source):
        entered.set()
        release.wait(5)
        return text

    ctx, runtime = make_ctx(fn=slow, config=ServiceConfig(lane_wait_timeout_s=0.1))
    with Client(create_service_app(ctx, runtime), base_url=BASE) as c:
        results: list[int] = []
        t = threading.Thread(
            target=lambda: results.append(
                c.post("/v1/translate", json={"text": "a", "target": "en"}).status_code),
            daemon=True)
        t.start()
        assert entered.wait(5), "第一个请求没能拿到通道"

        r = c.post("/v1/translate", json={"text": "b", "target": "en"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "lane_busy"
        assert "Retry-After" in r.headers
        release.set()
        t.join(10)
        assert results == [200], "第一个请求必须照常成功"


# ================================================================ 504（Q2 孤儿）
def test_timeout_returns_504_with_orphan_header():
    def slow(text, target, source):
        time.sleep(1.0)
        return text

    ctx, runtime = make_ctx(fn=slow, timeout_s=0.2)
    r = client(ctx, runtime).post("/v1/translate", json={"text": "hi", "target": "en"})
    assert r.status_code == 504
    body = r.json()
    assert body["error"]["code"] == "timeout"
    # 机器可读的孤儿标记
    assert r.headers["x-nput-orphan"] == "1"
    # 「不可取消」这条语义必须写在 message 里，否则调用方会误判服务端已经停了
    assert "不可取消" in body["error"]["message"]
    # lane 与队列位必须**立刻**释放，而不是等孤儿跑完
    assert runtime.lane.owner == ""
    assert runtime.gate.depth == 0


def test_orphan_counter_settles_after_the_thread_finishes():
    def slow(text, target, source):
        time.sleep(0.4)
        return text

    ctx, runtime = make_ctx(fn=slow, timeout_s=0.1)
    assert client(ctx, runtime).post(
        "/v1/translate", json={"text": "hi", "target": "en"}).status_code == 504
    assert runtime.orphans == 1, "此时孤儿线程还在跑"
    deadline = time.time() + 5
    while runtime.orphans and time.time() < deadline:
        time.sleep(0.05)
    assert runtime.orphans == 0, "孤儿跑完必须自己把账记平"


def test_health_reports_orphans_and_lane():
    def slow(text, target, source):
        time.sleep(0.8)
        return text

    ctx, runtime = make_ctx(fn=slow, timeout_s=0.1)
    c = client(ctx, runtime)
    assert c.post("/v1/translate", json={"text": "hi", "target": "en"}).status_code == 504
    body = c.get("/v1/health").json()
    assert body["orphans"] >= 1
    assert isinstance(body["lane"], str)


# ================================================================ SSE
def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for chunk in raw.split("\n\n"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        if chunk.startswith(":"):
            events.append((":comment", {"text": chunk[1:].strip()}))
            continue
        name, data = "", ""
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        events.append((name, json.loads(data)))
    return events


def test_sse_event_sequence():
    engine = FakeEngine(("Hel", "lo, ", "世界"))
    ctx, runtime = make_ctx(engine=engine)
    r = client(ctx, runtime).post("/v1/translate/stream", json={"text": "hi", "target": "en"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    # 关掉中间缓冲层的三件套
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"

    events = _parse_sse(r.text)
    kinds = [name for name, _ in events]
    assert kinds[0] == ":comment", "首行必须是注释行（冲掉中间缓冲层）"
    assert kinds[1] == "ready"
    assert kinds[-1] == "done"
    assert kinds[2:-1] == ["token", "token", "token"]
    assert events[1][1]["lane"] == "stream"
    assert events[-1][1]["text"] == "Hello, 世界"
    assert events[-1][1]["chars"] == len("Hello, 世界")
    # 流结束后通道与流槽都要还回去
    assert runtime.lane.owner == ""
    assert runtime.stream_slots.in_use == 0


def test_second_stream_is_503_stream_busy():
    """第二个流**不排队**，直接 503：排着等于把整段输出缓冲下来，语义已经没了。"""
    entered = threading.Event()
    release = threading.Event()

    class HoldingEngine(FakeEngine):
        def stream(self, text, target=None, source=None, max_new_tokens=None):
            self.started.set()
            entered.set()
            release.wait(5)
            yield "x"

    ctx, runtime = make_ctx(engine=HoldingEngine())
    with Client(create_service_app(ctx, runtime), base_url=BASE) as c:
        t = threading.Thread(
            target=lambda: c.post("/v1/translate/stream", json={"text": "a", "target": "en"}),
            daemon=True)
        t.start()
        assert entered.wait(5), "第一个流没能开始"

        r = c.post("/v1/translate/stream", json={"text": "b", "target": "en"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "stream_busy"
        assert "Retry-After" in r.headers
        release.set()
        t.join(10)


def test_translate_is_not_starved_by_a_running_stream():
    """★ N1：流式进行中发 `/v1/translate`，必须**不被饿死**。

    为什么这条会红在错误的实现上：
    `Translator.stream()` 是同步生成器，内部阻塞在 `q.get()`。若把它丢进
    `max_workers=1` 的 executor，一个流就占死整个池；而且没有 lane 的话，
    后到的 translate 会在**提交时**就开始计 `timeout_s` —— 而本用例把
    `timeout_s` 设得比流还短，所以那种实现必然 504。
    我们的实现里计时从「拿到 lane 之后真正跑起来」才开始，因此是 200。
    """
    engine = FakeEngine(("a", "b", "c", "d", "e", "f"), delay=0.25)   # 约 1.5 s
    ctx, runtime = make_ctx(engine=engine, timeout_s=0.5)
    with Client(create_service_app(ctx, runtime), base_url=BASE) as c:
        stream_status: list[int] = []
        t = threading.Thread(
            target=lambda: stream_status.append(
                c.post("/v1/translate/stream", json={"text": "hi", "target": "en"}).status_code),
            daemon=True)
        t.start()
        assert engine.started.wait(5), "流没能开始"
        time.sleep(0.2)                       # 让流真的在生成中

        started = time.perf_counter()
        r = c.post("/v1/translate", json={"text": "hi", "target": "en"})
        elapsed = time.perf_counter() - started

        assert r.status_code == 200, f"翻译被流式饿死了：{r.status_code} {r.text}"
        assert elapsed < 8, "翻译被永久阻塞"

        t.join(15)
        assert stream_status == [200], "流本身也必须正常结束"


# ================================================================ OpenAPI 策略
def test_openapi_json_is_open_by_default():
    """给第三方程序读的契约文件，**不受** `--debug` gate（但仍需过鉴权）。"""
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    r = client(ctx, runtime).get("/v1/openapi.json", headers=auth())
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/v1/translate" in paths
    assert "/v1/translate/stream" in paths
    assert "/v1/health" in paths
    assert "/v1/languages" in paths


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_are_404_by_default(path):
    """交互式文档页 = 给探测者一份地图，默认关。"""
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    assert client(ctx, runtime).get(path, headers=auth()).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_open_with_debug(path):
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN), debug=True)
    assert client(ctx, runtime).get(path, headers=auth()).status_code in {200, 307}


# ================================================================ 前缀 = "/" 的含义
def test_unknown_path_still_requires_auth_before_404():
    """`api_prefix="/"` 的意义：未知路径也要先过 Host 白名单与鉴权才拿 404，
    否则 `/随便什么` 就是一条不需要凭据的探测面。"""
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    c = client(ctx, runtime)
    assert c.get("/whatever").status_code == 401
    assert c.get("/whatever", headers=auth()).status_code == 404


def test_no_static_mount():
    """服务没有页面：多一个面就多一个探测点。"""
    ctx, runtime = make_ctx(token=TokenChecker(TOKEN))
    assert client(ctx, runtime).get("/", headers=auth()).status_code == 404


# ================================================================ 安全基线继承
def test_security_headers_are_present():
    heads = client(*make_ctx()).get("/v1/health").headers
    assert heads["x-frame-options"] == "DENY"
    assert heads["x-content-type-options"] == "nosniff"
    assert heads["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in heads["content-security-policy"]


def test_hsts_only_when_https():
    plain = client(*make_ctx()).get("/v1/health").headers
    assert "strict-transport-security" not in plain

    ctx, runtime = make_ctx()
    runtime.security.https = True
    ctx.security.https = True
    app = create_service_app(ctx, runtime)
    assert "strict-transport-security" in Client(app, base_url=BASE).get("/v1/health").headers


def test_service_security_defaults_differ_from_nputweb_only_in_three_places():
    """三处不同（前缀 / 豁免 / 静态与文档），其余字段必须与 nputweb 默认值一致。

    这条是"安全基线只有一份实现"的机器保证：多一处不同就要说清为什么。
    """
    sec = service_security()
    base = SecurityConfig()
    for name in ("require_token", "max_input_chars", "timeout_s", "queue_size",
                 "rate_per_min", "debug", "https", "ssl_certfile", "ssl_keyfile"):
        assert getattr(sec, name) == getattr(base, name), f"{name} 不该变"

    assert sec.api_prefix == "/" and base.api_prefix == "/api/"
    assert sec.exempt_from_rate == frozenset() and base.exempt_from_rate == frozenset({"/api/health"})
    assert sec.exempt_from_queue == frozenset({"/v1/health", "/v1/languages"})
    assert sec.mount_static is False and base.mount_static is True
    assert sec.openapi_url == "/v1/openapi.json" and base.openapi_url == "/openapi.json"
    # nputserve 的 openapi 不受 --debug gate（它是给第三方程序读的契约文件）
    assert sec.openapi_requires_debug is False and base.openapi_requires_debug is True
    # 两个豁免集合在 nputserve 侧**取值不同** —— 这正是它们必须是两个字段的理由
    assert sec.exempt_from_rate != sec.exempt_from_queue


# ================================================================ CLI
def test_options_from_env_uses_serve_prefix(monkeypatch):
    from npu_translator.server import DEFAULT_SERVE_PORT, options_from_env

    monkeypatch.setenv("NPT_SERVE_HOST", "10.0.0.5")
    monkeypatch.setenv("NPT_SERVE_PORT", "9999")
    monkeypatch.setenv("NPT_SERVE_MAX_STREAM_CHARS", "300")
    monkeypatch.setenv("NPT_SERVE_LANE_WAIT", "0")
    opts = options_from_env()
    assert opts.host == "10.0.0.5"
    assert opts.port == 9999
    assert opts.max_stream_chars == 300
    assert opts.lane_wait == 0
    assert DEFAULT_SERVE_PORT != 9999


def test_serve_port_is_different_from_nputweb_port():
    """两个命令会同时起，默认端口必须错开，否则第二个直接启动失败。"""
    from npu_translator.server import DEFAULT_SERVE_PORT
    from npu_translator.web import DEFAULT_PORT

    assert DEFAULT_SERVE_PORT != DEFAULT_PORT


def test_build_runtime_reuses_resolve_binding_decisions(monkeypatch):
    """D6 / D7 / D11 的判定要复用 `web.cli.resolve_binding`，不能另写一份。"""
    from npu_translator import server as srv

    opts = srv.Options(host="127.0.0.1", tls="off", no_auth=True)
    ctx, runtime, checker, devices, plan = srv.build_runtime(opts)
    assert checker.enabled is False          # 回环 + 明文 + --no-auth → 不鉴权
    assert runtime.security.api_prefix == "/"
    assert runtime.security.mount_static is False
    assert runtime.executor is ctx.executor and runtime.gate is ctx.gate
    assert devices == ["NPU"]
    assert plan.enabled is False


def test_build_runtime_rejects_non_loopback_plaintext(capsys):
    """D7：非回环 + 明文 → 拒绝启动（退出码 3）。"""
    import typer

    from npu_translator import server as srv

    opts = srv.Options(host="192.168.1.5", tls="off", allow_insecure=False)
    with pytest.raises(typer.Exit) as excinfo:
        srv.build_runtime(opts)
    assert excinfo.value.exit_code == 3
    assert "拒绝启动" in capsys.readouterr().err


def test_service_is_reused_not_duplicated():
    """适配层不许写第二份编排：不许 import 池 / 缓存 / 分段的**实现符号**。

    只查 import（不查源码里的字符串）：注释里提到 `TranslationPool` 是允许的
    （说明"为什么不走它"正是要写下来的），但 import 它就是要自己动手了。
    """
    import ast
    import inspect

    from npu_translator import service as svcmod

    tree = ast.parse(inspect.getsource(svcmod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)

    for banned in ("TranslationPool", "TranslationCache", "build_workers", "segment"):
        assert banned not in imported, f"适配层 import 了编排细节：{banned}"
    # 编排的唯一入口与分段**常量**是允许（且应该）引的
    assert "Translator" in imported and "OrchestrateConfig" in imported
    assert "NEWLINE_MODES" in imported


def test_version_option_prints_version(capsys):
    from npu_translator import __version__
    from npu_translator import server as srv

    class _StubCtx:
        """`main()` 是 typer 回调，只需要读 `invoked_subcommand`。"""

        invoked_subcommand = None

    srv.main(_StubCtx(), version=True)  # type: ignore[arg-type]
    assert __version__ in capsys.readouterr().out


# ================================================================ QA 补测（P0-2 覆盖缺口）
# 以下三条是验收时发现的「安全基线上有要求、但没有用例钉住」的点。
# 它们都走 TestClient 或真实构造路径，不做纯逻辑模拟 —— 模拟不经过中间件，真代码坏了照样绿。

def test_d6_forces_token_on_non_loopback(monkeypatch):
    """D6：非回环绑定 → 强制 token。nputserve 必须继承，不能因为换了入口就漏掉。

    `resolve_binding` 本身在 `test_web_app.py` 里有覆盖，这里钉的是**nputserve 真的调了它**
    （`Options` 是鸭子类型，改名/漏传字段会让 `must_auth` 静默变 False）。
    `resolve_tls` 打桩是为了不在用户家目录里真的签一张证书。
    """
    from npu_translator import server as srv

    class _Plan:
        enabled = True
        certfile = ""
        keyfile = ""
        fingerprint = ""

    monkeypatch.setattr(srv, "resolve_tls", lambda *a, **k: _Plan())
    opts = srv.Options(host="192.168.1.5", tls="auto", token="X9#m2!qL7v@rT4wZq")
    _ctx, _runtime, checker, _devices, _plan = srv.build_runtime(opts)
    assert checker.enabled is True, "非回环绑定必须强制鉴权"
    assert checker.weak is False


def test_rate_limit_ignores_x_forwarded_for():
    """限流 key 必须看真实 socket 对端，**不看**可伪造的 `X-Forwarded-For`。

    信了 XFF 等于把限流的 key 交给攻击者自己选：换一个头就换一份配额，
    30/min 的限流可以无限绕过。这里用三个不同的 XFF 打同一个连接，配额必须仍然共享。
    """
    ctx, runtime = make_ctx(rate=2)
    c = client(ctx, runtime)
    codes = [
        c.get("/v1/languages", headers={"X-Forwarded-For": ip}).status_code
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
    ]
    assert codes == [200, 200, 429], f"换 XFF 就换配额 = 限流被绕过：{codes}"


def test_unexpected_exception_returns_scrubbed_500():
    """兜底 500 也必须走脱敏：异常消息里常带本机绝对路径（含用户名）。

    走 `build_router(runtime, service)` 注入一个必炸的适配层，让请求真穿过中间件与路由，
    打到 `except Exception` 那个兜底分支 —— 纯逻辑模拟碰不到它。
    """
    from npu_translator.server import build_router
    from npu_translator.service import TranslatorService
    from npu_translator.web.app import create_app

    class BoomService(TranslatorService):
        async def translate(self, req, *, queue_position: int = 0):
            raise RuntimeError("找不到权重 D:/Users/victim/models/HY-MT/openvino_model.xml")

    ctx, runtime = make_ctx()
    app = create_app(ctx, title="nputserve", version="0",
                     router=build_router(runtime, BoomService(runtime)))
    r = Client(app, base_url=BASE).post("/v1/translate", json={"text": "hi", "target": "en"})

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    # ★ 绝不回传本机路径，也不回传 traceback
    assert "victim" not in r.text and "Traceback" not in r.text
    assert set(r.json()["error"]) == {"code", "message"}


def test_serve_allow_no_auth_env_var(monkeypatch):
    from npu_translator.server import options_from_env

    monkeypatch.setenv("NPT_SERVE_ALLOW_NO_AUTH", "1")
    assert options_from_env().allow_no_auth is True


def test_build_runtime_allows_no_auth_on_non_loopback_with_escape_hatch():
    """nputserve 复用 `web.cli.resolve_binding` —— 逃生舱在这里必须同样生效。

    用 `tls="off"`：auto 会真去签一张自签证书（往用户目录写文件），单测不该有这个副作用。
    """
    from npu_translator import server as srv

    opts = srv.Options(host="192.168.1.5", tls="off", no_auth=True, allow_no_auth=True)
    _ctx, _runtime, checker, _devices, plan = srv.build_runtime(opts)
    assert checker.enabled is False
    assert plan.enabled is False


def test_build_runtime_still_refuses_no_auth_without_escape_hatch(capsys):
    """护栏（nputserve 侧）：没给逃生舱，非回环 --no-auth 依然退 2。"""
    import typer

    from npu_translator import server as srv

    opts = srv.Options(host="192.168.1.5", tls="off", allow_insecure=True, no_auth=True)
    with pytest.raises(typer.Exit) as excinfo:
        srv.build_runtime(opts)
    assert excinfo.value.exit_code == 2
    assert "allow-no-auth" in capsys.readouterr().err
