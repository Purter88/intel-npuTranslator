"""CLI 层测试：退出码、硬退出、关停诊断（CLI 管道契约）。

⚠️ 这层以前是空的（106 项单测全在纯逻辑层，没有 CLI 测试），
   所以「译文打完了但进程不退出」这类退出路径的问题从来没被覆盖过。

两条硬规则：

1. **不要直接调 `main_entry()`** —— 它默认 `os._exit`，会把 pytest 进程一起带走。
   进程内测试一律用 `_cli_main()` 或 `_build_command()`。
2. **不要触发模型加载** —— 只测参数校验与退出路径，保证套件的秒级特性。
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from npu_translator import cli

SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------- 子进程工具
def _run_in_subprocess(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """在**独立进程**里跑 `main_entry()`，用于验证 `os._exit` 的行为。

    这类断言没法在进程内做（一 `os._exit` 测试就没了），只能起子进程看退出码。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    code = "from npu_translator.cli import main_entry; main_entry()"
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        cwd=str(SRC.parent),
    )


# ---------------------------------------------------------------- 退出码归一
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, cli.EXIT_OK),
        (0, 0),
        (4, 4),
        (120, 120),
        ("some message", cli.EXIT_USAGE),  # click 用字符串码表示"已打印过错误"
    ],
)
def test_as_exit_code(raw, expected):
    assert cli._as_exit_code(raw) == expected


# ---------------------------------------------------------------- 参数校验（不加载模型）
def test_languages_option_returns_zero():
    result = CliRunner().invoke(cli._build_command(), ["--languages"])
    assert result.exit_code == cli.EXIT_OK
    assert "繁体中文" in result.output  # §7.2：官方 33 种里最常被漏掉的那个


def test_invalid_newline_is_usage_error():
    result = CliRunner().invoke(cli._build_command(), ["hello", "--to", "en", "--newline", "bogus"])
    assert result.exit_code == cli.EXIT_USAGE


def test_bom_and_append_are_mutually_exclusive():
    result = CliRunner().invoke(cli._build_command(), ["hello", "--bom", "--append"])
    assert result.exit_code == cli.EXIT_USAGE


def test_interspersed_args_are_allowed():
    """`nputr "文本" --to en` 必须能解析（click 默认遇到位置参数就停止解析选项）。"""
    result = CliRunner().invoke(cli._build_command(), ["hello", "--newline", "bogus"])
    assert "No such command" not in result.output
    assert result.exit_code == cli.EXIT_USAGE  # 真进了业务逻辑，而不是被当成子命令


# ---------------------------------------------------------------- --model 解析
def _combined(result) -> str:
    """click 8.2 起 stderr 不再混进 `result.output`，两种版本都拿到。"""
    return result.output + (getattr(result, "stderr", "") or "")


def test_resolve_model_path_treats_plain_name_as_subdir(monkeypatch):
    """纯名字 → models/<名字>（与 NPT_MODEL 的语义一致）。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", Path("X:/models"))
    assert cli.cfg.resolve_model_path("Qwen3-1.7B") == str(Path("X:/models/Qwen3-1.7B"))


def test_resolve_model_path_keeps_path_like_values(monkeypatch):
    """含分隔符或绝对路径 → 原样，不再拼 MODEL_DIR。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", Path("X:/models"))
    # 用 str(Path(...)) 比对：Windows 上会把 / 归一成 \，别写死分隔符
    assert cli.cfg.resolve_model_path("D:/elsewhere/m") == str(Path("D:/elsewhere/m"))
    assert cli.cfg.resolve_model_path("sub/dir") == str(Path("sub/dir"))


def test_resolve_model_path_empty_falls_back_to_config():
    """不给 --model 就回落到 cfg.MODEL_PATH（含 NPT_MODEL 环境变量的结果）。"""
    assert cli.cfg.resolve_model_path("") == cli.cfg.MODEL_PATH
    assert cli.cfg.resolve_model_path(None) == cli.cfg.MODEL_PATH


def test_resolve_model_accepts_existing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", tmp_path)
    (tmp_path / "real-one").mkdir()
    path, code = cli._resolve_model("real-one", cli._Log())
    assert code == cli.EXIT_OK
    assert path == str(tmp_path / "real-one")


