"""分段与换行三档单测（SPEC.md · CLI 管道契约）。

验收点：
- 硬折行散文 6 行 → 输出仍是 6 行（R12）
- hard 模式：日志 4 行 → 切 4 段，空行透传不送模型
- 中文拼接无多余空格（旧 `" ".join` 的 bug）
不加载模型，秒级。
"""
from __future__ import annotations

import pytest

from npu_translator.segment import AUTO, HARD, SOFT, Plan, segment, split_sentences

# 硬折行散文：6 个物理行，只有 2 个句末标点。这是 R12 里"6 行 -> 4 行"的原始用例
PROSE = (
    "今天天气不错，\n"
    "我们决定去公园散步，\n"
    "顺便在路边的咖啡店\n"
    "买两杯拿铁。公园里\n"
    "人很多，孩子们在草地上奔跑，\n"
    "笑声一直传到湖边。"
)

LOG = (
    "2026-09-12 10:00:00 INFO  start\n"
    "2026-09-12 10:00:01 WARN  retry\n"
    "2026-09-12 10:00:02 ERROR failed\n"
    "2026-09-12 10:00:03 INFO  done"
)


def identity(plan: Plan) -> list[str]:
    """用"模型原样返回"来隔离分段逻辑：验证的纯粹是结构保真。"""
    return [u.text for u in plan.units]


# ---------------------------------------------------------------- 基本切分
def test_split_sentences_chinese():
    assert len(split_sentences("今天天气很好。我们去公园吧！好吗？")) == 3


def test_segment_empty():
    plan = segment("")
    assert plan.units == []
    assert plan.join([]) == ""


def test_segment_whitespace_only():
    plan = segment("   \n\n  ")
    assert plan.units == []
    assert plan.lines == ["   ", "", "  "]


def test_segment_indices_are_sequential():
    plan = segment("第一句。第二句。\n\n第三句。")
    assert [u.index for u in plan.units] == list(range(len(plan.units)))


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        segment("x", mode="nonsense")


# ---------------------------------------------------------------- R12：换行保真
def test_soft_keeps_six_lines_of_hard_wrapped_prose():
    """旧实现输出 4 行，修复后必须 6 行。"""
    plan = segment(PROSE, mode=SOFT)
    out = plan.join(identity(plan))
    assert out.split("\n") == PROSE.split("\n")
    assert len(out.split("\n")) == 6


def test_hard_keeps_six_lines_of_hard_wrapped_prose():
    plan = segment(PROSE, mode=HARD)
    out = plan.join(identity(plan))
    assert len(out.split("\n")) == 6


def test_auto_keeps_six_lines_of_hard_wrapped_prose():
    plan = segment(PROSE, mode=AUTO)
    out = plan.join(identity(plan))
    assert len(out.split("\n")) == 6


def test_hard_splits_log_into_four_units():
    """hard 模式日志 4 行切 4 段（见 SPEC.md · 回归验收清单）。"""
    plan = segment(LOG, mode=HARD)
    assert len(plan.units) == 4
    assert [u.text for u in plan.units] == LOG.split("\n")
    assert plan.join(identity(plan)) == LOG


def test_hard_blank_lines_pass_through_and_are_not_translated():
    plan = segment("第一行\n\n第三行", mode=HARD)
    assert len(plan.units) == 2          # 空行不送模型
    assert plan.join(identity(plan)) == "第一行\n\n第三行"


def test_soft_keeps_paragraph_blank_lines():
    plan = segment("第一段。\n\n第二段。", mode=SOFT)
    assert len(plan.units) == 2
    assert plan.join(identity(plan)) == "第一段。\n\n第二段。"


def test_multiple_blank_lines_preserved():
    plan = segment("a\n\n\nb", mode=HARD)
    assert plan.join(identity(plan)) == "a\n\n\nb"


def test_trailing_newline_preserved():
    plan = segment("a\n", mode=SOFT)
    assert plan.join(identity(plan)) == "a\n"


def test_markdown_list_lines_kept():
    md = "# 标题\n- 第一项\n- 第二项\n- 第三项"
    plan = segment(md, mode=AUTO)
    assert plan.join(identity(plan)) == md


# ---------------------------------------------------------------- join() 空格修复
def test_no_spaces_between_chinese_fragments():
    """旧 `" ".join()` 会给中文插空格。"""
    plan = segment("第一句。第二句。第三句。", mode=HARD, max_chars=4)
    out = plan.join(["译一。", "译二。", "译三。"])
    assert " " not in out
    assert out == "译一。译二。译三。"


