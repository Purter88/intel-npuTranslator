"""orchestrate.py 的单测（SPEC.md · 架构与目录结构 · 共用编排层 orchestrate.py）。

为什么这一层值得单测：它是 CLI / TUI / WebUI / M3 的**唯一**编排实现，
「批内去重」「hard 打包行数校验」这类踩过坑的行为一旦漂移，四个入口一起错。

全部用例都走 `pool_factory` 注入替身 worker —— **不加载模型**，保持套件的秒级特性。

给侦查者的提示：segments 的合并粒度受 `cfg.SEGMENT_MAX_CHARS` 控制，
想让每行单独成段就把它打到很小（下面 `SMALL` 的用途）。
"""
from __future__ import annotations

import pytest

from npu_translator import config as cfg
from npu_translator.orchestrate import OrchestrateConfig, Outcome, Translator
from npu_translator.pool import CallableWorker

SMALL = 4  # 小到每行只能单独成段的 max_chars


def echo_worker(name: str = "NPU", suffix: str = "", boom_on: str | None = None):
    """替身 worker：把每段原样回吐（可加后缀），便于观察段数与顺序。"""
    calls: list[str] = []

    def fn(text: str, target: str, source: str) -> str:
        calls.append(text)
        if boom_on is not None and text == boom_on:
            raise RuntimeError("模拟段失败")
        return f"{suffix}{text}"

    return CallableWorker(name, fn), calls


def make_translator(text_workers=None, **opts_kwargs) -> Translator:
    opts = OrchestrateConfig(**opts_kwargs)

    def factory(_o: OrchestrateConfig):
        return text_workers if text_workers is not None else [echo_worker()[0]]

    return Translator(opts, pool_factory=factory)


# ---------------------------------------------------------------- 配置
def test_bad_newline_is_rejected():
    with pytest.raises(ValueError, match="newline"):
        OrchestrateConfig(newline="bogus")


def test_supported_target_flag():
    assert OrchestrateConfig(target="en").supported_target is True
    assert OrchestrateConfig(target="xx-invalid").supported_target is False


# ---------------------------------------------------------------- 基本翻译
def test_translate_single_line():
    """单句：默认 SEGMENT_MAX_CHARS(512) 下应当只出一个单元。"""
    orch = make_translator()
    out = orch.translate("今天天气不错。")
    assert isinstance(out, Outcome)
    assert out.units == 1
    assert out.lines == 1
    assert out.chars == 7
    assert out.ok


def test_short_orchestration_splits_to_units(monkeypatch):
    """段长上限收紧时，同一句话会被切成多个单元 —— 这是 NPU KV cache 768 的必然要求。"""
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    orch = make_translator()
    out = orch.translate("今天天气不错。")
    assert out.units >= 1


def test_device_reports_worker_name():
    orch = make_translator()
    assert orch.device == "NPU"
    assert orch.devices == ["NPU"]


def test_workers_empty_before_prepare_is_fine():
    """构造不碰模型，也不该因为没 prepare 就崩 —— WebUI 启动横幅要读 device。"""
    orch = make_translator()
    assert orch.is_ready is False
    assert orch.workers  # 但 worker 列表已经在了


# ---------------------------------------------------------------- 批内去重（★ 踩过坑）
def test_duplicate_segments_are_translated_once(monkeypatch):
    """同一批里重复段只翻一次，翻完回填。不做去重的话重复段会被翻 N 遍。"""
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    worker, calls = echo_worker()
    orch = make_translator([worker])
    out = orch.translate("aaa\nbbb\naaa\nccc")

    assert out.units == 4
    assert len(calls) == 3, f"重复段应只翻一次，实际调用了 {len(calls)} 次"
    assert out.model_calls == 3
    assert out.reused == 1  # 回填的那一段
    lines = out.text.split("\n")
    assert lines[0] == lines[2], "重复段必须拿到**同一份**译文"


def test_cache_hit_skips_model(monkeypatch):
    """第二次翻译同样内容应整段命中缓存，一次都不打扰模型。"""
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    worker, calls = echo_worker()
    orch = make_translator([worker])

    first = orch.translate("一段话。")
    assert len(calls) == 1
    second = orch.translate("一段话。")

    assert len(calls) == 1, "命中缓存后不该再调模型"
    assert second.cache_hits == 1
    assert second.model_calls == 0
    assert second.cached is True
    assert first.cached is False
    assert second.text == first.text


