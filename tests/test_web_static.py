"""nputweb 前端的只读文本断言（SPEC.md · WebUI（nputweb））。

## 为什么只做文本断言，不上 playwright

前端是原生三件套：**没有 package.json、没有 node_modules、没有构建步骤**。
为了几行断言引入一整套 JS 工具链，会让「秒级单测」这个前提直接破产 ——
而套件跑得慢的后果是没人愿意跑它，等于没有测试（SPEC.md · WebUI（nputweb））。

所以这里守的是**不需要浏览器就能验证、却又最容易悄悄退化**的三类不变量：

1. **心跳节奏** —— 退化了就是本轮 P0 复发（心跳吃光限流配额 → 翻译 429）。
2. **combobox 的无障碍属性** —— 必须静态写在 HTML 里，JS 运行时补的不算数。
3. **CSP 兼容性** —— CSP 是 `default-src 'self'` 且**没有** `'unsafe-inline'`，
   内联 `style=` / `on*=` 事件 / `<style>` 块在真实浏览器里会被直接拦掉，
   而本地起服务肉眼看又是好的 —— 这类问题只能在提交前靠断言拦。

断言一律用正则抓值、不写死行号：前端会重排，行号一变就挂的断言是噪音不是保护。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "npu_translator" / "web" / "static"
)
_APP_JS = _STATIC_DIR / "app.js"
_INDEX_HTML = _STATIC_DIR / "index.html"

pytestmark = pytest.mark.skipif(
    not _STATIC_DIR.is_dir(), reason="前端静态目录缺失，无法做文本断言"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _health_idle_ms() -> int:
    """从 `app.js` 里抓出空闲心跳间隔。

    刻意断言「存在且足够慢」而不是「等于某个数」—— 将来把 10 s 微调成 8 s 或 12 s
    是合理的，只要没退回秒级就不该亮红灯。
    """
    src = _read(_APP_JS)
    match = re.search(r"^const\s+HEALTH_IDLE_MS\s*=\s*(\d+)\s*;", src, re.MULTILINE)
    assert match, "app.js 里找不到 HEALTH_IDLE_MS 常量 —— 心跳节奏失去约束了？"
    return int(match.group(1))


# ---------------------------------------------------------------- 心跳节奏
def test_idle_health_poll_never_saturates_default_quota():
    """★ 本轮 P0 的回归闸门。

    原 bug：心跳 1.5 s（40 次/分）和翻译抢同一个 30/min 的桶 → 页面开着 45 s 后翻译必挂。
    空闲间隔必须慢到「心跳自己吃不光配额」：10 s = 6 次/分钟，只占默认配额的 1/5。
    """
    from npu_translator.web import DEFAULT_RATE_PER_MIN

    idle_ms = _health_idle_ms()
    per_min = 60_000 / idle_ms
    assert per_min <= DEFAULT_RATE_PER_MIN / 2, (
        f"空闲心跳 {idle_ms}ms = {per_min:.1f} 次/分，超过默认配额 "
        f"{DEFAULT_RATE_PER_MIN}/min 的一半 —— 会把翻译请求重新挤成 429"
    )


def test_idle_health_poll_is_not_sub_second_paced():
    """补一条绝对下限：防止有人为了「进度更跟手」把空闲心跳改回秒级。"""
    assert _health_idle_ms() >= 5_000


def test_health_poll_self_reschedules_instead_of_fixed_interval():
    """必须用 `setTimeout` 自调度。

    `setInterval` 的节奏一旦定下就改不了，做不到「空闲 10 s / 忙碌 1.5 s / 失败退避」
    这三档切换，也没法在 `setBusy()` 翻转时立刻重排。
    """
    src = _read(_APP_JS)
    # 断言的是「有调用」而不是「出现这个词」—— 注释里本来就在解释为什么不用它
    assert not re.search(r"setInterval\s*\(", src), (
        "app.js 不该再调用 setInterval —— 固定节奏等于本轮 P0 复发"
    )
    assert re.search(r"scheduleHealth\s*\(", src), "缺少自调度入口"


def test_health_failure_does_not_paint_main_status_bar():
    """S3：health 失败只动徽标，连续失败够次数才允许写主状态栏。

    漏了这条会退回「每 1.5 s 把状态栏刷红一次」，把刚翻译成功的提示冲掉，
    用户以为翻译挂了于是反复刷新 → abort in-flight → 服务端刷 10054。
    """
    src = _read(_APP_JS)
    assert "unreachable" in src, "缺少设备徽标的不可达态 class"
    assert re.search(r"HEALTH_FAILS_BEFORE_STATUS\s*=\s*[1-9]", src), (
        "缺少「连续失败 N 次才提示」的门槛"
    )


# ---------------------------------------------------------------- combobox 无障碍
@pytest.mark.parametrize(
    "attr", ['role="combobox"', "aria-expanded", "aria-controls"]
)
def test_language_combobox_exposes_aria_attributes(attr):
    """这三个属性必须**静态写在 HTML 里**，不能靠 JS 运行时补。

    理由有二：验收是只读文本断言（没有浏览器可跑）；且 `aria-controls` 必须指向
    真实存在的 id —— 运行时才发现拼错了，等于没有无障碍。
    """
    assert attr in _read(_INDEX_HTML)


def test_aria_controls_points_at_real_element_ids():
    """`aria-controls` 指向的 id 必须真的存在，否则读屏软件报的是空气。"""
    html = _read(_INDEX_HTML)
    known_ids = set(re.findall(r'id="([^"]+)"', html))
    targets = re.findall(r'aria-controls="([^"]+)"', html)
    assert targets, "没有任何 aria-controls，combobox 与候选列表没关联上"
    for target in targets:
        assert target in known_ids, f"aria-controls 指向了不存在的 id: {target}"


def test_language_select_remains_the_value_store():
    """`<select id="src-lang">` / `<select id="tgt-lang">` 必须还在。

    它是唯一的值存储：`runTranslate()` 与 `downloadOutput()` 都直接读它的 `.value`。
    哪天把它删了，这两处会静默拿到 `undefined`。
    """
    html = _read(_INDEX_HTML)
    for select_id in ("src-lang", "tgt-lang"):
        assert re.search(rf'id="{select_id}"', html), f"值存储 {select_id} 不见了"


# ---------------------------------------------------------------- CSP 兼容性
def test_index_has_no_inline_style():
    """CSP 的 `style-src 'self'` 没有 `'unsafe-inline'`，内联样式会被浏览器拦掉。"""
    html = _read(_INDEX_HTML)
    assert "style=" not in html, "出现内联 style 属性（会被 CSP 拦）"
    assert "<style" not in html, "出现 <style> 块（会被 CSP 拦）"


def test_index_has_no_inline_event_handler():
    """同上：`script-src 'self'` 没有 `'unsafe-inline'`，`on*=` 事件属性会被拦掉。"""
    html = _read(_INDEX_HTML)
    handlers = re.findall(r"\son[a-z]+\s*=", html)
    assert not handlers, f"出现内联事件属性（会被 CSP 拦）：{handlers}"
