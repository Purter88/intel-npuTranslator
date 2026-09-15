"""异构调度单测（CLI 管道契约）——**用假 worker，不加载模型**。

覆盖：有序重组 / 失败换人重试 / 重试上限 / worker 致命摘除 / 预热降级 / LPT / CPU 属性构造。
"""
from __future__ import annotations

import threading
import time

import pytest

from npu_translator.pool import (
    CallableWorker,
    FatalWorkerError,
    TranslationPool,
    cpu_pipeline_props,
)


def make_worker(name: str, delay: float = 0.0, fail_on: set[str] | None = None,
                fatal_on: set[str] | None = None):
    fail_on = fail_on or set()
    fatal_on = fatal_on or set()

    def fn(text: str, target: str, source: str) -> str:
        time.sleep(delay)
        if text in fatal_on:
            raise FatalWorkerError(f"{name} 设备掉线")
        if text in fail_on:
            raise RuntimeError(f"{name} 推理出错")
        return f"{name}|{text}"

    return CallableWorker(name, fn)


def test_empty_input():
    pool = TranslationPool([make_worker("A")])
    assert pool.run([], "en") == []


def test_serial_path_single_worker():
    w = make_worker("A")
    pool = TranslationPool([w])
    res = pool.run(["一", "二", "三"], "en")
    assert [r.text for r in res] == ["A|一", "A|二", "A|三"]
    assert all(r.ok for r in res)


def test_single_task_uses_serial_path():
    pool = TranslationPool([make_worker("A"), make_worker("B")])
    res = pool.run(["唯一"], "en")
    assert res[0].text == "A|唯一"


def test_output_order_matches_input_order():
    """核心不变量：并行也不能打乱顺序（CLI 管道契约）。"""
    slow = make_worker("SLOW", delay=0.02)
    fast = make_worker("FAST", delay=0.001)
    texts = [f"t{i}" for i in range(12)]
    pool = TranslationPool([slow, fast])
    res = pool.run(texts, "en")
    assert [r.index for r in res] == list(range(12))
    for i, r in enumerate(res):
        assert r.text.endswith(f"|t{i}")


def test_both_workers_are_actually_used():
    fast = make_worker("FAST", delay=0.001)
    slow = make_worker("SLOW", delay=0.01)
    pool = TranslationPool([fast, slow])
    pool.run([f"t{i}" for i in range(8)], "en")
    assert fast.calls and slow.calls, "两个设备都该被派到活（动态派活的意义）"


def test_longest_task_goes_first_lpt():
    """LPT：长段优先出队，避免尾巴上卡一个长段让快设备空等。"""
    long_text = "x" * 500
    texts = [long_text] + ["短" * 5 for _ in range(6)]
    a = make_worker("A", delay=0.005)
    b = make_worker("B", delay=0.005)
    pool = TranslationPool([a, b])
    pool.run(texts, "en")
    first_calls = {w.calls[0] for w in (a, b) if w.calls}
    assert long_text in first_calls


def test_failed_task_is_retried_by_another_worker():
    """CLI 管道契约：失败段在另一个设备重试。"""
    bad = make_worker("BAD", fail_on={"难句"})
    good = make_worker("GOOD")
    pool = TranslationPool([bad, good], max_retries=2)
    res = pool.run(["易句", "难句"], "en")
    by_index = {r.index: r for r in res}
    assert by_index[1].ok
    assert by_index[1].device == "GOOD"
    assert "难句" in good.calls


def test_retry_exhausted_is_reported_as_error():
    bad = make_worker("BAD", fail_on={"难句"})
    pool = TranslationPool([bad], max_retries=1)
    res = pool.run(["难句"], "en")
    assert not res[0].ok
    assert "推理出错" in (res[0].error or "")


