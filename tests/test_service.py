"""适配层 `service.py` 的单测（纯逻辑 + 真线程，不打 HTTP 栈）。

三条刻意的选择：

1. **用真 `Translator` + 假 worker**（不造假 Translator）：假 Translator 会把
   「编排层真的被调用了 / 语向真的传到位了」这两件事一起免掉，绿灯没有意义。
2. **流式用真线程 + 真 `asyncio.Queue`**：Q2 孤儿的整条链路（专用线程 → 队列 →
   pump 协程 → settle 记账）就是要在真实并发下验证。
3. `service.py` **不 import fastapi** 这条靠**独立子进程**验证（同进程里谁先 import
   都会污染结论）。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from npu_translator.orchestrate import OrchestrateConfig, Translator
from npu_translator.pool import CallableWorker, FatalWorkerError
from npu_translator.service import (
    DEFAULT_MAX_STREAM_CHARS,
    ApiError,
    InferenceLane,
    JobTicket,
    ServiceConfig,
    ServiceRuntime,
    StreamSlots,
    TranslateRequest,
    TranslatorService,
)
from npu_translator.web.app import SecurityConfig
from npu_translator.web.limits import QueueGate

TOKEN_TEXT = ("Hel", "lo, ", "世界")


# ---------------------------------------------------------------- 替身
class FakeEngine:
    """`Translator.stream()` 会取 `worker.engine` 并调它的 `stream()`。"""

    def __init__(self, tokens=TOKEN_TEXT, delay: float = 0.0):
        self._tokens = list(tokens)
        self._delay = delay
        self.started = threading.Event()

    def stream(self, text: str, target: str | None = None, source: str | None = None,
               max_new_tokens: int | None = None):
        self.started.set()
        for tok in self._tokens:
            if self._delay:
                time.sleep(self._delay)
            yield tok


def make_translator(fn=None, engine: FakeEngine | None = None, target: str = "en") -> Translator:
    worker = CallableWorker("NPU", fn or (lambda t, tg, src: f"<{tg}>{t}"))
    if engine is not None:
        worker.engine = engine  # type: ignore[attr-defined]
    return Translator(OrchestrateConfig(target=target), pool_factory=lambda _o: [worker])


def make_runtime(**kwargs) -> ServiceRuntime:
    opts: dict = dict(
        translator=make_translator(),
        security=SecurityConfig(max_input_chars=500, timeout_s=5, queue_size=4),
        config=ServiceConfig(),
    )
    opts.update(kwargs)
    runtime = ServiceRuntime(**opts)  # type: ignore[arg-type]
    return runtime


def make_service(**kwargs) -> TranslatorService:
    return TranslatorService(make_runtime(**kwargs))


# ================================================================ ApiError
def test_api_error_payload_shape():
    exc = ApiError(504, "timeout", "超时了", headers={"X-NPUT-Orphan": "1"})
    assert exc.to_payload() == {"error": {"code": "timeout", "message": "超时了"}}
    assert set(exc.to_payload()["error"]) == {"code", "message"}
    assert exc.headers == {"X-NPUT-Orphan": "1"}
    # str(exc) 要有内容（日志里看得到）
    assert "超时了" in str(exc)


# ================================================================ ServiceConfig
def test_service_config_defaults():
    cfg = ServiceConfig()
    assert cfg.lane_wait_timeout_s == 10.0
    assert cfg.max_streams == 1
    assert cfg.max_stream_chars == DEFAULT_MAX_STREAM_CHARS
    assert cfg.stream_keepalive_s == 15.0


@pytest.mark.parametrize(
    "kwargs",
    [{"max_streams": 0}, {"max_stream_chars": 0}, {"stream_keepalive_s": 0},
     {"lane_wait_timeout_s": -1}],
)
def test_service_config_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        ServiceConfig(**kwargs)


def test_lane_wait_zero_means_infinite():
    """`--lane-wait 0` 是逃生舱：0 合法且表示无限等。"""
    assert ServiceConfig(lane_wait_timeout_s=0).lane_wait_timeout_s == 0


# ================================================================ 校验
def test_parse_translate_happy_path():
    req = make_service().parse_translate({"text": "你好", "target": "ja"})
    assert req.text == "你好" and req.target == "ja" and req.source == "auto"
    assert req.newline == "soft" and req.strict is False


def test_parse_translate_falls_back_to_orchestrate_defaults():
    svc = TranslatorService(make_runtime(translator=make_translator(target="ko")))
    req = svc.parse_translate({"text": "hi"})
    assert req.target == "ko"


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"text": "x", "target": "not-a-lang"}, 400, "bad_target"),
        ({"text": 1}, 400, "bad_request"),
        ({"text": "  "}, 400, "empty_input"),
        ({"text": "hi", "newline": "nope"}, 400, "bad_newline"),
        ({"text": "hi", "max_new_tokens": 0}, 400, "bad_request"),
        ({"text": "hi", "max_new_tokens": "8"}, 400, "bad_request"),
        ([], 400, "bad_json"),
    ],
)
def test_parse_translate_rejects_bad_input(payload, status, code):
    with pytest.raises(ApiError) as excinfo:
        make_service().parse_translate(payload)
    assert excinfo.value.status == status
    assert excinfo.value.code == code


def test_parse_translate_accepts_valid_target():
    req = make_service().parse_translate({"text": "hi", "target": "en"})
    assert req.target == "en"


def test_batch_input_over_limit_is_413():
    with pytest.raises(ApiError) as excinfo:
        make_service().parse_translate({"text": "x" * 501, "target": "en"})
    assert excinfo.value.status == 413
    assert excinfo.value.code == "payload_too_large"


def test_stream_input_over_stream_limit_is_413():
    """流式有自己的、更严的上限（不分段，超了会静默截断）。"""
    with pytest.raises(ApiError) as excinfo:
        make_service().parse_translate({"text": "x" * 200, "target": "en"}, streaming=True)
    assert excinfo.value.status == 413
    assert "静默截断" in excinfo.value.message


def test_stream_ignores_newline_and_strict():
    """流式与分段互斥，所以 newline / strict 一律忽略而不是报错
    （调用方很可能直接复用了 /v1/translate 的请求体）。"""
    req = make_service().parse_translate(
        {"text": "hi", "newline": "nope", "strict": True}, streaming=True)
    assert req.strict is False
    assert req.newline == "nope"   # 原样保留但不参与流式


def test_max_stream_chars_is_much_shorter_than_segment_size():
    """流式不分段，所以它的上限必须**远小于**批量翻译的单段上限（512 字符）。

    这条断言防的是「有人把流式上限顺手调到 5000」—— 那会让流式静默截断。
    """
    assert DEFAULT_MAX_STREAM_CHARS < 512


# ================================================================ 只读端点
def test_languages_shape_matches_webui():
    body = make_service().languages()
    assert body["total"] == 38
    hant = next(l for l in body["common"] if l["code"] == "zh-Hant")
    # prompt 名必须是中文「繁体中文」，给英文名模型会回吐原文
    assert hant["prompt_name"] == "繁体中文"
    assert hant["en_name"] == "Traditional Chinese"


def test_health_exposes_device_lane_orphans_and_limits():
    runtime = make_runtime()
    runtime.translator.prepare()
    body = TranslatorService(runtime).health()
    assert body["status"] == "ready"
    # 硬要求字段名，且与 "device" 同值
    assert body["active_device"] == "NPU" == body["device"]
    assert body["lane"] == ""
    assert body["orphans"] == 0
    assert body["streaming"] is False
    assert body["limits"]["max_stream_chars"] == DEFAULT_MAX_STREAM_CHARS
    assert body["limits"]["lane_wait_timeout_s"] == 10.0


def test_health_status_is_loading_before_prepare():
    assert make_service().health()["status"] == "loading"


def test_health_reports_queue_depth():
    runtime = make_runtime(gate=QueueGate(max_pending=3))
    runtime.gate.try_enter()
    body = TranslatorService(runtime).health()
    assert body["queue"] == 1 and body["waiting"] == 0 and body["max_pending"] == 3


# ================================================================ InferenceLane
def test_lane_acquire_release_and_owner():
    async def main():
        lane = InferenceLane(wait_timeout_s=0.2)
        ticket = await lane.acquire("translate")
        assert lane.owner == "translate"
        assert lane.held_s() >= 0
        lane.release(ticket)
        assert lane.owner == ""
        assert lane.held_s() == 0.0

    asyncio.run(main())


def test_lane_timeout_raises_503_and_does_not_hold_the_lock():
    async def main():
        lane = InferenceLane(wait_timeout_s=0.1)
        first = await lane.acquire("stream")
        with pytest.raises(ApiError) as excinfo:
            await lane.acquire("translate")
        assert excinfo.value.status == 503
        assert excinfo.value.code == "lane_busy"
        assert "retry-after" in {k.lower() for k in excinfo.value.headers}
        # ★ 超时后锁**不能**被持有，否则后面的人永远进不来
        lane.release(first)
        got = await lane.acquire("translate")
        assert lane.owner == "translate"
        lane.release(got)

    asyncio.run(main())


def test_lane_zero_timeout_waits_forever():
    async def main():
        lane = InferenceLane(wait_timeout_s=0)
        first = await lane.acquire("stream")
        task = asyncio.create_task(lane.acquire("translate"))
        await asyncio.sleep(0.12)          # 远超默认 10s？不 —— 0 表示无限等
        assert not task.done(), "0 应当表示无限等待，不该超时"
        lane.release(first)
        assert (await task).kind == "translate"

    asyncio.run(main())


def test_lane_survives_multiple_event_loops():
    """单测里每个 `asyncio.run()` 都是新 loop；不处理的话第二条用例直接红。"""
    lane = InferenceLane(wait_timeout_s=0.2)

    async def one():
        t = await lane.acquire("translate")
        lane.release(t)

    asyncio.run(one())
    asyncio.run(one())


# ================================================================ JobTicket
def test_orphan_accounting_balances():
    runtime = make_runtime()
    ticket = JobTicket(seq=1, kind="translate", runtime=runtime)
    ticket.abandon()
    assert runtime.orphans == 1
    ticket.settle()
    assert runtime.orphans == 0


def test_orphan_abandon_and_settle_are_idempotent():
    runtime = make_runtime()
    ticket = JobTicket(seq=1, kind="translate", runtime=runtime)
    ticket.abandon()
    ticket.abandon()
    assert runtime.orphans == 1
    ticket.settle()
    ticket.settle()
    assert runtime.orphans == 0
    # settle 之后再 abandon 不该把计数打回去（那会让 health 永远显示有孤儿）
    ticket.abandon()
    assert runtime.orphans == 0


def test_settle_without_abandon_is_a_noop():
    runtime = make_runtime()
    JobTicket(seq=1, kind="translate", runtime=runtime).settle()
    assert runtime.orphans == 0


# ================================================================ 翻译
def test_translate_returns_outcome_plus_service_fields():
    async def main():
        svc = make_service()
        data = await svc.translate(
            svc.parse_translate({"text": "你好", "target": "ja"}), queue_position=3)
        assert data["text"] == "<ja>你好"
        assert data["device"] == "NPU"
        assert data["newline"] == "soft"
        assert data["queue_position"] == 3
        assert data["request_id"] >= 1
        assert svc.runtime.lane.owner == "", "lane 必须已释放"

    asyncio.run(main())


def test_translate_maps_worker_exception_to_scrubbed_500():
    """worker 级致命异常要变成 500 + **脱敏**摘要（绝不回传本机路径）。

    ⚠️ 必须抛 `FatalWorkerError`：普通异常会被 `TranslationPool._do_one` 判成
    「段级失败」并保留原文，根本不会冒到适配层（那是编排层的既定行为）。
    """

    def boom(text, target, source):
        raise FatalWorkerError("找不到模型 D:/fake/models/HY-MT/openvino_model.xml")

    async def main():
        svc = make_service(translator=make_translator(fn=boom))
        with pytest.raises(ApiError) as excinfo:
            await svc.translate(svc.parse_translate({"text": "hi", "target": "en"}))
        assert excinfo.value.status == 500
        assert excinfo.value.code == "internal_error"
        # ★ 绝不回传本机路径
        assert "fake" not in excinfo.value.message
        assert "<path>" in excinfo.value.message

    asyncio.run(main())


def test_translate_timeout_abandons_and_settles_later():
    """Q2：504 之后孤儿 +1，线程跑完自己把账记平。"""
    def slow(text, target, source):
        time.sleep(0.6)
        return text

    async def main():
        runtime = make_runtime(
            translator=make_translator(fn=slow),
            security=SecurityConfig(max_input_chars=500, timeout_s=0.15, queue_size=4),
        )
        svc = TranslatorService(runtime)
        with pytest.raises(ApiError) as excinfo:
            await svc.translate(svc.parse_translate({"text": "hi", "target": "en"}))
        assert excinfo.value.status == 504
        assert excinfo.value.code == "timeout"
        assert excinfo.value.headers["X-NPUT-Orphan"] == "1"
        # 「不可取消」这条语义必须写在 message 里，否则调用方会误判服务端已停
        assert "不可取消" in excinfo.value.message
        # ★ lane 必须**立刻**释放（不等孤儿），否则后面的请求全被堵死
        assert runtime.lane.owner == ""
        assert runtime.orphans == 1, "此时孤儿线程还在跑"

    asyncio.run(main())
    # 线程跑完后自己 settle（这里 sleep 只是为了等它落地）


def test_orphan_settles_after_thread_finishes():
    def slow(text, target, source):
        time.sleep(0.3)
        return text

    async def main():
        runtime = make_runtime(
            translator=make_translator(fn=slow),
            security=SecurityConfig(max_input_chars=500, timeout_s=0.1, queue_size=4),
        )
        svc = TranslatorService(runtime)
        with pytest.raises(ApiError):
            await svc.translate(svc.parse_translate({"text": "hi", "target": "en"}))
        return runtime

    runtime = asyncio.run(main())
    deadline = time.time() + 5
    while runtime.orphans and time.time() < deadline:
        time.sleep(0.05)
    assert runtime.orphans == 0, "孤儿跑完必须自己把账记平"


def test_translate_lane_busy_when_lane_held():
    async def main():
        runtime = make_runtime(config=ServiceConfig(lane_wait_timeout_s=0.1))
        svc = TranslatorService(runtime)
        held = await runtime.lane.acquire("stream")
        with pytest.raises(ApiError) as excinfo:
            await svc.translate(svc.parse_translate({"text": "hi", "target": "en"}))
        assert excinfo.value.code == "lane_busy"
        runtime.lane.release(held)

    asyncio.run(main())


# ================================================================ 流槽
def test_stream_slots_fail_fast_without_queueing():
    slots = StreamSlots(1)
    assert slots.try_acquire() is True
    assert slots.try_acquire() is False, "第二个流必须**快失败**，不能排队"
    assert slots.in_use == 1 and slots.free == 0
    slots.release()
    assert slots.try_acquire() is True
    slots.release()
    slots.release()      # 多释放一次不该变负
    assert slots.in_use == 0


# ================================================================ 流式
def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    """把已格式化的 SSE 文本解析成 [(event, data)]。"""
    import json

    events: list[tuple[str, dict]] = []
    name, data = "", ""
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


def test_stream_events_sequence():
    engine = FakeEngine(TOKEN_TEXT)
    svc = make_service(translator=make_translator(engine=engine))

    async def main():
        session = await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        out: list[str] = []

        async def never_gone() -> bool:
            return False

        async for line in svc.stream_events(session.req, never_gone, session=session):
            out.append(line)
        return "".join(out)

    raw = asyncio.run(main())
    events = _parse_sse(raw)
    kinds = [name for name, _ in events]
    assert kinds[0] == ":comment", "首行必须是注释行（冲掉中间缓冲层）"
    assert kinds[1] == "ready"
    assert kinds[-1] == "done"
    assert kinds[2:-1] == ["token"] * len(TOKEN_TEXT)

    ready = events[1][1]
    assert ready["lane"] == "stream"
    assert ready["max_stream_chars"] == DEFAULT_MAX_STREAM_CHARS
    done = events[-1][1]
    assert done["text"] == "".join(TOKEN_TEXT)
    assert done["chars"] == len("".join(TOKEN_TEXT))
    assert done["chars_per_second"] > 0
    # 流式没有池内计时，infer_s 与 elapsed_s 同值（字段保留是为了形状一致）
    assert done["infer_s"] == done["elapsed_s"]


def test_stream_releases_lane_and_slot_when_done():
    engine = FakeEngine(TOKEN_TEXT)
    runtime = make_runtime(translator=make_translator(engine=engine))
    svc = TranslatorService(runtime)

    async def main():
        session = await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        assert runtime.stream_slots.in_use == 1
        assert runtime.lane.owner == "stream"

        async def never_gone() -> bool:
            return False

        async for _ in svc.stream_events(session.req, never_gone, session=session):
            pass
        assert runtime.stream_slots.in_use == 0
        assert runtime.lane.owner == ""

    asyncio.run(main())


def test_second_stream_is_503_stream_busy():
    runtime = make_runtime(translator=make_translator(engine=FakeEngine()))
    svc = TranslatorService(runtime)

    async def main():
        await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        with pytest.raises(ApiError) as excinfo:
            await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        assert excinfo.value.status == 503
        assert excinfo.value.code == "stream_busy"
        assert "Retry-After" in excinfo.value.headers

    asyncio.run(main())


def test_acquire_stream_gives_slot_back_when_lane_busy():
    """拿了槽却没拿到通道 → 槽必须还回去，否则流永久堵死。"""
    runtime = make_runtime(
        translator=make_translator(engine=FakeEngine()),
        config=ServiceConfig(lane_wait_timeout_s=0.1),
    )
    svc = TranslatorService(runtime)

    async def main():
        held = await runtime.lane.acquire("translate")
        with pytest.raises(ApiError) as excinfo:
            await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        assert excinfo.value.code == "lane_busy"
        assert runtime.stream_slots.in_use == 0, "槽没还回去 → 流式永久堵死"
        runtime.lane.release(held)

    asyncio.run(main())


def test_stream_client_disconnect_abandons_and_releases():
    """客户端断开：产出 error{abandoned:true}，lane 与流槽都要还回去。"""
    engine = FakeEngine(TOKEN_TEXT, delay=0.01)
    runtime = make_runtime(translator=make_translator(engine=engine))
    svc = TranslatorService(runtime)
    state = {"gone": False}

    async def is_gone() -> bool:
        return state["gone"]

    async def main():
        session = await svc.acquire_stream(TranslateRequest(text="hi", target="en"))
        out: list[str] = []
        async for line in svc.stream_events(session.req, is_gone, session=session):
            out.append(line)
            if line.startswith("event: token"):
                state["gone"] = True      # 收到第一个 token 之后"断开"
        return "".join(out)

    raw = asyncio.run(main())
    events = _parse_sse(raw)
    last_name, last_data = events[-1]
    assert last_name == "error"
    assert last_data["abandoned"] is True
    assert "不可取消" in last_data["message"]
    assert runtime.stream_slots.in_use == 0
    assert runtime.lane.owner == ""


def test_stream_error_event_on_engine_failure():
    class BoomEngine:
        def stream(self, text, target=None, source=None, max_new_tokens=None):
            raise RuntimeError("NPU 被别的进程占用了 D:/npu/dev")
            yield  # pragma: no cover

    runtime = make_runtime(translator=make_translator(engine=BoomEngine()))  # type: ignore[arg-type]
    svc = TranslatorService(runtime)

    async def main():
        session = await svc.acquire_stream(TranslateRequest(text="hi", target="en"))

        async def never_gone() -> bool:
            return False

        out = [line async for line in svc.stream_events(session.req, never_gone, session=session)]
        return "".join(out)

    raw = asyncio.run(main())
    name, data = _parse_sse(raw)[-1]
    assert name == "error"
    assert data["code"] == "internal_error"
    assert "npu" not in data["message"], "错误体绝不回传本机路径"


# ================================================================ 架构约定
def test_service_module_does_not_import_fastapi():
    """`import npu_translator.service` 不得拉起 fastapi / starlette / uvicorn。

    这条必须在**独立进程**里验证：同进程里任何测试先 import 了 fastapi 都会污染结论
    （`sys.modules` 是进程级共享的）。
    """
    import os
    import subprocess
    import sys

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import sys, importlib;"
        "importlib.import_module('npu_translator.service');"
        "print([m in sys.modules for m in ('fastapi','starlette','uvicorn')])"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[False, False, False]"


def test_max_stream_chars_stays_under_the_silent_truncation_boundary():
    """`max_stream_chars` 是**实测**出来的（中/日源 165、英源 468），别随手放大。

    这条断言不校验物理正确性（那需要真机），而是钉住"有人把它改成 5000 之类的整值"
    这种回归 —— 那会让流式静默截断。
    """
    assert DEFAULT_MAX_STREAM_CHARS <= 165
