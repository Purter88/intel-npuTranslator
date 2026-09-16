"""长文本分段与换行保真（SPEC.md · NPU 实现要点 · 长文本与换行三档）。

NPU 的 KV cache 只有 768（512+256），长文本**必须**先切段再逐段翻译，否则被静默截断。

## 为什么重写（R12）

旧实现把单个 `\\n` 当成"既不是硬边界、也不保证保留"的东西，实测三种输入：

| 输入 | 原行数 | 旧切出段数 | 旧 join 后行数 |
|---|---|---|---|
| 硬折行散文 | 6 | 2 | **4**（换行被 `strip` / `" ".join` 吃掉） |
| 日志 4 行 | 4 | **1** | 4（靠模型碰巧保留，**无保证**） |
| Markdown 列表 | 4 | **1** | 4（同上） |

根因两个：`split_sentences` 的 `strip()` 吃掉段落边界换行；`join()` 用 `" ".join(buf)`
把段与段用空格连（中文还会被插入多余空格）。

## 三档语义（SPEC.md · CLI 管道契约）

| 模式 | 单元粒度 | 单元内是否含 `\\n` | 行数保真 | 适用 |
|---|---|---|---|---|
| `hard` | 1 行 = 1 单元（超长行才硬切） | 否（打包模式除外） | **结构性保证** | 日志 / 列表 / CSV / 字幕 |
| `soft`（默认） | 段落块内贪心合并 | **是** | 依赖模型保留换行 | 散文 / 普通文档 |
| `auto` | 启发式判定硬边界后合并 | **是** | 依赖模型保留换行 | 混排文档（**会判断错**） |

`hard` 会把硬折行的散文从中间劈开（语法破碎、指代丢失）；
`soft` / `auto` 不会劈开，但换行的保留依赖模型，`join()` 检测到行数不匹配会降级
（整段落在起始行，**内容不丢、行数会掉**），并把不匹配的单元序号报给调用方决定是否重翻。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config as cfg
from .encoding import norm_newlines

__all__ = [
    "AUTO",
    "HARD",
    "NEWLINE_MODES",
    "SOFT",
    "Plan",
    "Unit",
    "segment",
    "split_sentences",
]

SOFT = "soft"
HARD = "hard"
AUTO = "auto"
NEWLINE_MODES = (SOFT, HARD, AUTO)

# 句末标点（后面可跟引号/括号/空白）
_SENT_END_RE = re.compile(r"(?<=[。！？!?；;])(?=[”’\"'）)\]\s]*|$)|(?<=[.!?])(?=[\s])")

# 兜底硬切的次级断点
_SOFT_BREAK_RE = re.compile(r"(?<=[，,、])|(?<=\s)")

# 判为硬边界的"下行开头"标记（auto 模式）
_LIST_MARKERS = tuple("-#*>+")

# CJK 判定区间（用于决定是否补空格，避免中文之间被插入空格）
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3040 <= o <= 0x30FF      # 日文假名
        or 0x3400 <= o <= 0x4DBF   # CJK 扩展 A
        or 0x4E00 <= o <= 0x9FFF   # CJK 基本
        or 0xF900 <= o <= 0xFAFF   # 兼容汉字
        or 0xAC00 <= o <= 0xD7AF   # 韩文音节
        or 0x0600 <= o <= 0x06FF   # 阿拉伯文
        or 0x0E00 <= o <= 0x0E7F   # 泰文
        or 0x1000 <= o <= 0x109F   # 缅甸文
        or 0x0F00 <= o <= 0x0FFF   # 藏文
    )


@dataclass
class Unit:
    """一个翻译单元 = 一次模型请求的载荷。

    :param text: 送模型的文本。soft / auto 模式下**内部保留 `\\n`**
    :param index: 全局序号，有序重组的依据
    :param line_start / line_end: 覆盖的输入行区间（含两端，0 起）
    :param sources: 覆盖的**原始行**文本。hard 打包模式下用于校验失败后逐行重翻
    :param packed: hard 打包快路径标记（一次请求出多行）
    """

    text: str
    index: int
    line_start: int
    line_end: int
    sources: tuple[str, ...] = ()
    packed: bool = False

    @property
    def line_count(self) -> int:
        return self.line_end - self.line_start + 1


@dataclass
class Plan:
    """分段计划：行结构 + 翻译单元列表。

    `join()` 一律**按行重建**，因此行数保真不依赖模型是否听话。
    """

    mode: str
    lines: list[str]
    units: list[Unit] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        return [u.text for u in self.units]

    def __len__(self) -> int:
        return len(self.units)

    def validate(self, translations: list[str]) -> list[int]:
        """返回**输出行数 != 输入行数**的单元序号（空列表 = 全部对齐）。

        翻译层据此决定是否把该批退回逐行重翻（SPEC.md · CLI 管道契约）。
        """
        bad: list[int] = []
        for unit, out in zip(self.units, translations):
            if unit.line_count > 1 and _split_output(out, unit.line_count) is None:
                bad.append(unit.index)
        return bad

    def join(self, translations: list[str]) -> str:
        """把译文按原行结构拼回全文。

        - 空行**原样透传**，不送模型也不改写
        - 行内多片段（超长行被硬切）用智能分隔符拼接，**中文之间不会插空格**
        - 跨行单元的译文行数对不上时：整段落在起始行（内容不丢，行数会掉）
        """
        if len(translations) != len(self.units):
            raise ValueError(f"译文数量 {len(translations)} 与单元数量 {len(self.units)} 不一致")

        out: list[str] = [""] * len(self.lines)
        for unit, trans in zip(self.units, translations):
            parts = _split_output(trans, unit.line_count)
            if parts is None:
                parts = [trans] + [""] * (unit.line_count - 1)
            for offset, part in enumerate(parts):
                i = unit.line_start + offset
                # 同一行可能被切成多个单元（超长行硬切），这里拼接而不是覆盖
                out[i] = _glue(out[i], part) if out[i] else part

        # 空行原样透传（硬折行、段落间距、缩进全都保住）
        for i, line in enumerate(self.lines):
            if not line.strip():
                out[i] = line

        return "\n".join(out)

    def join_lines(self, translations: list[str]) -> list[str]:
        """按行归位后的译文行列表（测试与逐行校验用）。"""
        return self.join(translations).split("\n")


def _split_output(text: str, expect: int) -> list[str] | None:
    """把模型输出切成 expect 行；对不上返回 None。

    容忍模型多吐的首尾空行（常见的多余换行），只在**非空行数**对得上时才采信。
    """
    if expect <= 1:
        stripped = text.strip()
        return None if "\n" in stripped else [stripped]
    parts = text.split("\n")
    while parts and not parts[0].strip():
        parts.pop(0)
    while parts and not parts[-1].strip():
        parts.pop()
    return parts if len(parts) == expect else None


def _need_space(left: str, right: str) -> bool:
    """两片段之间是否要补空格。

    修掉旧 `join()` 的 bug：`" ".join()` 会给中文插空格。
    规则：只有两端都是 ASCII 词字符（字母/数字）时才补。
    """
    if not left or not right:
        return False
    l, r = left[-1], right[0]
    if _is_cjk(l) or _is_cjk(r):
        return False
    if not (l.isascii() and r.isascii()):
        return False
    return l.isalnum() and r.isalnum()


def split_sentences(text: str) -> list[str]:
    """把一段文本切成句子（保留标点，且**不再 strip 掉内部换行**）。"""
    parts = _SENT_END_RE.split(text)
    sentences = [s.strip() for s in parts if s and s.strip()]
    return sentences if sentences else ([text.strip()] if text.strip() else [])


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    """单句仍超长时：先按逗号/空格切，再对仍超长的块按固定长度切。

    最后一步必须在：中文长句可能既无逗号也无空格（如无标点的一整段），
    只靠软断点切不开，会让片段超出 NPU 的 KV cache 窗口而被静默截断。
    """
    chunks: list[str] = []
    buf = ""
    for piece in _SOFT_BREAK_RE.split(sentence):
        piece = piece or ""
        if not buf:
            buf = piece
            continue
        if len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            if buf.strip():
                chunks.append(buf.strip())
            buf = piece
    if buf.strip():
        chunks.append(buf.strip())

    fixed: list[str] = []
    for chunk in chunks:
        while len(chunk) > max_chars:
            fixed.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        if chunk:
            fixed.append(chunk)
    return [c for c in fixed if c]


def _split_long_line(line: str, max_chars: int) -> list[str]:
    """单行超长时的切分（hard 模式用）：按句切，句仍超长再硬切。"""
    pieces: list[str] = []
    for s in split_sentences(line):
        if len(s) > max_chars:
            pieces.extend(_hard_split(s, max_chars))
        else:
            pieces.append(s)
    return pieces or ([line] if line.strip() else [])


def _is_hard_boundary(prev: str, nxt: str) -> bool:
    """auto 模式的启发式：判断 prev 与 nxt 之间的换行是不是硬边界。

    - 上行以句末标点结尾 → 一句话说完了，判硬边界
    - 下行以列表/标题/编号标记开头 → 判硬边界
    - 否则视为被硬折行的散文，合并翻译

    ⚠️ 这是启发式，**会判断错**（英文缩写 "e.g." 结尾会误判为硬边界）。
    """
    p = prev.rstrip()
    n = nxt.lstrip()
    if not p or not n:
        return True
    if p[-1] in "。！？!?；;…":
        return True
    if n[0] in _LIST_MARKERS or n[0].isdigit():
        return True
    return False


def _blocks(lines: list[str], mode: str) -> list[tuple[int, int]]:
    """把行列表切成"可合并块"，返回 (start, end) 行区间列表。空行不属于任何块。"""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    prev_idx = -1
    for i, line in enumerate(lines):
        if not line.strip():
            if start is not None:
                blocks.append((start, i - 1))
                start = None
            prev_idx = i
            continue
        if start is None:
            start = i
        elif mode == HARD:
            blocks.append((start, i - 1))
            start = i
        elif mode == AUTO and _is_hard_boundary(lines[prev_idx], line):
            blocks.append((start, i - 1))
            start = i
        prev_idx = i
    if start is not None:
        blocks.append((start, len(lines) - 1))
    return blocks


def segment(
    text: str,
    max_chars: int | None = None,
    mode: str = SOFT,
    pack: bool = False,
) -> Plan:
    """把长文本切成可安全送入 NPU 的单元，并保留行结构。

    :param text: 原文
    :param max_chars: 单单元字符上限，默认 `config.SEGMENT_MAX_CHARS`（512）
    :param mode: `soft` | `hard` | `auto`
    :param pack: hard 模式专用快路径——把连续短行打包成一个请求（总长 ≤ max_chars），
                 一次请求出多行，省掉每行一次 NPU TTFT（0.59 s）。
                 译文行数对不上时由调用方按 `Unit.sources` 退回逐行重翻。
    :return: Plan（含行结构与单元列表）
    """
    max_chars = max_chars or cfg.SEGMENT_MAX_CHARS
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    if mode not in NEWLINE_MODES:
        raise ValueError(f"mode 必须是 {NEWLINE_MODES} 之一，收到 {mode!r}")

    lines = norm_newlines(text).split("\n")
    if pack and mode != HARD:
        pack = False  # 打包只对逐行语义有意义

    units: list[Unit] = []
    idx = 0

    # 打包要跨行，所以块划分必须按"连续非空行"来，不能被 hard 的逐行语义切碎
    block_mode = SOFT if pack else mode
    for start, end in _blocks(lines, block_mode):
        block_lines = lines[start : end + 1]

        if mode == HARD:
            if pack:
                idx = _pack_lines(block_lines, start, max_chars, units, idx)
            else:
                for offset, line in enumerate(block_lines):
                    for piece in _split_long_line(line, max_chars):
                        units.append(
                            Unit(
                                text=piece,
                                index=idx,
                                line_start=start + offset,
                                line_end=start + offset,
                                sources=(line,),
                            )
                        )
                        idx += 1
            continue

        # soft / auto：块内按句切分后贪心合并，合并时保留块内换行
        sources = tuple(block_lines)
        for piece_start, piece_end, piece in _merge_block(block_lines, start, max_chars):
            units.append(
                Unit(
                    text=piece,
                    index=idx,
                    line_start=piece_start,
                    line_end=piece_end,
                    sources=sources[piece_start - start : piece_end - start + 1],
                )
            )
            idx += 1

    return Plan(mode=mode, lines=lines, units=units)


def _pack_lines(
    block_lines: list[str],
    block_start: int,
    max_chars: int,
    units: list[Unit],
    idx: int,
) -> int:
    """hard 打包快路径：把总长 ≤ max_chars 的连续行打成一个请求。

    行数靠 `Unit.sources` 记录，译文行数对不上时调用方退回逐行重翻（SPEC.md · CLI 管道契约）。
    """
    batch: list[str] = []
    batch_start = 0

    def flush(b: list[str], b_start: int) -> None:
        nonlocal idx
        if not b:
            return
        if len(b) == 1:
            for piece in _split_long_line(b[0], max_chars):
                units.append(Unit(text=piece, index=idx, line_start=block_start + b_start,
                                  line_end=block_start + b_start, sources=(b[0],)))
                idx += 1
            return
        units.append(
            Unit(
                text="\n".join(b),
                index=idx,
                line_start=block_start + b_start,
                line_end=block_start + b_start + len(b) - 1,
                sources=tuple(b),
                packed=True,
            )
        )
        idx += 1

    for offset, line in enumerate(block_lines):
        too_long = len(line) > max_chars
        if batch and (too_long or sum(len(x) + 1 for x in batch) - 1 + len(line) > max_chars):
            flush(batch, batch_start)
            batch, batch_start = [], offset
        if too_long:
            # 超长行不进打包，按句/硬切拆开，但仍占它自己的行
            for piece in _split_long_line(line, max_chars):
                units.append(Unit(text=piece, index=idx, line_start=block_start + offset,
                                  line_end=block_start + offset, sources=(line,)))
                idx += 1
            batch, batch_start = [], offset + 1
            continue
        if not batch:
            batch_start = offset
        batch.append(line)
    flush(batch, batch_start)
    return idx


def _merge_block(
    block_lines: list[str],
    block_start: int,
    max_chars: int,
) -> list[tuple[int, int, str]]:
    """块内贪心合并，返回 [(行起, 行止, 文本)]，文本**保留块内换行**。"""
    # 逐行切成句子，记下每句所属行号
    items: list[tuple[int, str]] = []
    for offset, line in enumerate(block_lines):
        for s in split_sentences(line):
            if len(s) > max_chars:
                for piece in _hard_split(s, max_chars):
                    items.append((offset, piece))
            else:
                items.append((offset, s))

    if not items:
        whole = "\n".join(block_lines)
        if len(whole) <= max_chars:
            return [(block_start, block_start + len(block_lines) - 1, whole)]
        # 无句末标点且整体超长：退化成逐行，至少不会超窗被截断
        return [(block_start + i, block_start + i, line) for i, line in enumerate(block_lines)]

    out: list[tuple[int, int, str]] = []
    cur_start, cur_end, buf = items[0][0], items[0][0], items[0][1]
    for offset, s in items[1:]:
        candidate = f"{buf}\n{s}" if offset > cur_end else (_glue(buf, s))
        if len(candidate) <= max_chars:
            buf = candidate
            cur_end = max(cur_end, offset)
        else:
            out.append((block_start + cur_start, block_start + cur_end, buf))
            cur_start, cur_end, buf = offset, offset, s
    out.append((block_start + cur_start, block_start + cur_end, buf))
    return out


def _glue(left: str, right: str) -> str:
    """同一行内两片段的连接：按需补空格。"""
    return f"{left} {right}" if _need_space(left, right) else f"{left}{right}"
