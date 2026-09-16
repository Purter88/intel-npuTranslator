"""命令行入口（SPEC.md · CLI 管道契约）。

硬约束（SPEC.md · CLI 管道契约）：

1. **stdout 只有译文**，诊断 / 进度 / 耗时 / 警告一律 stderr
2. **编码由程序全权控制**，不依赖 shell 重定向（PS 5.1 的 `>` 会产出 UTF-16LE）
3. **输出顺序 == 输入顺序**，并行也不例外
4. **单设备模式不加载第二条 pipeline**（hetero 才付 4 GB 内存）
5. **输入优先级**：位置参数 `TEXT` > `--file` > stdin

用法::

    nputr "今天天气不错" --to en
    nputr --to ja -f a.txt -o out.txt
    Get-Content a.txt -Encoding UTF8 -Raw | nputr --to en

基准模式（`-b`）：**不翻译**，跑性能基准并输出报告。此模式下 stdout 就是报告本身
（没有译文，所以不违反上面的第 1 条），进度与警告仍走 stderr::

    nputr -b                      # 自动挑全部可用设备（NPU / Intel iGPU / CPU）
    nputr -b -d cpu --bench-repeats 1
    nputr -b --to ja --bench-json -o docs/bench_ja.json
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from . import config as cfg
from . import benchmark as bench
from .device import DeviceManager, device_report, set_process_priority
from .encoding import (
    DecodeError,
    InputTooLarge,
    configure_stdio,
    decode_bytes,
    norm_newlines,
    open_output,
    read_bytes,
    read_stdin_bytes,
    silence_stdout,
    write_output,
    write_stdout,
)
from .languages import LANGUAGES, common, is_supported, others
from .orchestrate import OrchestrateConfig, Translator
from .pool import FatalWorkerError, cpu_pipeline_props
from .segment import NEWLINE_MODES

# ---------------------------------------------------------------- 退出码（SPEC.md · CLI 管道契约）
EXIT_OK = 0
EXIT_INPUT = 1           # 输入错误（含 --strict 下的空输入）
EXIT_USAGE = 2           # 参数错误（typer 默认也是 2）
EXIT_MODEL = 3           # 模型加载失败 / --strict 下的整体失败
EXIT_PARTIAL = 4         # 部分失败：长文有段未译出，原文保留，stdout 仍有输出
EXIT_BROKEN_PIPE = 120   # 下游关闭管道（| head / | more）
EXIT_INTERRUPTED = 130   # Ctrl+C

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    # 显式关掉 rich：typer 默认只要检测到 rich 就用它渲染 --help，
    # 而 rich 的可选依赖 markdown_it 未必装全（本机实测就是缺的），
    # 一个 --help 直接 ImportError 崩掉，对管道工具不可接受。
    rich_markup_mode=None,
    help="Intel NPU 本地离线翻译。默认即翻译：nputr \"文本\" --to en",
)


class _Log:
    """stderr 日志。--quiet 时静音，**但错误照常输出**。"""

    def __init__(self, quiet: bool = False, verbose: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose

    def info(self, msg: str) -> None:
        if not self.quiet:
            typer.secho(msg, err=True, fg=typer.colors.BRIGHT_BLACK)

    def debug(self, msg: str) -> None:
        if self.verbose and not self.quiet:
            typer.secho(msg, err=True, fg=typer.colors.BRIGHT_BLACK)

    def warn(self, msg: str) -> None:
        if not self.quiet:
            typer.secho(f"警告: {msg}", err=True, fg=typer.colors.YELLOW)

    def error(self, msg: str) -> None:
        typer.secho(f"错误: {msg}", err=True, fg=typer.colors.RED)


@dataclass
class Options:
    to: str = "en"
    source: str = "auto"
    device: str = cfg.DEVICE
    model: str = ""          # 空 = 不指定，回落到 cfg.MODEL_PATH（含 NPT_MODEL）
    cpu_threads: str = "0"
    cpu_core_type: str = "any"
    cpu_ht: Optional[str] = None
    cpu_priority: str = "normal"
    newline: str = "soft"
    no_segment: bool = False
    stream: bool = False
    quiet: bool = False
    strict: bool = False
    verbose: bool = False
    input_encoding: Optional[str] = None
    max_input_mb: float = 64.0
    no_cache: bool = False
    no_warmup: bool = False
    output: Optional[str] = None
    append: bool = False
    bom: bool = False
    log: _Log = field(default_factory=_Log)


# ---------------------------------------------------------------- 输入
def _read_input(text: Optional[str], file: Optional[str], opts: Options) -> str:
    """按 位置参数 > --file > stdin 取输入，全程走字节 + 显式解码。"""
    if text:
        # 位置参数一样受 --max-input-mb 约束：不然这条限制会被轻易绕过，
        # 而用户以为自己设了上限
        size = len(text.encode("utf-8"))
        limit = int(opts.max_input_mb * 1048576)
        if size > limit:
            raise InputTooLarge(size, limit, "命令行参数")
        return norm_newlines(text)

    if file:
        data = read_bytes(file, opts.max_input_mb)
        decoded, enc = decode_bytes(data, opts.input_encoding, f"文件 {file}")
    else:
        if sys.stdin.isatty():
            opts.log.info("等待 stdin 输入（Ctrl+Z / Ctrl+D 结束）...")
        data = read_stdin_bytes(opts.max_input_mb)
        decoded, enc = decode_bytes(data, opts.input_encoding, "stdin")

    opts.log.debug(f"输入解码: {enc}，{len(decoded)} 字符")
    return norm_newlines(decoded)


# ---------------------------------------------------------------- 模型路径
def _resolve_model(spec: str, log: _Log) -> tuple[str | None, int]:
    """解析 `--model`，返回 `(模型目录路径, 退出码)`，退出码非 0 表示失败。

    两条刻意的取舍：

    1. **未指定时返回 `None`**，由下层回落到 `cfg.MODEL_PATH` —— 这条路径的失败
       语义（加载不起来 → 退出码 3）保持原样，本次不动它。
    2. **显式给了 `--model` 却找不到目录 → 退出码 2（参数错误）**，不是 3。
       这是参数值写错，而且能在加载之前拦下来，不必白等一次 NPU 编译；
       顺手把 `models/` 下真正可用的名字列出来，省得用户再去翻目录。
    """
    raw = (spec or "").strip()
    if not raw:
        return None, EXIT_OK

    path = cfg.resolve_model_path(raw)
    if not Path(path).is_dir():
        log.error(f"--model 指向的目录不存在: {path}")
        found = cfg.available_models()
        if found:
            log.error(f"models/ 下可用: {', '.join(found)}")
        else:
            log.error(f"{cfg.MODEL_DIR} 下还没有模型，先用 scripts\\fetch_model.py 下载一个")
        return None, EXIT_USAGE
    return path, EXIT_OK


# ---------------------------------------------------------------- 主流程
def _run_translate(text: Optional[str], file: Optional[str], opts: Options) -> int:
    log = opts.log
    t_start = time.perf_counter()

    if opts.bom and opts.append:
        log.error("--bom 与 --append 不能同时使用（BOM 出现在文件中间会变成 \\ufeff）")
        return EXIT_USAGE
    if opts.newline not in NEWLINE_MODES:
        log.error(f"--newline 必须是 {NEWLINE_MODES} 之一")
        return EXIT_USAGE
    if opts.device.lower() == "hetero" and opts.stream:
        log.error("--stream 与 hetero 不能同时使用（流式只有一个设备产出）")
        return EXIT_USAGE

    model_path, code = _resolve_model(opts.model, log)
    if code:
        return code
    if model_path:
        log.debug(f"模型: {bench.display_path(model_path)}")

    # ---------------- 1. 建池（惰性，此刻还不碰模型）
    cpu_props = cpu_pipeline_props(opts.cpu_threads, opts.cpu_core_type, opts.cpu_ht)
    if cpu_props:
        log.debug(f"CPU 调度属性: {cpu_props}")
    if set_process_priority(opts.cpu_priority):
        log.debug(f"进程优先级已调整为 {opts.cpu_priority}")

    degraded: list[str] = []

    def on_degrade(name: str, err: str) -> None:
        degraded.append(name)
        log.warn(f"设备 {name} 不可用，已降级：{err}")

    # 编排层（SPEC.md · 架构与目录结构 · 共用编排层 orchestrate.py）：分段 / 去重 / 缓存 / 拼接都在它里面，
    # CLI 只负责「输入怎么来、输出怎么走、退出码怎么映射」。
    try:
        orch = Translator(
            OrchestrateConfig(
                target=opts.to, source=opts.source, device=opts.device,
                newline=opts.newline, no_segment=opts.no_segment,
                no_cache=opts.no_cache, cpu_props=cpu_props,
                model_path=model_path,
            ),
            on_degrade=on_degrade,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(f"初始化失败: {exc}")
        return EXIT_MODEL

    # ---------------- 2. 预热与读输入并行（NPU 首次编译约 30 s）
    warm_error: list[BaseException] = []

    def _warm() -> None:
        try:
            orch.prepare()
        except BaseException as exc:  # noqa: BLE001
            warm_error.append(exc)

    warm_thread = threading.Thread(target=_warm, daemon=True, name="warmup")
    if not opts.no_warmup:
        warm_thread.start()
        log.info("正在唤醒翻译引擎（首次约 30 秒）...")

    try:
        source_text = _read_input(text, file, opts)
    except (DecodeError, InputTooLarge, FileNotFoundError, OSError) as exc:
        log.error(str(exc))
        return EXIT_INPUT

    # 空输入：不等预热，直接退（daemon 预热线程随进程退出，白跑 30 s 编译不值得）
    if not source_text.strip():
        if opts.strict:
            log.error("输入为空（--strict）")
            return EXIT_INPUT
        return EXIT_OK

    # 分段过多时流式没有意义，提前拦下，省得白等一次编译
    if opts.stream and ("\n" in source_text.strip() or len(source_text) > cfg.SEGMENT_MAX_CHARS):
        log.error("--stream 不支持分段文本（多行或超长）。请先切分，或去掉 --stream")
        return EXIT_USAGE

    # ⚠️ 未启动的线程不能 join（RuntimeError: cannot join thread before it is started）。
    # `--no-warmup` 时这个线程根本没 start，旧代码在这里直接崩（2026-09-12 实测）。
    if warm_thread.ident is not None:
        if warm_thread.is_alive():
            log.info("输入已就绪，等待引擎...")
        # 限时等待：NPU 被别的进程占着时（R9）加载可能永不返回，
        # 不加超时就是"零输出 + 永久挂起"，跟卡死没法区分。
        warm_thread.join(cfg.WARMUP_TIMEOUT or None)
        if warm_thread.is_alive():
            log.error(
                f"引擎预热超过 {cfg.WARMUP_TIMEOUT}s 仍未就绪"
                f"（NPU 可能被其它进程占用，见 SPEC.md · 已知问题与活跃风险 · NPU 被其他进程占用时生成永久挂起）"
            )
            return EXIT_MODEL

    if warm_error:
        log.error(f"模型加载失败: {warm_error[0]}")
        return EXIT_MODEL
    if not orch.workers:
        log.error("没有可用的推理设备")
        return EXIT_MODEL

    # ---------------- 3. 空输入
    if not source_text.strip():
        if opts.strict:
            log.error("输入为空（--strict）")
            return EXIT_INPUT
        return EXIT_OK

    if not is_supported(opts.to):
        log.warn(f"目标语言 {opts.to!r} 不在已知语种表内，将原样传给模型")

    # ---------------- 4. 流式（与分段互斥，上面已拦过分段的情形）
    if opts.stream:
        try:
            for chunk in orch.stream(source_text, target=opts.to, source=opts.source):
                write_stdout(chunk)
            write_stdout("\n")
        except BrokenPipeError:
            silence_stdout()
            return EXIT_BROKEN_PIPE
        return EXIT_OK

    # ---------------- 5. 翻译：分段 / 缓存 / 批内去重 / 拼接全在编排层里
    # （这些踩过坑的细节归编排层，见 SPEC.md · 架构与目录结构 · 共用编排层 orchestrate.py）
    outcome = orch.translate(source_text)

    log.debug(f"分段: {outcome.units} 段 / {outcome.lines} 行，模式={opts.newline}")
    if outcome.cache_hits:
        log.debug(f"缓存命中 {outcome.cache_hits}/{outcome.units}")
    if outcome.model_calls:
        log.debug(f"推理 {outcome.model_calls} 段，用时 {outcome.infer_s:.2f}s")
    if outcome.packed_retries:
        log.debug(f"打包输出行数不匹配，退回逐行重翻 {outcome.packed_retries} 个单元")
    for index, err in outcome.failures:
        log.warn(f"第 {index + 1} 段翻译失败（{err}），保留原文")

    # ---------------- 6. 输出
    out = outcome.text
    if not out.endswith("\n"):
        out += "\n"
    try:
        if opts.output:
            with open_output(opts.output, append=opts.append, bom=opts.bom) as fh:
                write_output(fh, out)
            log.debug(f"已写入 {opts.output}")
        else:
            write_stdout(out)
    except BrokenPipeError:
        silence_stdout()
        return EXIT_BROKEN_PIPE

    if opts.verbose:
        total = time.perf_counter() - t_start
        log.debug(
            f"模型={bench.display_path(model_path or cfg.MODEL_PATH)}  "
            f"设备={','.join(w.name for w in orch.workers)}  "
            f"分段={outcome.units}  复用={outcome.reused}  "
            f"失败={outcome.failed}  总耗时={total:.2f}s"
        )
        snap = orch.cache.snapshot()
        if snap["hits"] or snap["misses"]:
            log.debug(f"缓存: {snap}")

    if outcome.failed:
        if opts.strict:
            log.error(f"有 {outcome.failed} 段未译出（--strict）")
            return EXIT_MODEL
        return EXIT_PARTIAL
    return EXIT_OK


# ---------------------------------------------------------------- 基准模式
@dataclass
class BenchOptions:
    """`-b` 模式的参数。与翻译路径分开，避免两套语义互相污染。"""

    devices: list[str] | None = None   # None = 自动挑全部可用设备
    target: str | None = None          # None = 用固定混合 prompt 集
    model: str = ""                    # 空 = 不指定，回落到 cfg.MODEL_PATH（含 NPT_MODEL）
    repeats: int = cfg.BENCH_REPEATS
    tokens: int = cfg.BENCH_MAX_NEW_TOKENS
    no_warmup: bool = False
    as_json: bool = False
    cpu_props: dict = field(default_factory=dict)
    output: Optional[str] = None
    log: _Log = field(default_factory=_Log)


def _run_benchmark(opts: BenchOptions) -> int:
    """跑基准并输出报告（SPEC.md · CLI 管道契约 · --benchmark）。

    与翻译路径的三点差异：

    1. **不读 stdin**（基准不需要输入，读了反而会在无输入的终端里挂住）
    2. **stdout 是报告**（无译文可输出，`-o` 额外存档一份，不像翻译模式那样顶替 stdout）
    3. **单台设备失败不中断其余设备** —— 全挂才退出码 3，部分失败 4
    """
    log = opts.log
    t_start = time.perf_counter()
    log.info("基准模式：不读取 stdin，进度走 stderr")

    model_path, code = _resolve_model(opts.model, log)
    if code:
        return code
    if model_path:
        log.info(f"模型: {bench.display_path(model_path)}")

    report = bench.run_benchmark(
        opts.devices,
        model_path=model_path,
        prompts=bench.build_prompts(opts.target),
        repeats=opts.repeats,
        max_new_tokens=opts.tokens,
        warmup=not opts.no_warmup,
        cpu_props=opts.cpu_props,
        on_event=log.info,
    )

    text = report.to_json() if opts.as_json else bench.render_text(report)
    # 报告**永远**上 stdout（基准模式下它就是主产出，没有译文可让位）；
    # `-o` 只是额外存档一份，不像翻译模式那样顶替 stdout。
    try:
        if opts.output:
            with open_output(opts.output, append=False, bom=False) as fh:
                write_output(fh, text)
            log.info(f"报告已写入 {opts.output}")
        write_stdout(text)
    except BrokenPipeError:
        silence_stdout()
        return EXIT_BROKEN_PIPE

    log.debug(f"基准总耗时 {time.perf_counter() - t_start:.2f}s")

    if not report.ok_devices:
        log.error("所有设备均未能完成基准")
        return EXIT_MODEL
    if report.failed_devices:
        log.warn(f"{len(report.failed_devices)} 台设备失败（报告里已标注）")
        return EXIT_PARTIAL
    return EXIT_OK


# ---------------------------------------------------------------- typer 绑定
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    text: Optional[str] = typer.Argument(None, help="待翻译文本；省略则读 --file 或 stdin"),
    to: str = typer.Option("en", "--to", "-t", help="目标语言代码，如 en / ja / zh-Hant"),
    source: str = typer.Option("auto", "--from", "-f", help="源语言代码，auto 交给模型判断"),
    file: Optional[str] = typer.Option(None, "--file", "-i", help="从文件读取待翻译文本"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="输出到文件（UTF-8 无 BOM）。推荐用这个而不是 shell 重定向",
    ),
    append: bool = typer.Option(False, "--append", help="追加到输出文件"),
    bom: bool = typer.Option(False, "--bom", help="输出写 UTF-8 BOM（与 --append 互斥）"),
    device: str = typer.Option(
        cfg.DEVICE, "--device", "-d",
        help="npu | cpu | gpu | auto | hetero（hetero = NPU+CPU 并行，约 4 GB 内存）",
    ),
    model: str = typer.Option(
        "", "--model", "-m",
        help=(
            "指定模型：models/ 下的目录名，或一个路径（相对/绝对都行）。"
            "不给则回落到 NPT_MODEL 环境变量 / 内置默认模型。"
            "例：-m Qwen3-1.7B-int4-ov、-m X:/models/Qwen3-1.7B-int4-ov"
        ),
    ),
    cpu_threads: str = typer.Option(
        "0", "--cpu-threads",
        help="CPU 线程数：0=OpenVINO 自动（默认，不写死核心数）| half=物理核一半 | 正整数",
    ),
    cpu_core_type: str = typer.Option(
        "any", "--cpu-core-type",
        help="any | pcore | ecore，原样透传给 OpenVINO，代码不做机器判断",
    ),
    cpu_ht: Optional[str] = typer.Option(
        None, "--cpu-ht", help="on | off 超线程，默认交给 OpenVINO",
    ),
    cpu_priority: str = typer.Option(
        "normal", "--cpu-priority",
        help="idle | below | normal。⚠️ 改的是**整个进程**，会连带拖慢 NPU 宿主侧调度",
    ),
    newline: str = typer.Option(
        "soft", "--newline",
        help=(
            "换行策略。soft=保证换行不丢失，但不强制在此断句（默认，适合散文）；"
            "hard=每行独立翻译，**保证行数 1:1 对齐**，适合日志/列表/CSV/字幕，"
            "⚠️ 会把硬折行的散文从中间劈开，导致语法破碎、指代丢失；"
            "auto=启发式判断硬边界，⚠️ 会判断错，混排文档可能译坏"
        ),
    ),
    lines: bool = typer.Option(False, "--lines", help="--newline hard 的别名"),
    no_segment: bool = typer.Option(
        False, "--no-segment", help="强制单段（超过 KV cache 会被静默截断，风险自负）",
    ),
    stream: bool = typer.Option(
        False, "--stream", help="流式输出（与分段互斥，长文本会报错）",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="静音 stderr（错误除外）"),
    strict: bool = typer.Option(
        False, "--strict", help="部分失败视为整体失败；空输入退出码 1",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="把耗时 / 设备 / 分段信息打到 stderr",
    ),
    input_encoding: Optional[str] = typer.Option(
        None, "--input-encoding", help="强制指定输入编码（默认 BOM → utf-8 → cp936）",
    ),
    max_input_mb: float = typer.Option(64.0, "--max-input-mb", help="输入体积上限（MB）"),
    no_cache: bool = typer.Option(False, "--no-cache", help="禁用译文 LRU 缓存"),
    no_warmup: bool = typer.Option(False, "--no-warmup", help="跳过预热（首次推理会更慢）"),
    run_bench: bool = typer.Option(
        False, "--benchmark", "-b",
        help=(
            "跑性能基准而不是翻译：逐个设备测加载耗时 / TTFT / tok/s / RSS，"
            "输出跨平台可比的报告。不给 -d 则自动跑全部可用设备；"
            "给 --to 则按该语向生成 prompt"
        ),
    ),
    bench_repeats: int = typer.Option(
        cfg.BENCH_REPEATS, "--bench-repeats",
        help="每个 prompt 重复次数（默认 3，机器间对比别改）",
    ),
    bench_tokens: int = typer.Option(
        cfg.BENCH_MAX_NEW_TOKENS, "--bench-tokens",
        help=f"每次生成的 token 上限（NPU 上超过 {cfg.MIN_RESPONSE_LEN} 会被静默截断）",
    ),
    bench_json: bool = typer.Option(
        False, "--bench-json", help="报告输出 JSON（便于跨机器比对 / 存档）",
    ),
    bench_no_warmup: bool = typer.Option(
        False, "--bench-no-warmup",
        help="跳过预热那一次生成（数字会包含首次编译，更接近冷启动）",
    ),
    list_languages: bool = typer.Option(
        False, "--languages", help="列出支持的语种后退出",
    ),
    list_devices: bool = typer.Option(
        False, "--devices", help="列出可用设备与 NPU 信息后退出",
    ),
) -> None:
    """翻译文本（默认行为，`nputr "文本" --to en`）。

    ⚠️ 语种与设备列表做成**选项**而不是子命令：callback 里的位置参数 `text`
    会把子命令名吃掉（click 的 MultiCommand 先解析 group 参数），
    实测 `nputr languages` 会变成"把 languages 这个词翻译成英文"。
    """
    if ctx.invoked_subcommand is not None:
        return

    if list_languages:
        _print_languages()
        return
    if list_devices:
        typer.echo(device_report())
        return

    log = _Log(quiet=quiet, verbose=verbose)

    if run_bench:
        # 只有**显式**给出 -d / --to 才收窄基准范围：
        # 默认是"全部可用设备 + 固定混合 prompt 集"，保证跨机器可比。
        # `-d hetero` 在这里展开成逐个设备（要的是单设备成绩，不是并联那条管道）。
        code = _run_benchmark(BenchOptions(
            devices=bench.select_devices(device if _is_explicit(ctx, "device") else "auto"),
            target=to if _is_explicit(ctx, "to") else None,
            model=model,
            repeats=bench_repeats,
            tokens=bench_tokens,
            no_warmup=bench_no_warmup,
            as_json=bench_json,
            cpu_props=cpu_pipeline_props(cpu_threads, cpu_core_type, cpu_ht),
            output=output,
            log=log,
        ))
        if code:
            raise typer.Exit(code=code)
        return

    opts = Options(
        to=to, source=source, device=device, model=model, cpu_threads=cpu_threads,
        cpu_core_type=cpu_core_type, cpu_ht=cpu_ht, cpu_priority=cpu_priority,
        newline="hard" if lines else newline, no_segment=no_segment, stream=stream,
        quiet=quiet, strict=strict, verbose=verbose, input_encoding=input_encoding,
        max_input_mb=max_input_mb, no_cache=no_cache, no_warmup=no_warmup,
        output=output, append=append, bom=bom, log=log,
    )
    code = _run_translate(text, file, opts)
    if code:
        raise typer.Exit(code=code)


def _is_explicit(ctx: typer.Context, name: str) -> bool:
    """判断某个选项是不是用户在命令行上**真的写了**（而不是吃默认值）。

    基准要靠这个区分「`-d npu` 只测 NPU」和「没写 -d → 全部设备都测」——
    两者的默认值都是配置里的 `npu`，只看取值分不出来。

    ⚠️ 按**名字**比而不是拿枚举对象比：typer 0.27 自带一份 click（`typer._click`），
    `click.core.ParameterSource.DEFAULT`（值 5）与 `typer._click.core.ParameterSource.DEFAULT`
    （值 3）是两个不同的枚举类，`==` 恒为 False，直接比会静默判成"用户写过了"。
    """
    src = ctx.get_parameter_source(name)
    if src is None:
        return False
    return getattr(src, "name", str(src)) != "DEFAULT"


def _print_languages() -> None:
    typer.echo(f"共 {len(LANGUAGES)} 种（{len(common())} 常用 + {len(others())} 其他）")
    typer.echo("")
    typer.echo("常用:")
    for lang in common():
        typer.echo(f"  {lang.code:<8} {lang.zh_name:<10} {lang.en_name}")
    typer.echo("")
    typer.echo("其他（民族语/方言等）:")
    for lang in others():
        typer.echo(f"  {lang.code:<8} {lang.zh_name:<10} {lang.en_name}")

def _build_command():
    """构造 click 命令对象。

    click 的 `MultiCommand.allow_interspersed_args = False`：一旦遇到第一个
    位置参数就停止解析选项，于是 `nputr "文本" --to en` 会把 --to 当成
    子命令名报 "No such command"。规格（SPEC.md · CLI 管道契约）的示例就是这种写法，必须放开。

    没有子命令（语种/设备列表改成了 --languages / --devices 选项），
    所以这里可以无条件放开。
    """
    cmd = typer.main.get_command(app)
    cmd.allow_interspersed_args = True
    return cmd


def _as_exit_code(code: object) -> int:
    """把 click 抛出的退出码归一成 int（None=0，字符串=已打印过错误，按参数错误处理）。"""
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_USAGE


def _cli_main() -> int:
    """CLI 主逻辑，**只返回退出码，不结束进程**。

    与 `main_entry` 分开的原因（SPEC.md · CLI 管道契约）：`main_entry` 默认会 `os._exit`，
    在 pytest / CliRunner 里直接调用它会把测试进程一起带走。测试请调本函数。
    """
    # `configure_stdio()` 必须在 typer 解析参数**之前**：否则参数错误的提示
    # 会用 cp936 写 stderr，遇到非 GBK 字符直接 UnicodeEncodeError。
    configure_stdio()
    try:
        _build_command()(prog_name="nputr")
    except SystemExit as exc:
        return _as_exit_code(exc.code)
    except BrokenPipeError:
        silence_stdout()
        return EXIT_BROKEN_PIPE
    except KeyboardInterrupt:
        typer.secho("\n已中断", err=True, fg=typer.colors.YELLOW)
        return EXIT_INTERRUPTED
    return EXIT_OK


def _flush_std_streams() -> None:
    """退出前把 stdout / stderr 全部冲掉。

    `os._exit` 不跑解释器关停、也不跑 `TextIOWrapper` 的析构，缓冲区里的东西会直接丢，
    所以必须在它**之前**显式 flush。管道已关闭时静默忽略（`| more` 场景）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - 关停阶段没有可恢复的动作，管道已断属预期
            pass