def test_space_added_between_ascii_words():
    plan = segment("A. B. C.", mode=HARD, max_chars=2)
    out = plan.join(["One", "Two", "Three"])
    assert out == "One Two Three"


def test_line_fragments_are_glued_not_overwritten():
    """同一行被硬切成多段时，旧实现会互相覆盖导致丢内容。"""
    long_line = "这是一句很长的话没有标点" * 20
    plan = segment(long_line, mode=HARD, max_chars=50)
    assert len(plan.units) > 1
    out = plan.join([f"<{i}>" for i in range(len(plan.units))])
    assert all(f"<{i}>" in out for i in range(len(plan.units)))
    assert len(out.split("\n")) == 1


# ---------------------------------------------------------------- max_chars
def test_respects_max_chars_soft():
    plan = segment("这是一句话。" * 200, mode=SOFT, max_chars=100)
    assert all(len(u.text) <= 100 for u in plan.units)


def test_respects_max_chars_hard():
    plan = segment("这是一句话。" * 200, mode=HARD, max_chars=100)
    assert all(len(u.text) <= 100 for u in plan.units)


def test_very_long_single_line_is_hard_split():
    plan = segment("无标点长文本" * 300, mode=HARD, max_chars=200)
    assert len(plan.units) > 1
    assert all(len(u.text) <= 200 for u in plan.units)


# ---------------------------------------------------------------- hard 打包快路径
def test_pack_batches_short_lines_into_one_request():
    plan = segment(LOG, mode=HARD, pack=True)
    assert len(plan.units) == 1
    unit = plan.units[0]
    assert unit.packed is True
    assert unit.line_count == 4
    assert unit.sources == tuple(LOG.split("\n"))


def test_pack_respects_max_chars():
    plan = segment("\n".join(f"line {i}" for i in range(100)), mode=HARD, pack=True, max_chars=120)
    assert len(plan.units) > 1
    assert all(len(u.text) <= 120 for u in plan.units)


def test_pack_is_ignored_outside_hard_mode():
    plan = segment(PROSE, mode=SOFT, pack=True)
    assert all(not u.packed for u in plan.units)


def test_packed_output_line_count_is_validated():
    plan = segment(LOG, mode=HARD, pack=True)
    good = [LOG]  # 模型按行返回 4 行
    assert plan.validate(good) == []
    bad = ["只有一行"]  # 模型吞掉了换行
    assert plan.validate(bad) == [0]


def test_validate_reports_mismatched_units_only():
    plan = segment("第一行。\n第二行。\n第三行。", mode=HARD, pack=True)
    assert plan.validate(["a\nb\nc"]) == []
    assert plan.validate(["a\nb"]) == [0]


def test_join_degrades_gracefully_on_line_count_mismatch():
    """内容不丢，行数会掉——降级策略，不做静默截断。"""
    plan = segment(LOG, mode=HARD, pack=True)
    out = plan.join(["全部挤在一行"])
    assert "全部挤在一行" in out
    assert len(out.split("\n")) == 4


# ---------------------------------------------------------------- 跨行单元行数校验
def test_soft_unit_covers_multiple_lines():
    plan = segment(PROSE, mode=SOFT, max_chars=512)
    assert sum(u.line_count for u in plan.units) == 6


def test_auto_detects_hard_boundary():
    """上行以句号结尾 / 下行以列表标记开头 → 判硬边界，不合并。"""
    text = "这是第一句。\n- 列表项一\n- 列表项二"
    plan = segment(text, mode=AUTO)
    starts = sorted({u.line_start for u in plan.units})
    assert starts == [0, 1, 2]


def test_auto_merges_hard_wrapped_prose():
    """散文被硬折行（上行不以句末标点结尾）→ 合并翻译，不从中间劈开。"""
    text = "这是一个被硬折行的\n长句子，它应该被\n合并起来翻译。"
    plan = segment(text, mode=AUTO)
    assert len(plan.units) == 1
    assert plan.units[0].line_count == 3


def test_hard_never_merges_across_lines():
    plan = segment("第一行没有句号\n第二行也没有", mode=HARD)
    assert len(plan.units) == 2


# ---------------------------------------------------------------- 不变量
@pytest.mark.parametrize("mode", [SOFT, HARD, AUTO])
@pytest.mark.parametrize(
    "text",
    [PROSE, LOG, "a\n\n\nb", "第一段。\n\n第二段。\n\n第三段。", "no newline at all"],
)
def test_line_count_is_invariant(mode, text):
    """任何模式下，模型原样返回时行数必须与输入一致。"""
    plan = segment(text, mode=mode)
    out = plan.join(identity(plan))
    assert out.split("\n") == text.split("\n")
