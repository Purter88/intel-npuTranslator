"""基准模块测试（SPEC.md · CLI 管道契约 · --benchmark）。

原则与其它测试一致：**不加载模型、不碰 OpenVINO**，套件必须保持秒级。
做法是把管道工厂注入进去（`pipeline_factory` / `gen_config_factory`），
用假管道把"时序"跑出来，只断言测量与汇总逻辑。
"""
from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from npu_translator import benchmark as bench
from npu_translator import cli
from npu_translator import config as cfg


# ---------------------------------------------------------------- 假管道
class FakePipe:
    """假 LLMPipeline：`generate()` 逐 token 回调 streamer，可注入失败。"""

    def __init__(self, tokens: int = 6, per_token: float = 0.001, fail: bool = False) -> None:
        self.tokens = tokens
        self.per_token = per_token
        self.fail = fail
        self.calls = 0

    def generate(self, prompt, gen_config, streamer=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("设备被占用")
        for i in range(self.tokens):
            time.sleep(self.per_token)
            if streamer is not None:
                streamer(f"t{i}")
        return " ".join(f"t{i}" for i in range(self.tokens))


def _fake_factory(pipe: FakePipe | None = None, fail_on: str | None = None):
    """构造管道工厂；`fail_on` 指定哪台设备在**加载**阶段就炸。"""
    created: list[tuple[str, dict]] = []

    def factory(model_path, device, **config):
        created.append((device, dict(config)))
        if fail_on is not None and device.upper().startswith(fail_on.upper()):
            raise RuntimeError(f"{device} 不可用")
        return pipe if pipe is not None else FakePipe()

    factory.created = created  # type: ignore[attr-defined]
    return factory


class _DummyGenConfig:
    max_new_tokens = 128


# ---------------------------------------------------------------- 设备选择
class _FakeManager:
    def __init__(self, devices, intel_gpu=None):
        self._devices = devices
        self._igpu = intel_gpu

    def available_devices(self):
        return list(self._devices)

    def intel_gpu(self):
        return self._igpu

    def resolve(self, preferred="auto"):
        return "CPU" if preferred.upper() not in self._devices else preferred.upper()


def test_select_devices_skips_non_intel_gpu():
    """OpenVINO 会把 NVIDIA dGPU 列成 GPU，`intel_gpu()` 过滤不掉就会跑上去必失败。"""
    m = _FakeManager(["NPU", "GPU.0", "GPU.1", "CPU"], intel_gpu="GPU.0")
    assert bench.select_devices("auto", m) == ["NPU", "GPU.0", "CPU"]


def test_select_devices_auto_without_npu():
    """没有 NPU 的机器（或 Linux / 老平台）只该跑 CPU。"""
    m = _FakeManager(["CPU"], intel_gpu=None)
    assert bench.select_devices("auto", m) == ["CPU"]


def test_select_devices_explicit_single():
    m = _FakeManager(["NPU", "CPU"], intel_gpu=None)
    assert bench.select_devices("npu", m) == ["NPU"]


def test_select_devices_hetero_expands():
    """hetero 是并联管道，基准要的是单设备成绩 → 展开成逐个设备。"""
    m = _FakeManager(["NPU", "CPU"], intel_gpu=None)
    assert bench.select_devices("hetero", m) == ["NPU", "CPU"]


# ---------------------------------------------------------------- 配置
def test_pipeline_config_npu_has_static_shape():
    cfg_dict = bench.pipeline_config_for("NPU")
    assert cfg_dict["MAX_PROMPT_LEN"] == cfg.MAX_PROMPT_LEN
    assert "NPUW_CACHE_DIR" in cfg_dict


def test_pipeline_config_cpu_takes_thread_props():
    cfg_dict = bench.pipeline_config_for("CPU", {"INFERENCE_NUM_THREADS": 8})
    assert cfg_dict["INFERENCE_NUM_THREADS"] == 8


def test_pipeline_config_does_not_leak_cpu_props_to_npu():
    """CPU 的线程属性不该跑到 NPU 上（会导致 NPU 加载失败）。"""
    assert "INFERENCE_NUM_THREADS" not in bench.pipeline_config_for("NPU", {"INFERENCE_NUM_THREADS": 8})


# ---------------------------------------------------------------- prompt
def test_default_prompts_are_stable():
    """固定 prompt 集 = 跨机器可比的口径，改动要有意识。"""
    assert len(bench.DEFAULT_PROMPTS) == 5
    assert bench.build_prompts(None) == list(bench.DEFAULT_PROMPTS)


def test_build_prompts_for_target():
    prompts = bench.build_prompts("ja")
    assert len(prompts) == len(bench.BENCH_SOURCE_SENTENCES)
    assert all("Japanese" in p for p in prompts)


def test_build_prompts_uses_chinese_name_for_traditional():
    """§7.2：繁体中文必须用中文名，写 English 名模型会回吐原文。"""
    prompts = bench.build_prompts("zh-Hant")
    assert all("繁体中文" in p for p in prompts)


# ---------------------------------------------------------------- 测量
def test_run_benchmark_counts_and_averages():
    pipe = FakePipe(tokens=6, per_token=0.001)
    report = bench.run_benchmark(
        ["CPU"],
        prompts=["p1", "p2"],
        repeats=2,
        max_new_tokens=32,
        warmup=True,
        pipeline_factory=_fake_factory(pipe),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    dev = report.devices[0]
    assert dev.ok
    assert len(dev.runs) == 4          # 2 prompt × 2 次
    assert dev.avg_tok_s > 0
    assert dev.avg_ttft_s > 0
    assert pipe.calls == 5             # 4 次测量 + 1 次预热
    assert dev.warmup_s is not None    # 预热那次被记下但**不进** runs


def test_run_benchmark_no_warmup():
    pipe = FakePipe()
    report = bench.run_benchmark(
        ["CPU"], prompts=["p1"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(pipe),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    assert report.devices[0].warmup_s is None
    assert pipe.calls == 1


def test_warmup_run_is_excluded_from_stats():
    """预热含首次编译，混进平均值会把结论带偏（NPU 首次 30 s vs 稳定 4 s）。"""
    pipe = FakePipe(tokens=4, per_token=0.001)
    report = bench.run_benchmark(
        ["CPU"], prompts=["p1"], repeats=1, warmup=True,
        pipeline_factory=_fake_factory(pipe),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    assert len(report.devices[0].runs) == 1


def test_device_failure_does_not_abort_others():
    report = bench.run_benchmark(
        ["NPU", "CPU"],
        prompts=["p1"], repeats=1,
        pipeline_factory=_fake_factory(FakePipe(), fail_on="NPU"),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    assert report.devices[0].error and not report.devices[0].ok
    assert report.devices[1].ok
    assert report.fastest() is report.devices[1]
    assert len(report.failed_devices) == 1


def test_inference_failure_keeps_partial_samples():
    """中途炸了也要保留已测到的样本，不能整台设备白跑。"""
    class _Flaky(FakePipe):
        def generate(self, prompt, gen_config, streamer=None):
            if self.calls >= 2:
                raise RuntimeError("推理中断")
            return super().generate(prompt, gen_config, streamer)

    report = bench.run_benchmark(
        ["CPU"], prompts=["p1", "p2"], repeats=2, warmup=False,
        pipeline_factory=_fake_factory(_Flaky()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    dev = report.devices[0]
    assert len(dev.runs) == 2
    assert dev.error is not None


def test_npu_over_window_warns():
    """NPU 是静态形状，max_new_tokens 超 MIN_RESPONSE_LEN 会被**静默截断**。"""
    events: list[str] = []
    report = bench.run_benchmark(
        ["NPU"], prompts=["p1"], repeats=1, max_new_tokens=cfg.MIN_RESPONSE_LEN + 1,
        pipeline_factory=_fake_factory(FakePipe()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        on_event=events.append,
        with_env=False,
    )
    assert report.devices[0].warnings
    assert any("截断" in e for e in events)


# ---------------------------------------------------------------- 报告
def _sample_report() -> bench.BenchReport:
    return bench.run_benchmark(
        ["NPU", "CPU"],
        prompts=["p1", "p2"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(FakePipe()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )


def test_render_text_has_table_and_summary():
    text = bench.render_text(_sample_report())
    assert "nputr benchmark" in text
    assert "NPU" in text and "CPU" in text
    assert "吞吐最快" in text


def test_report_exposes_spread():
    """只给平均值会藏掉"这数字能不能信"（本机 GPU 两轮 37.6 / 25.4）。"""

    class _VaryingPipe(FakePipe):
        def generate(self, prompt, gen_config, streamer=None):
            self.per_token = 0.001 * (1 + self.calls)   # 越跑越慢，制造波动
            return super().generate(prompt, gen_config, streamer)

    report = bench.run_benchmark(
        ["CPU"], prompts=["p1", "p2"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(_VaryingPipe(tokens=6)),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    dev = report.devices[0]
    assert dev.min_tok_s < dev.avg_tok_s < dev.max_tok_s
    assert report.to_dict()["devices"][0]["min_tok_s"] == dev.min_tok_s
    assert "区间" in bench.render_text(report)


def test_render_text_marks_failed_device():
    report = bench.run_benchmark(
        ["NPU"], prompts=["p1"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(FakePipe(), fail_on="NPU"),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    text = bench.render_text(report)
    assert "失败" in text and "失败设备" in text


def test_json_is_serializable_and_complete():
    report = bench.run_benchmark(
        ["CPU"], prompts=["p1"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(FakePipe()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    data = json.loads(report.to_json())
    assert data["settings"]["repeats"] == 1
    assert data["devices"][0]["runs"]
    assert data["summary"]["fastest"] == "CPU"


def test_json_survives_nan():
    """TTFT 拿不到时是 NaN，`json.dumps` 默认会产出非法 JSON（字面量 NaN）。"""

    class _EmptyPipe:
        def generate(self, prompt, gen_config, streamer=None):
            return ""

    report = bench.run_benchmark(
        ["CPU"], prompts=["p1"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(_EmptyPipe()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    raw = report.to_json()
    assert "NaN" not in raw
    json.loads(raw)  # 必须是合法 JSON


def test_report_never_leaks_absolute_paths():
    """见 `SPEC.md · Git 约定：本机绝对路径禁止入库`。基准报告是要进 docs/ 的。

    模型路径与 NPUW_CACHE_DIR 天然是绝对路径，不收敛就把机器目录结构写进了版本库。
    """
    report = bench.run_benchmark(
        ["NPU"], prompts=["p1"], repeats=1, warmup=False,
        pipeline_factory=_fake_factory(FakePipe()),
        gen_config_factory=lambda n: _DummyGenConfig(),
        with_env=False,
    )
    raw = report.to_json()
    assert str(cfg.ROOT) not in raw
    assert report.to_dict()["model"].startswith("models/")
    assert report.to_dict()["devices"][0]["config"]["NPUW_CACHE_DIR"] == ".npucache"


def test_display_path_keeps_relative_and_strips_outside_paths():
    assert bench.display_path("models/x") == "models/x"   # 相对路径原样
    assert bench.display_path(123) == 123                 # 非字符串原样
    assert bench.display_path("Z:/elsewhere/model") == "model"   # 项目外的只留名字
    assert bench.display_path(str(cfg.ROOT / "models" / "m")) == "models/m"


def test_environment_info_is_best_effort():
    info = bench.environment_info()
    assert "python" in info and "platform" in info
    # 拿不到 OpenVINO 也不许抛，值是 N/A 占位
    assert isinstance(info["openvino"], str)


# ---------------------------------------------------------------- CLI
@pytest.fixture
def bench_cli(monkeypatch):
    """把基准路径里的重活换成假货：不 import OpenVINO、不加载模型。"""
    monkeypatch.setattr(bench, "environment_info", lambda: {
        "time": "2026-09-13T00:00:00+08:00", "platform": "test", "cpu_count": 4,
        "python": "3.11.9", "openvino": "test",
    })
    monkeypatch.setattr(bench, "select_devices", lambda preferred="auto", manager=None: ["CPU"])
    monkeypatch.setattr(bench, "_make_pipeline", lambda model, device, **config: FakePipe())
    monkeypatch.setattr(bench, "_make_gen_config", lambda n: _DummyGenConfig())
    return monkeypatch


def test_cli_benchmark_prints_report(bench_cli):
    result = CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"])
    assert result.exit_code == cli.EXIT_OK, result.output
    # 报告在 stdout，进度在 stderr（`result.output` 是两者混合，别拿它做格式断言）
    assert "nputr benchmark" in result.stdout
    assert "CPU" in result.stdout


def test_cli_benchmark_progress_goes_to_stderr(bench_cli):
    result = CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"])
    assert "载入模型" in result.stderr


def test_cli_benchmark_json_flag(bench_cli):
    result = CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1", "--bench-json"])
    assert result.exit_code == cli.EXIT_OK
    data = json.loads(result.stdout)
    assert data["devices"][0]["device"] == "CPU"


def test_cli_benchmark_writes_to_output_file(bench_cli, tmp_path):
    """`-o` 是**额外存档**：文件要写，stdout 也照样有报告（与翻译模式不同）。"""
    out = tmp_path / "bench.json"
    result = CliRunner().invoke(cli._build_command(),
                                ["-b", "--bench-repeats", "1", "--bench-json", "-o", str(out)])
    assert result.exit_code == cli.EXIT_OK
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["devices"]
    assert json.loads(result.stdout)["devices"]


def test_cli_benchmark_all_devices_fail_is_model_error(monkeypatch, bench_cli):
    def _boom(model, device, **config):
        raise RuntimeError("无可用设备")

    monkeypatch.setattr(bench, "_make_pipeline", _boom)
    result = CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"])
    assert result.exit_code == cli.EXIT_MODEL


def test_cli_benchmark_ignores_stdin(bench_cli):
    """基准不该读 stdin —— 否则在无输入的终端里会一直挂着等。"""
    result = CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"], input="")
    assert result.exit_code == cli.EXIT_OK
    assert "nputr benchmark" in result.stdout


def test_cli_still_translates_when_no_benchmark(monkeypatch):
    """回归：`-b` 没给时必须走翻译路径，不能因为加了分支就走错。"""
    called: list[str] = []

    def _fake_run(text, file, opts):
        called.append("translate")
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_run_translate", _fake_run)
    result = CliRunner().invoke(cli._build_command(), ["hello", "--to", "en"])
    assert result.exit_code == cli.EXIT_OK
    assert called == ["translate"]


def test_cli_runs_all_devices_by_default(monkeypatch, bench_cli):
    """没写 `-d` → 全部设备（跨平台对比的默认姿势）；写了 `-d cpu` → 只测 CPU。

    判据是"用户是否真的在命令行写了这个选项"，不是取值 —— 两者的默认值都是 npu。
    """
    seen: list[str] = []

    def _rec(preferred="auto", manager=None):
        seen.append(preferred)
        return ["CPU"]

    monkeypatch.setattr(bench, "select_devices", _rec)
    CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"])
    CliRunner().invoke(cli._build_command(), ["-b", "-d", "cpu", "--bench-repeats", "1"])
    assert seen == ["auto", "cpu"]


def test_cli_target_option_selects_prompt_set(monkeypatch, bench_cli):
    """`-b --to ja` 按该语向生成 prompt；没给 `--to` 用固定混合集（跨机器可比）。"""
    seen: list[str | None] = []
    real = bench.build_prompts

    def _rec(target=None):
        seen.append(target)
        return real(target)

    monkeypatch.setattr(bench, "build_prompts", _rec)
    CliRunner().invoke(cli._build_command(), ["-b", "--bench-repeats", "1"])
    CliRunner().invoke(cli._build_command(), ["-b", "--to", "ja", "--bench-repeats", "1"])
    assert seen == [None, "ja"]