def test_resolve_model_unspecified_is_not_validated(monkeypatch):
    """未指定 → None，交给下层回落。**刻意不校验**：默认路径的失败语义（退出码 3）保持原样。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", Path("X:/definitely-not-there"))
    path, code = cli._resolve_model("", cli._Log())
    assert (path, code) == (None, cli.EXIT_OK)


def test_resolve_model_missing_dir_is_usage_error(tmp_path, monkeypatch):
    """显式给了却找不到 → 退出码 2（参数错误），不是 3（加载失败）。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", tmp_path)
    (tmp_path / "real-one").mkdir()
    path, code = cli._resolve_model("typo-name", cli._Log())
    assert path is None
    assert code == cli.EXIT_USAGE


def test_cli_rejects_unknown_model_before_loading(tmp_path, monkeypatch):
    """端到端：`-m <不存在的名字>` 必须在加载模型之前就退出 2，并列出可用候选。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", tmp_path)
    (tmp_path / "real-one").mkdir()
    result = CliRunner().invoke(
        cli._build_command(), ["hi", "--to", "en", "--model", "typo-name"]
    )
    assert result.exit_code == cli.EXIT_USAGE
    assert "real-one" in _combined(result)  # 提示里带上真正可用的名字


def test_benchmark_rejects_unknown_model(tmp_path, monkeypatch):
    """`-b` 也吃 --model，校验规则与翻译路径一致。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", tmp_path)
    result = CliRunner().invoke(cli._build_command(), ["-b", "-m", "typo-name"])
    assert result.exit_code == cli.EXIT_USAGE


def test_short_model_flag_parses(tmp_path, monkeypatch):
    """`-m` 短名可用（click 遇到位置参数后会停止解析选项的坑已在 _build_command 里放开）。"""
    monkeypatch.setattr(cli.cfg, "MODEL_DIR", tmp_path)
    (tmp_path / "real-one").mkdir()
    result = CliRunner().invoke(
        cli._build_command(), ["hi", "-m", "real-one", "--newline", "bogus"]
    )
    # 能走到 newline 校验，说明 -m 被正常解析而不是当成未知参数
    assert result.exit_code == cli.EXIT_USAGE
    assert "No such option" not in _combined(result)


# ---------------------------------------------------------------- flush 与关停诊断
class _BrokenStream:
    def flush(self):
        raise BrokenPipeError(22, "Invalid argument")


def test_flush_std_streams_tolerates_broken_pipe(monkeypatch):
    """关停阶段 flush 撞上已关闭的管道（`| more`）不得抛出。"""
    monkeypatch.setattr(cli.sys, "stdout", _BrokenStream())
    monkeypatch.setattr(cli.sys, "stderr", _BrokenStream())
    cli._flush_std_streams()


def test_flush_std_streams_flushes_both(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.sys, "stderr", err)
    out.write("x")
    err.write("y")
    cli._flush_std_streams()
    assert out.getvalue() == "x" and err.getvalue() == "y"


def test_exit_debug_writes_thread_dump(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(cli.sys, "stderr", buf)
    monkeypatch.setattr(cli.cfg, "EXIT_DEBUG", True)
    cli._log_shutdown_diagnostics(0)
    assert "exit-debug" in buf.getvalue()


def test_exit_debug_is_silent_by_default(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(cli.sys, "stderr", buf)
    monkeypatch.setattr(cli.cfg, "EXIT_DEBUG", False)
    cli._log_shutdown_diagnostics(0)
    assert buf.getvalue() == ""


# ---------------------------------------------------------------- 硬退出（子进程）
def test_hard_exit_returns_zero():
    """默认路径：os._exit(0)，进程必须真的退出且退出码为 0。"""
    proc = _run_in_subprocess(["--languages"])
    assert proc.returncode == cli.EXIT_OK


def test_hard_exit_preserves_error_code():
    """os._exit 不能把错误码吃掉。"""
    proc = _run_in_subprocess(["hello", "--newline", "bogus"])
    assert proc.returncode == cli.EXIT_USAGE


def test_hard_exit_can_be_disabled():
    """`NPT_HARD_EXIT=0` 走自然退出，退出码一致（用于对比排查关停阶段）。"""
    proc = _run_in_subprocess(["--languages"], {"NPT_HARD_EXIT": "0"})
    assert proc.returncode == cli.EXIT_OK


def test_exit_debug_survives_hard_exit():
    """诊断输出必须在 os._exit 之前写完，否则等于没有。"""
    proc = _run_in_subprocess(["--languages"], {"NPT_EXIT_DEBUG": "1"})
    assert proc.returncode == cli.EXIT_OK
    assert "exit-debug" in proc.stderr