def test_no_cache_disables_reuse(monkeypatch):
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    worker, calls = echo_worker()
    orch = make_translator([worker], no_cache=True)
    orch.translate("一段话。")
    orch.translate("一段话。")
    assert len(calls) == 2


# ---------------------------------------------------------------- 段失败
def test_failed_segment_keeps_source_and_is_reported(monkeypatch):
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    worker, _ = echo_worker(boom_on="bbb")
    orch = make_translator([worker])
    out = orch.translate("aaa\nbbb\nccc")

    assert out.failed == 1
    assert out.ok is False
    assert out.failures, "失败明细要留给调用方（CLI 逐条 warn，WebUI 提示哪段是原文）"
    lines = out.text.split("\n")
    assert lines[1] == "bbb", "失败的段必须保留原文"
    assert lines[0] == "aaa"


# ---------------------------------------------------------------- hard 打包回退
def test_packed_rows_mismatch_falls_back_to_per_line(monkeypatch):
    """hard 打包一次请求出多行；模型吐的行数对不上 → 退回逐行重翻，行数必须 1:1。"""
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", 200)

    def broken(text: str, target: str, source: str) -> str:
        # 打包进来的是多行，这里故意只回一行 → 行数不匹配
        return "only one line"

    orch = make_translator([CallableWorker("NPU", broken)], newline="hard")
    out = orch.translate("第一行。\n第二行。\n第三行。")

    assert out.lines == 3
    assert len(out.text.split("\n")) == 3, "打包失败必须退回逐行，行数不能掉"
    assert out.packed_retries >= 1


def test_hard_mode_keeps_line_count(monkeypatch):
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", 200)
    worker, _ = echo_worker()
    orch = make_translator([worker], newline="hard")
    out = orch.translate("a\nb\nc\nd")
    assert len(out.text.split("\n")) == 4


# ---------------------------------------------------------------- no-segment
def test_no_segment_produces_single_unit():
    orch = make_translator(no_segment=True)
    out = orch.translate("一行。\n两行。\n三行。")
    assert out.units == 1
    assert out.lines == 3


# ---------------------------------------------------------------- 进度与吞吐
def test_progress_callback_reports_each_unit(monkeypatch):
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    seen: list[tuple[int, int]] = []
    worker, _ = echo_worker()
    orch = Translator(
        OrchestrateConfig(),
        pool_factory=lambda _o: [worker],
        on_progress=lambda d, t: seen.append((d, t)),
    )
    orch.translate("aaa\nbbb\nccc")
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert orch.progress.done == 3


def test_progress_snapshot_starts_idle():
    orch = make_translator()
    snap = orch.progress
    assert snap.active is False and snap.total == 0


def test_chars_per_second_is_zero_without_inference():
    """全命中缓存时没有推理耗时，吞吐必须是 0 而不是除零崩溃。"""
    out = Outcome(text="x", units=1, infer_s=0.0)
    assert out.chars_per_second == 0.0
    assert out.cached is False  # units>0 但 reused=0 → 不是缓存命中


def test_outcome_to_dict_has_api_shape():
    data = Outcome(text="hi", device="NPU", units=2, chars=5).to_dict()
    for key in ("text", "device", "segments", "chars", "elapsed_s", "chars_per_second",
                "failed", "model_calls", "cached"):
        assert key in data, f"WebUI 的 API 契约要用到 {key}"


# ---------------------------------------------------------------- 目标语言可被请求覆盖
def test_translate_accepts_runtime_target(monkeypatch):
    monkeypatch.setattr(cfg, "SEGMENT_MAX_CHARS", SMALL)
    worker, calls = echo_worker()
    orch = make_translator([worker], target="en")
    orch.translate("一段话。", target="ja")
    # 换了目标语言 = 换了缓存 key，不该命中上一次的结果
    orch.translate("一段话。", target="ja")
    assert len(calls) == 1, "同语向第二次应命中缓存"
