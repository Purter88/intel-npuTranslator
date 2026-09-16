"""`docs/SPEC.md` 引用护栏（T04-c）。

## 为什么需要这个文件

代码注释里的规格引用统一写成 `SPEC.md · <章名>`。这样写之后，唯一的失效方式是
**章名漂移**：SPEC.md 改了标题，注释里的引用悄悄变成死链，而**没有任何东西会红**。
本文件就是把"漂移"变成红灯。

## 两条断言

1. 所有 `SPEC.md · X` 的 X 必须是 SPEC.md 的**一级或二级标题原文**
   （允许 `父章节 · 子章节` 两段式，第二段取三级标题，**不参与**校验 —— 见 SPEC.md §0.1）。
2. SPEC.md 的章节清单做**快照**：改标题必须显式回来改快照。

## 解析上的两个坑（本机实测踩过）

- 🔴 **全角括号**：`WebUI（nputweb）` 里有一对全角括号，而引用常常整个被 `（…）` 包着
  （`…（SPEC.md · WebUI（nputweb））`）。朴素正则会把章名截成 `WebUI（nputweb`，
  或者把外层那个 `）` 一起吞进来。所以章名解析用**带括号配平**的扫描，而不是纯正则。
- 🔴 **围栏代码块**：SPEC.md 里有以 `# ` 开头的 shell 示例行，朴素正则会把它们当一级标题。
  所以标题提取必须跳过 ``` / ~~~ 围栏之间的内容 —— 现在围栏里恰好没有 `## `，
  但护栏要挡的是**将来**的写法。

## 不扫什么，以及为什么

- `docs/SPEC.md` 自身：§0.1 的对照表里有两行**故意写错**的示例（❌ 那两行），扫它必然自伤。
- 本文件自身：为了说清"什么算合法的引用形态"，示例里会原样写出各种写法。
- `.gitignore`：它的注释讲的是"为什么忽略某路径"，提到被忽略的目录很正常。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------- 路径与范围
ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "SPEC.md"

#: 参与扫描的目录（相对仓库根）
SCAN_DIRS: tuple[str, ...] = ("src", "tests", "scripts", "docs")

#: 参与扫描的根目录文本文件
SCAN_ROOT_FILES: tuple[str, ...] = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-export.txt",
)

#: 参与扫描的扩展名。只挑**会入库的**文本文件，二进制/产物一律不看。
SCAN_SUFFIXES: frozenset[str] = frozenset({".py", ".md", ".toml", ".txt", ".cfg", ".ini"})

#: 跳过：目录名 / 文件名。理由见模块 docstring「不扫什么，以及为什么」。
SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", ".workbuddy",
})
SKIP_FILE_NAMES: frozenset[str] = frozenset({
    "SPEC.md",                  # §0.1 的对照表有两行故意写错的 ❌ 示例
    "test_spec_references.py",  # 本文件：示例里会原样写出各种引用写法
})

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,2})\s+(.*\S)\s*$")
#: 标题前面的 `N.` / `N.N` 序号**不属于**章名（SPEC.md §0.1 第 2 条）。
ORDINAL_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s*")

#: 触发一次引用扫描的锚点。`SPEC.md · ` 中间的空格允许写成全角或不写。
SPEC_REF_RE = re.compile(r"SPEC\.md\s*·")

#: 章名的终止符：遇到就认为章名结束（括号另走配平逻辑）。
#: 🔴 `·` **不能**当终止符：`父章节 · 子章节` 是合法的两段式写法，得整段扫进来再拆。
#: 🔴 反引号必须当终止符：它是 markdown 行内代码的边界，且能顺手把
#:    「章名后面还跟着一句解释」这种写法挡成红灯 —— 那正是 SPEC.md §0.1 禁止的。
_NAME_STOP_CHARS = "`"

#: 章名尾部允许出现的标点 —— 它们永远不会是标题的一部分，却常常紧跟在引用后面。
#: 🔴 这里**不含** `）`：右括号由扫描器的配平逻辑处理，交给 rstrip 会把
#:    `WebUI（nputweb）` 的尾括号一起吃掉（本机实测踩过）。
_NAME_TRAILING_CHARS = "。，、；：,.!?\"'“”‘’"


# ---------------------------------------------------------------- 断言 2 的快照
#: SPEC.md 的一级标题。改标题 → 这里必须显式改，让"改标题"变成一个**有意识**的动作。
EXPECTED_H1: tuple[str, ...] = (
    "SPEC.md — Intel NPU 本地多语言翻译程序 · 技术契约",
)

#: SPEC.md 的二级标题（去掉 `N.` 序号后的章名）—— 引用里能填的合法取值就是这些。
#: 顺序即文档顺序；新增 / 改名 / 删章都必须同步改这里。
EXPECTED_H2: tuple[str, ...] = (
    "地位与稳定性契约",
    "目标与非目标",
    "环境与版本",
    "架构与目录结构",
    "CLI 管道契约",
    "Prompt 与语言",
    "模型",
    "NPU 实现要点",
    "实测基线",
    "WebUI（nputweb）",
    "服务（nputserve）/v1 API 契约",
    "服务：并发与超时模型",
    "Git 约定：本机绝对路径禁止入库",
    "执行约定",
    "踩坑记录",
    "已知问题与活跃风险",
    "回归验收清单",
    "跨平台（Linux / ARM）可行性评审结论",
)


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class Heading:
    """SPEC.md 里的一个标题。"""

    level: int          # 1 = `# `，2 = `## `
    title: str          # 去掉 `N.` 序号后的章名
    lineno: int


@dataclass(frozen=True)
class Reference:
    """代码里出现的一处 `SPEC.md · X` 引用。"""

    rel: str            # 相对仓库根的路径
    lineno: int
    raw: str            # 解析出来的章名（可能带 `父 · 子` 两段）


@dataclass(frozen=True)
class HeadingIndex:
    """SPEC.md 的标题表，按层级分好。"""

    h1: tuple[Heading, ...]
    h2: tuple[Heading, ...]

    @property
    def valid_names(self) -> frozenset[str]:
        """一级 + 二级标题的章名集合 —— 引用的第一段必须落在这个集合里。"""
        return frozenset(h.title for h in (*self.h1, *self.h2))


# ---------------------------------------------------------------- 解析
def strip_fenced_blocks(lines: list[str]) -> list[tuple[int, str]]:
    """丢掉围栏代码块（``` / ~~~）之间的行，返回 (行号, 行内容)。

    行号保留原始值：报错信息要能指回 SPEC.md 的真实行。
    """
    kept: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for lineno, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)[0]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            continue
        if not in_fence:
            kept.append((lineno, line))
    return kept


def parse_headings(text: str) -> HeadingIndex:
    """提取 SPEC.md 的一级 / 二级标题（跳过围栏、去掉序号前缀）。"""
    h1: list[Heading] = []
    h2: list[Heading] = []
    for lineno, line in strip_fenced_blocks(text.splitlines()):
        match = HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = ORDINAL_RE.sub("", match.group(2)).strip()
        heading = Heading(level=level, title=title, lineno=lineno)
        if level == 1:
            h1.append(heading)
        else:
            h2.append(heading)
    return HeadingIndex(h1=tuple(h1), h2=tuple(h2))


def _scan_chapter_name(text: str, start: int) -> str:
    """从 `SPEC.md · ` 之后开始扫章名，返回扫描到的原始串。

    终止条件（见模块 docstring「解析上的两个坑」）：
    - `·` / 反引号 → 立刻停（前者是两段式的分隔符，后者是 markdown 行内代码边界）；
    - 全角 / 半角括号 → **配平**着吃：深度归零后再遇到右括号才停。
      `WebUI（nputweb）` 这种章名自带一对，外层再包一对也不能误吞。
    """
    depth = 0
    end = len(text)
    for index in range(start, len(text)):
        char = text[index]
        if char in _NAME_STOP_CHARS and depth == 0:
            end = index
            break
        if char in "（(":
            depth += 1
        elif char in "）)":
            if depth == 0:
                end = index          # 不属于章名的右括号 → 引用到此结束
                break
            depth -= 1
    return text[start:end]


def parse_reference(line: str, start: int) -> str:
    """解析一处引用的章名：从 `SPEC.md ·` 之后扫到终止符，再清掉尾部标点。"""
    raw = _scan_chapter_name(line, start)
    return raw.strip().rstrip(_NAME_TRAILING_CHARS).strip()


def find_references(text: str, rel: str) -> list[Reference]:
    """找出一个文件里所有 `SPEC.md · X` 引用（按行、按出现顺序）。"""
    refs: list[Reference] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in SPEC_REF_RE.finditer(line):
            name = parse_reference(line, match.end())
            refs.append(Reference(rel=rel, lineno=lineno, raw=name))
    return refs


# ---------------------------------------------------------------- 扫描范围
def iter_scanned_files() -> list[Path]:
    """收集待扫文件：源码目录里的文本 + 指定的根目录文本文件。"""
    found: list[Path] = []
    for dirname in SCAN_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            parts = path.relative_to(ROOT).parts
            if any(part in SKIP_DIR_NAMES for part in parts):
                continue
            if path.name in SKIP_FILE_NAMES:
                continue
            found.append(path)
    for name in SCAN_ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            found.append(path)
    return found


def read_text(path: Path) -> str:
    """读文本文件。二进制或非 UTF-8 一律当空串 —— 那种文件里不会有注释。"""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


# ---------------------------------------------------------------- 夹具
@pytest.fixture(scope="module")
def index() -> HeadingIndex:
    """SPEC.md 的标题表（整个模块只读一次盘）。"""
    text = read_text(SPEC_PATH)
    if not text:
        pytest.fail(f"读不到 SPEC.md：{SPEC_PATH}")
    return parse_headings(text)


@pytest.fixture(scope="module")
def scanned() -> list[tuple[str, str]]:
    """待扫文件的 (相对路径, 内容) 列表。"""
    return [(str(p.relative_to(ROOT)), read_text(p)) for p in iter_scanned_files()]


# ---------------------------------------------------------------- 断言 1
def test_all_spec_references_point_to_real_headings(index, scanned):
    """每处 `SPEC.md · X` 的第一段都必须是 SPEC.md 的一级或二级标题原文。"""
    valid = index.valid_names
    bad: list[str] = []
    total = 0
    for rel, text in scanned:
        for ref in find_references(text, rel):
            total += 1
            first_segment = ref.raw.split("·")[0].strip()
            if first_segment in valid:
                continue
            # 最常见的失效方式是「章名后面追加了说明文字」—— 顺手把它指出来，省一轮排查
            hint = ""
            for name in sorted(valid, key=len, reverse=True):
                if first_segment.startswith(name):
                    hint = f"（疑似章名「{name}」后面追加了 {first_segment[len(name):]!r}）"
                    break
            bad.append(f"{ref.rel}:{ref.lineno} → 「{ref.raw}」{hint}")
    assert total > 0, "一处 SPEC.md 引用都没扫到 —— 扫描范围或锚点正则写错了"
    assert not bad, (
        f"{len(bad)}/{total} 处 SPEC.md 引用指向了不存在的章节"
        "（章名后面追加了说明文字，或标题已改名）：\n  " + "\n  ".join(bad)
    )


def test_fullwidth_parentheses_survive_parsing():
    """`WebUI（nputweb）` 这类带全角括号的章名必须被**完整**解析出来。

    这条守的是解析器本身：括号配平写错的话，断言 1 会因为解析出 `WebUI（nputweb`
    而全线飘红 —— 那种红是误报，会让人想去"修"注释而不是修解析器。
    """
    line = "（见 SPEC.md · WebUI（nputweb））。"
    match = SPEC_REF_RE.search(line)
    assert match is not None
    assert parse_reference(line, match.end()) == "WebUI（nputweb）"


def test_two_segment_reference_keeps_parent_and_child():
    """`父章节 · 子章节` 两段式要能解析；子章节（三级标题）只作精度，不参与校验。"""
    line = "（SPEC.md · 已知问题与活跃风险 · 跨机器默认值不可外推）"
    match = SPEC_REF_RE.search(line)
    assert match is not None
    assert parse_reference(line, match.end()) == "已知问题与活跃风险 · 跨机器默认值不可外推"


def test_backtick_terminates_the_reference():
    """反引号是 markdown 行内代码边界，必须终止章名扫描。"""
    line = "理由见 `SPEC.md · 架构与目录结构` —— OpenVINO 初始化有百毫秒级开销"
    match = SPEC_REF_RE.search(line)
    assert match is not None
    assert parse_reference(line, match.end()) == "架构与目录结构"


# ---------------------------------------------------------------- 断言 2
def test_spec_h1_snapshot(index):
    """SPEC.md 的一级标题快照 —— 改标题必须回来改这里。"""
    actual = tuple(h.title for h in index.h1)
    assert actual == EXPECTED_H1, (
        "SPEC.md 的一级标题变了。确属有意改名 → 同步更新 "
        "tests/test_spec_references.py::EXPECTED_H1，并检查引用是否跟着改。\n"
        f"  现在: {actual}\n  快照: {EXPECTED_H1}"
    )


def test_spec_h2_snapshot(index):
    """SPEC.md 的二级标题（章名）快照 —— 引用能填的合法取值就是这些。"""
    actual = tuple(h.title for h in index.h2)
    assert actual == EXPECTED_H2, (
        "SPEC.md 的章节清单变了。确属有意改名 → 同步更新 "
        "tests/test_spec_references.py::EXPECTED_H2，并改掉所有旧引用。\n"
        f"  现在: {actual}\n  快照: {EXPECTED_H2}"
    )


def test_heading_parser_ignores_fenced_code_blocks():
    """围栏代码块里以 `# ` 开头的 shell 示例行，不能被当成一级标题。

    现在 SPEC.md 的围栏里恰好没有 `## `，本条守的是**将来**的写法：
    有人在示例里写 `## 安装` 的那天，快照断言不该莫名其妙地红。
    """
    text = (
        "# 真标题\n"
        "\n"
        "```sh\n"
        "# 这是注释，不是标题\n"
        "## 这也是 shell 示例里的行\n"
        "```\n"
        "\n"
        "## 二级标题\n"
    )
    parsed = parse_headings(text)
    assert [h.title for h in parsed.h1] == ["真标题"]
    assert [h.title for h in parsed.h2] == ["二级标题"]


def test_ordinal_prefix_is_not_part_of_chapter_name():
    """`15. 已知问题与活跃风险` 的章名是后半截 —— `N.` 序号不属于章名。"""
    parsed = parse_headings("# 标题\n\n## 15. 已知问题与活跃风险\n")
    assert [h.title for h in parsed.h2] == ["已知问题与活跃风险"]