def _log_shutdown_diagnostics(code: int) -> None:
    """`NPT_EXIT_DEBUG=1` 时把退出前仍存活的线程打到 stderr。

    用途：再出现"译文输出完但进程不退出"时，开着它跑一次就知道卡在谁身上，
    而不是靠猜。
    """
    if not cfg.EXIT_DEBUG:
        return
    try:
        main = threading.current_thread()
        others = [t for t in threading.enumerate() if t is not main]
        lines = [f"[exit-debug] 退出码={code} hard_exit={cfg.HARD_EXIT} 残留线程={len(others)}"]
        for t in others:
            lines.append(f"[exit-debug]   {t.name!r} daemon={t.daemon} alive={t.is_alive()}")
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 诊断本身不许影响退出
        pass


def main_entry() -> None:
    """console script 入口（SPEC.md · CLI 管道契约）。

    职责：**把进程干净地结束掉**。真正的 CLI 逻辑在 `_cli_main`。

    为什么要 `os._exit` 而不是让它自然退出：译文输出完之后，进程里仍然挂着
    OpenVINO 留下的 daemon 线程（实测为 `ThreadPoolExecutor-0_0`），而 CPython 关停阶段
    会把它 join 掉。一旦这个线程被原生调用卡住（NPU 被占用 / 驱动态异常），
    `threading._shutdown()` 就永远返回不了 —— 表现为"译文打完了，但终端不退出"。
    flush 之后直接 `os._exit` 把这条路径物理切断（可用 `NPT_HARD_EXIT=0` 关掉做对比）。
    """
    code = _cli_main()
    _flush_std_streams()
    _log_shutdown_diagnostics(code)
    if cfg.HARD_EXIT:
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main_entry()
