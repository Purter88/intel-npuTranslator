"""编码归一（SPEC.md · CLI 管道契约）。

Windows 控制台的两个坑，实测确认：

1. **stdout**：即使被管道接走，`sys.stdout.encoding` 仍是 cp936（gbk）。
   缅甸语 / 阿拉伯语 / 藏语写出来直接 `UnicodeEncodeError`。
2. **stdin**：同样按 cp936 解，喂 UTF-8 日文会 `UnicodeDecodeError`；
   更糟的是某些字节序列会被 cp936 **静默解成乱码**——比崩溃危险得多，
   因为用户拿到的是"看起来能跑"的错误译文。

对策：**所有 I/O 走字节，编码由程序全权决定**，不依赖 shell、locale 与重定向。

解码优先级（有物理证据的 > 用户强制 > 猜测）：

1. BOM（UTF-32/16/8，明确存在就无条件采信）
2. `--input-encoding` 指定的编码
3. UTF-8 严格
4. cp936（GBK）兜底
5. 全失败 → 抛 `DecodeError`，由 CLI 转成退出码 1（**绝不静默 replace**）
"""
from __future__ import annotations

import codecs
import errno
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = [
    "DecodeError",
    "InputTooLarge",
    "configure_stdio",
    "decode_bytes",
    "is_broken_pipe",
    "norm_newlines",
    "open_output",
    "read_bytes",
    "read_stdin_bytes",
    "silence_stdout",
    "write_stdout",
]

