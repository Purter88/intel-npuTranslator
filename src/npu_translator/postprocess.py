"""译文后处理（SPEC.md · NPU 实现要点）。

greedy 解码 + 强约束 prompt 已经能挡掉绝大多数解释性输出，
这里只做**保守**清洗：去掉明确的前缀标记与包裹符号，绝不改写译文正文。
"""
from __future__ import annotations

import re

# 常见的「解释性前缀」，命中即剥离（含中英文冒号）
_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"^\s*" + p + r"[：:]\s*", re.IGNORECASE)
    for p in (
        r"以下是翻译(结果)?",
        r"翻译结果(如下)?",
        r"译文(如下)?",
        r"翻译(如下)?",
        r"here is the translation",
        r"here'?s the translation",
        r"the translation is",
        r"translation",
        r"sure[,.]?",
        r"certainly[,.]?",
    )
)

# 包裹用的引号对
_QUOTE_PAIRS = (
    ("“", "”"),
    ("‘", "’"),
    ('"', '"'),
    ("'", "'"),
    ("「", "」"),
    ("『", "』"),
)

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    """去掉 ```lang ... ``` 代码块围栏（模型偶尔会套一层）。"""
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def strip_wrapping_quotes(text: str) -> str:
    """去掉成对包裹的引号；非成对则不动（避免误删正文里的引号）。"""
    for left, right in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            inner = text[len(left): -len(right)]
            # 内部不应再出现同类右引号，否则可能是正文引用而非包裹
            if right not in inner:
                return inner.strip()
    return text


def strip_prefix(text: str) -> str:
    """剥离解释性前缀，只剥一次（避免反复循环误伤）。"""
    for pat in _PREFIX_PATTERNS:
        m = pat.match(text)
        if m:
            return text[m.end():].strip()
    return text


def clean(text: str, strip_quotes: bool = True) -> str:
    """完整清洗流水线。

    :param text: 模型原始输出
    :param strip_quotes: 是否去掉成对包裹引号
    """
    if not text:
        return ""
    out = text.strip()
    out = strip_fence(out)
    out = strip_prefix(out)
    if strip_quotes:
        out = strip_wrapping_quotes(out)
    # 归一：去掉尾部多余空行，保留段内换行
    return out.strip()
