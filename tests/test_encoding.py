"""编码层单测（SPEC.md · CLI 管道契约）。

覆盖：BOM 四种 + 无 BOM 猜测链 + 强制编码 + 失败路径 + 换行归一 + 体积上限 + 输出 BOM 语义。
不加载模型，秒级。
"""
from __future__ import annotations

import codecs

import pytest

from npu_translator.encoding import (
    DecodeError,
    InputTooLarge,
    decode_bytes,
    norm_newlines,
    open_output,
    read_bytes,
    write_output,
)


def test_bom_utf8():
    data = codecs.BOM_UTF8 + "こんにちは".encode("utf-8")
    text, enc = decode_bytes(data)
    assert text == "こんにちは"
    assert enc == "utf-8-sig"


def test_bom_utf16_le():
    data = codecs.BOM_UTF16_LE + "안녕".encode("utf-16-le")
    text, enc = decode_bytes(data)
    assert text == "안녕"
    assert enc == "utf-16-le"


def test_bom_utf16_be():
    """字节序必须与 BOM 一致：裸 'utf-16' codec 会按本机 LE 解，把 BE 文件读成乱码。"""
    data = codecs.BOM_UTF16_BE + "안녕".encode("utf-16-be")
    text, enc = decode_bytes(data)
    assert text == "안녕"
    assert enc == "utf-16-be"


def test_bom_utf32_le_not_misread_as_utf16():
    """UTF-32LE 的 BOM 以 UTF-16LE 的 BOM 开头，长的必须先判。"""
    data = codecs.BOM_UTF32_LE + "ABC".encode("utf-32-le")
    text, enc = decode_bytes(data)
    assert text == "ABC"
    assert enc == "utf-32-le"


def test_no_bom_utf8_wins():
    text, enc = decode_bytes("今天天气不错".encode("utf-8"))
    assert text == "今天天气不错"
    assert enc == "utf-8"


def test_no_bom_falls_back_to_cp936():
    text, enc = decode_bytes("今天天气不错".encode("gbk"))
    assert text == "今天天气不错"
    assert enc == "cp936"


def test_forced_encoding_beats_utf8_guess():
    """用户明确指定时不许偷偷回退到 utf-8，否则乱码会被当成正常结果。"""
    text, enc = decode_bytes("中文".encode("gbk"), forced="gbk")
    assert text == "中文"
    assert enc == "gbk"


def test_forced_encoding_failure_raises():
    """指定 utf-8 却解不开 → 必须报错，不能悄悄回退到 cp936 编出乱码。"""
    with pytest.raises(DecodeError):
        decode_bytes(b"\xff\xff\xff", forced="utf-8")


def test_undecodable_raises_instead_of_mojibake():
    """关键：绝不静默 replace。静默 mojibake 比崩溃危险（SPEC.md · CLI 管道契约）。"""
    with pytest.raises(DecodeError):
        decode_bytes(b"\x81\x20\xff\xfe\xfd")


def test_is_broken_pipe_covers_windows_einval():
    """Windows 关闭管道给的是 OSError(22)，不是 BrokenPipeError（SPEC.md · CLI 管道契约）。"""
    from npu_translator.encoding import is_broken_pipe

    assert is_broken_pipe(BrokenPipeError())
    assert is_broken_pipe(OSError(22, "Invalid argument"))   # Windows
    assert is_broken_pipe(OSError(32, "Broken pipe"))        # POSIX
    assert not is_broken_pipe(OSError(13, "Permission denied"))
    assert not is_broken_pipe(ValueError("boom"))


def test_write_stdout_normalizes_broken_pipe(monkeypatch):
    """无论底层抛 BrokenPipeError 还是 OSError(22)，调用方只需处理一种。"""
    import io

    import pytest

    from npu_translator import encoding

    class _DeadStdout:
        def __init__(self, err: Exception) -> None:
            self.buffer = io.BytesIO()
            self._err = err

        def write(self, _data) -> int:  # noqa: ANN001
            raise self._err

        def flush(self) -> None:
            raise self._err

    for exc in (BrokenPipeError(), OSError(22, "Invalid argument")):
        monkeypatch.setattr(encoding.sys, "stdout", _DeadStdout(exc))
        with pytest.raises(BrokenPipeError):
            encoding.write_stdout("你好")


def test_empty_input():
    assert decode_bytes(b"") == ("", "utf-8")


def test_norm_newlines():
    assert norm_newlines("a\r\nb\rc\n") == "a\nb\nc\n"
    assert norm_newlines("a\nb") == "a\nb"


def test_read_bytes_size_limit(tmp_path):
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * 2048)
    with pytest.raises(InputTooLarge):
        read_bytes(p, max_mb=0.001)
    assert len(read_bytes(p, max_mb=1)) == 2048


def test_read_bytes_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_bytes(tmp_path / "nope.txt")


def test_output_no_bom_by_default(tmp_path):
    p = tmp_path / "o.txt"
    with open_output(p) as fh:
        write_output(fh, "你好")
    assert p.read_bytes() == "你好".encode("utf-8")


def test_output_with_bom(tmp_path):
    p = tmp_path / "o.txt"
    with open_output(p, bom=True) as fh:
        write_output(fh, "你好")
    assert p.read_bytes().startswith(codecs.BOM_UTF8)


def test_append_never_writes_bom_in_middle(tmp_path):
    """BOM 出现在文件中间会变成一个 \\ufeff 字符——追加场景必须禁用。"""
    p = tmp_path / "o.txt"
    p.write_bytes("existing".encode("utf-8"))
    with open_output(p, append=True, bom=True) as fh:
        write_output(fh, " appended")
    assert p.read_bytes() == "existing appended".encode("utf-8")


def test_append_to_empty_file_can_have_bom(tmp_path):
    p = tmp_path / "o.txt"
    p.write_bytes(b"")
    with open_output(p, append=True, bom=True) as fh:
        write_output(fh, "你好")
    assert p.read_bytes().startswith(codecs.BOM_UTF8)


def test_output_creates_parent_dir(tmp_path):
    p = tmp_path / "sub" / "deep" / "o.txt"
    with open_output(p) as fh:
        write_output(fh, "x")
    assert p.exists()