def test_fatal_worker_is_removed_and_other_keeps_going():
    """CLI 管道契约（降级链）：一条 pipeline 挂了，退回单设备继续，任务不能丢。"""
    doomed = make_worker("DOOMED", fatal_on={"t0"})
    healthy = make_worker("HEALTHY", delay=0.001)
    degraded: list[str] = []
    pool = TranslationPool([doomed, healthy], on_degrade=lambda name, err: degraded.append(name))
    res = pool.run([f"t{i}" for i in range(4)], "en")
    assert all(r.ok for r in res), [r.error for r in res]
    assert [r.index for r in res] == [0, 1, 2, 3]
    assert res[0].device == "HEALTHY"
    assert degraded == ["DOOMED"]
    assert "DOOMED" in pool.dead


def test_prepare_failure_drops_worker():
    def boom() -> None:
        raise RuntimeError("模型加载失败")

    bad = CallableWorker("BAD", lambda t, a, b: t, prepare_fn=boom)
    good = make_worker("GOOD")
    pool = TranslationPool([bad, good])
    alive = pool.prepare()
    assert [w.name for w in alive] == ["GOOD"]
    assert "BAD" in pool.dead


def test_prepare_all_failed_raises():
    def boom() -> None:
        raise RuntimeError("nope")

    pool = TranslationPool([
        CallableWorker("A", lambda t, a, b: t, prepare_fn=boom),
        CallableWorker("B", lambda t, a, b: t, prepare_fn=boom),
    ])
    with pytest.raises(FatalWorkerError):
        pool.prepare()


def test_no_worker_raises():
    with pytest.raises(FatalWorkerError):
        TranslationPool([]).run(["x"], "en")


def test_progress_callback_counts_every_task_once():
    seen: list[int] = []
    lock = threading.Lock()

    def on_done(done: int, total: int) -> None:
        with lock:
            seen.append(done)

    pool = TranslationPool([make_worker("A", 0.001), make_worker("B", 0.002)], on_done=on_done)
    pool.run([f"t{i}" for i in range(6)], "en")
    assert sorted(seen) == list(range(1, 7))


def test_results_carry_device_and_elapsed():
    pool = TranslationPool([make_worker("A")])
    res = pool.run(["x"], "en")
    assert res[0].device == "A"
    assert res[0].elapsed_s >= 0


# ---------------------------------------------------------------- CPU 属性
def test_cpu_props_default_is_empty():
    """默认 0 = OpenVINO 自动，**不写死核心数**（R13）。"""
    assert cpu_pipeline_props() == {}
    assert cpu_pipeline_props(threads=0) == {}


def test_cpu_props_explicit_threads():
    assert cpu_pipeline_props(threads=8)["INFERENCE_NUM_THREADS"] == 8
    assert cpu_pipeline_props(threads="12")["INFERENCE_NUM_THREADS"] == 12


def test_cpu_props_half():
    import os

    assert cpu_pipeline_props(threads="half")["INFERENCE_NUM_THREADS"] == max(1, (os.cpu_count() or 2) // 2)


def test_cpu_props_core_type():
    assert cpu_pipeline_props(core_type="pcore")["SCHEDULING_CORE_TYPE"] == "PCORE_ONLY"
    assert cpu_pipeline_props(core_type="ecore")["SCHEDULING_CORE_TYPE"] == "ECORE_ONLY"
    assert "SCHEDULING_CORE_TYPE" not in cpu_pipeline_props(core_type="any")


def test_cpu_props_hyper_threading():
    assert cpu_pipeline_props(ht="off")["ENABLE_HYPER_THREADING"] == "NO"
    assert cpu_pipeline_props(ht="on")["ENABLE_HYPER_THREADING"] == "YES"
    assert "ENABLE_HYPER_THREADING" not in cpu_pipeline_props(ht=None)


def test_cpu_props_unknown_value_is_ignored():
    assert cpu_pipeline_props(threads="nonsense") == {}
    assert cpu_pipeline_props(core_type="nonsense") == {}