# BOM 表：(BOM 字节, 剥离 BOM 后用的 codec, 对外报告的编码名)
#
# 两个坑：
# - 顺序：UTF-32LE 的 BOM 以 UTF-16LE 的 BOM 开头，长的必须先判
# - codec 必须带字节序：裸 "utf-16" 会**自己再吃一次 BOM**，而我们已经把 BOM 剥掉了，
#   它会回退到本机字节序（x86 = LE），把 BE 文件解成乱码（实测 '안녕' -> '䣅喱'）
_BOMS: tuple[tuple[bytes, str, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le", "utf-32-le"),   # FF FE 00 00
    (codecs.BOM_UTF32_BE, "utf-32-be", "utf-32-be"),   # 00 00 FE FF
    (codecs.BOM_UTF8, "utf-8", "utf-8-sig"),           # EF BB BF
    (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"),   # FF FE
    (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"),   # FE FF
)

# 无 BOM 时的猜测顺序。cp936 放最后——它能解出几乎任何字节序列，
# 放在前面会把合法的 UTF-8 吃掉变成乱码。
_GUESS_CHAIN: tuple[str, ...] = ("utf-8", "cp936")

_CHUNK = 1 << 16

# Windows 关闭管道给 EINVAL(22)，POSIX 给 EPIPE(32)；EBADF(9) 兜底
_BROKEN_PIPE_ERRNOS = frozenset(
    e for e in (getattr(errno, "EPIPE", None), getattr(errno, "EINVAL", None),
                getattr(errno, "EBADF", None)) if e is not None
)


class DecodeError(ValueError):
    """所有候选编码都解不开。CLI 应转成退出码 1。"""

    def __init__(self, tried: tuple[str, ...], source: str) -> None:
        self.tried = tried
        self.source = source
        super().__init__(f"无法解码{source}，已尝试: {', '.join(tried)}；请用 --input-encoding 指定")


class InputTooLarge(ValueError):
    """输入超过体积上限。`--max-input-mb` 存在的原因：不能无脑 read() 到 OOM。"""

    def __init__(self, size: int, limit: int, source: str) -> None:
        self.size = size
        self.limit = limit
        self.source = source
        super().__init__(
            f"{source} 体积 {size / 1048576:.2f} MB 超过上限 {limit / 1048576:.2f} MB；"
            f"调大 --max-input-mb 或先切分"
        )


def norm_newlines(text: str) -> str:
    """CRLF / CR 归一为 LF。

    分段逻辑只认 `\\n`，不归一的话 Windows 文本文件的换行会被当成普通字符，
    换行三档直接失效。
    """
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def decode_bytes(data: bytes, forced: str | None = None, source: str = "输入") -> tuple[str, str]:
    """解码字节串。

    :param data: 原始字节
    :param forced: 用户用 `--input-encoding` 强制的编码，优先级仅低于 BOM
    :return: `(文本, 实际使用的编码名)`
    :raises DecodeError: 全部候选失败（绝不静默 replace，避免 mojibake）
    """
    if not data:
        return "", "utf-8"

    for bom, codec, label in _BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(codec), label

    tried: list[str] = []
    if forced:
        tried.append(forced)
        try:
            return data.decode(forced), forced
        except (UnicodeDecodeError, LookupError):
            pass
    else:
        for enc in _GUESS_CHAIN:
            tried.append(enc)
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                continue

    raise DecodeError(tuple(tried), source)


def read_stdin_bytes(max_mb: float = 64.0) -> bytes:
    """读 stdin 原始字节，带体积上限。

    必须走 `sys.stdin.buffer`：文本层已被 locale 绑成 gbk。
    """
    src = getattr(sys.stdin, "buffer", None)
    if src is None:  # 被替换成 StringIO 之类（测试场景）
        raw = sys.stdin.read()
        return raw.encode("utf-8") if isinstance(raw, str) else b""
    return _read_limited(src.read, max_mb, "stdin")


def read_bytes(path: str | Path, max_mb: float = 64.0) -> bytes:
    """读文件原始字节，先用 stat 快检再分块读（避免大文件一次性进内存）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"输入文件不存在: {p}")
    size = p.stat().st_size
    limit = int(max_mb * 1048576)
    if size > limit:
        raise InputTooLarge(size, limit, f"文件 {p.name}")
    with p.open("rb") as fh:
        return _read_limited(fh.read, max_mb, f"文件 {p.name}")


def _read_limited(reader, max_mb: float, source: str) -> bytes:
    limit = int(max_mb * 1048576)
    if limit <= 0:
        limit = 1
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise InputTooLarge(total, limit, source)
        chunks.append(chunk)
    return b"".join(chunks)


def configure_stdio() -> None:
    """把 stdout/stderr 切成 UTF-8 且不做换行转换。

    `newline="\\n"` 阻止 Windows 把 `\\n` 翻译成 `\\r\\n`——管道下游拿到 CRLF
    会污染 diff / 日志比对。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 被包装/替换的流没有 reconfigure
            pass


def is_broken_pipe(exc: BaseException) -> bool:
    """判断是不是"下游把管道关了"。

    ⚠️ 实测（Windows）：管道被关闭时抛的**不是** `BrokenPipeError` 而是
    `OSError: [Errno 22] Invalid argument`。只 catch `BrokenPipeError` 的话，
    `nputr ... | more` 会直接把 traceback 喷到 stderr，正是 `SPEC.md · CLI 管道契约` 要避免的。
    POSIX 上 EPIPE 会被 Python 映射成 `BrokenPipeError`，两者都覆盖。
    """
    if isinstance(exc, BrokenPipeError):
        return True
    return isinstance(exc, OSError) and exc.errno in _BROKEN_PIPE_ERRNOS


def write_stdout(text: str) -> None:
    """写 stdout 的**唯一**正确姿势：绕开文本层直接写 UTF-8 字节。

    管道被关闭时归一化成 `BrokenPipeError` 抛出，调用方只需处理一种异常。
    """
    buf = getattr(sys.stdout, "buffer", None)
    data = text.encode("utf-8")
    try:
        if buf is None:
            sys.stdout.write(text)
        else:
            buf.write(data)
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as exc:
        if not is_broken_pipe(exc):
            raise
        raise BrokenPipeError from exc


@contextmanager
def open_output(path: str | Path, append: bool = False, bom: bool = False) -> Iterator[object]:
    """程序自控的输出文件。

    - 默认 UTF-8 **无 BOM**；`--bom` 才写 BOM（记事本 / Excel 兼容）
    - 追加模式下**只在文件原本为空时**写 BOM：BOM 出现在文件中间会变成 `\\ufeff` 字符
    - 父目录不存在时自动创建
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)

    need_bom = bom and (not append or not p.exists() or p.stat().st_size == 0)
    # newline="" —— 不让平台把 \n 换成 \r\n，输出字节完全由我们决定
    with p.open("ab" if append else "wb") as fh:
        if need_bom:
            fh.write(codecs.BOM_UTF8)
        yield fh


def write_output(fh, text: str) -> None:  # noqa: ANN001 - 二进制文件对象
    fh.write(text.encode("utf-8"))


def silence_stdout() -> None:
    """BrokenPipe 后把 stdout 指向 devnull。

    不这么做的话，解释器关停时会往 stderr 喷
    `Exception ignored in: <_io.TextIOWrapper name='<stdout>'>`，
    在 `| head` / `| more` 场景下吓用户一跳。
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:  # noqa: BLE001 - 兜底，静默失败不影响主流程
        pass
