"""快速部署：建 venv -> 装依赖 -> 选模型 -> 生成命令转发脚本。

用法::

    python scripts/build.py                   # 交互（默认会问要不要下载模型）
    python scripts/build.py --skip-model      # 非交互，跳过模型
    python scripts/build.py --model HY-MT1.5-1.8B-int4-ov-npu
    python scripts/build.py --list-models
    python scripts/build.py --python C:\\Python311\\python.exe
    python scripts/build.py --clean           # 只删 bin\\（不动 venv 与模型）

产出 ``bin\\nputr.cmd``、``bin\\nputweb.cmd`` 与 ``bin\\nputserve.cmd`` 三个转发脚本。

**本脚本不修改系统 PATH**，只打印提示由用户自己添加 —— 不碰注册表，
也就不需要管理员权限、不会有改坏 PATH 的风险（User PATH 是 REG_EXPAND_SZ，
用 setx 改会把 ``%USERPROFILE%`` 这类变量展平成死字符串）。

两条硬约定（改之前先想清楚）：
1. **只导出 nputr / nputweb / nputserve 三个命令**。pyproject 里还有 ``npu-translate`` 别名，
   它仍存在于 ``.venv\\Scripts`` 但**不进 PATH**（见 SPEC.md · CLI 管道契约）。
2. **保留 editable install**：它提供 console script 与 dist-info 元数据，
   是 `pip list` / `importlib.metadata` 能认到本项目的唯一来源。
   转发脚本另外设 ``PYTHONPATH`` 作为兜底 —— sys.path 里 PYTHONPATH 排在
   ``.pth`` 之前，所以即使整个目录被搬走，新路径也会优先于 editable 里写死的旧绝对路径。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
BIN_DIR = ROOT / "bin"
REQUIREMENTS = ROOT / "requirements.txt"
FETCH_MODEL = ROOT / "scripts" / "fetch_model.py"
MODEL_DIR = ROOT / "models"

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
WANT_PY = (3, 11)


# ------------------------------------------------------------------ 配置


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    size: str
    desc: str
    source: str = "modelscope"


# 与 config.py 对齐：模型落在 models/<key>/，用 NPT_MODEL 或 --model <key> 切换。
# 以后加模型只改这个列表，交互菜单与 --list-models 会自动跟上。
MODELS: list[ModelSpec] = [
    ModelSpec(
        key="HY-MT1.5-1.8B-int4-ov-npu",
        repo="rainhenry/HY-MT1.5-1.8B-int4-ov-npu",
        size="1.02 GB",
        desc="INT4 已量化，NPU 直接可用（推荐）",
    ),
]

# (命令名, 模块路径)。只放三个 —— 见文件头第 1 条约定。
COMMANDS: list[tuple[str, str]] = [
    ("nputr", "npu_translator.cli"),
    ("nputweb", "npu_translator.web.cli"),
    ("nputserve", "npu_translator.server"),
]

# .cmd 必须全 ASCII：cmd.exe 按 GBK 解析批处理，中文注释会乱码。
# CRLF 换行；setlocal/endlocal 避免污染调用者的环境变量。
SHIM_TEMPLATE = (
    "@echo off\r\n"
    "setlocal\r\n"
    'set "NPT_ROOT=%~dp0.."\r\n'
    'set "PYTHONPATH=%NPT_ROOT%\\src;%PYTHONPATH%"\r\n'
    '"%NPT_ROOT%\\.venv\\Scripts\\python.exe" -m {module} %*\r\n'
    'set "NPT_RC=%errorlevel%"\r\n'
    "endlocal & exit /b %NPT_RC%\r\n"
)


# ------------------------------------------------------------------ 工具


def _stdout_utf8() -> None:
    """Windows 控制台默认是 GBK，print 中文会 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def info(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    info(f"\n==> {msg}")


def die(msg: str) -> NoReturn:
    info(f"\n[FAIL] {msg}")
    raise SystemExit(1)


def run(cmd: list[str], *, cwd: Path | None = None) -> int:
    info(f"    $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd or ROOT)).returncode


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


# ------------------------------------------------------------------ 步骤


def pick_interpreter(wanted: str | None) -> str:
    """挑一个用来建 venv 的解释器；优先 3.11（实测组合）。"""
    candidates = [wanted] if wanted else [shutil.which("python"), shutil.which("py"), shutil.which("python3")]
    for c in candidates:
        if not c:
            continue
        try:
            out = subprocess.run(
                [c, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        if out.returncode == 0:
            ver = out.stdout.strip()
            mark = " (匹配 3.11)" if ver == f"{WANT_PY[0]}.{WANT_PY[1]}" else ""
            info(f"    解释器 {c} -> Python {ver}{mark}")
            return c
    die("找不到可用的 Python 解释器，用 --python 指定一个")


def ensure_venv(python: str) -> Path:
    vp = venv_python()
    if vp.is_file():
        info(f"    已存在，跳过：{vp}")
        return vp
    step("创建虚拟环境 .venv")
    if run([python, "-m", "venv", str(VENV_DIR)]) != 0:
        die("venv 创建失败")
    if not venv_python().is_file():
        die(f"venv 建了但找不到 {venv_python()}")
    return venv_python()


def install_deps(vp: Path, upgrade_pip: bool) -> None:
    step("安装运行时依赖")
    # 默认**不**升级 pip：本机踩过「pip install 被中断后清空 site-packages」的事故，
    # 而升级 pip 对"程序能不能跑起来"没有任何帮助。真要升用 --upgrade-pip。
    if upgrade_pip:
        if run([vp, "-m", "pip", "install", "--upgrade", "pip", "-i", PIP_MIRROR]) != 0:
            die("pip 升级失败")
    if run([vp, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-i", PIP_MIRROR]) != 0:
        die("依赖安装失败")
    # editable install：提供 console script 与 dist-info 元数据（见文件头第 2 条约定）
    step("可编辑安装 npu_translator")
    if run([vp, "-m", "pip", "install", "-e", ".", "--no-deps", "-i", PIP_MIRROR]) != 0:
        die("可编辑安装失败")


def model_present(key: str) -> bool:
    """权重主体是否已在 models/<key>/ 下（不 import 项目代码，避免拉起 OpenVINO）。"""
    return (MODEL_DIR / key / "openvino_model.bin").is_file()


def choose_model(args: argparse.Namespace) -> ModelSpec | None:
    """返回要下载的模型；None 表示跳过。"""
    if args.skip_model:
        return None
    if args.model:
        spec = next((m for m in MODELS if m.key == args.model), None)
        if spec is None:
            known = ", ".join(m.key for m in MODELS) or "(空)"
            die(f"未知模型 '{args.model}'。可用：{known}")
        return spec

    # 交互：列出清单 + 跳过项
    info("\n可选模型：")
    for i, m in enumerate(MODELS, start=1):
        flag = " [已下载]" if model_present(m.key) else ""
        info(f"  [{i}] {m.key}   {m.size}   {m.desc}{flag}")
    info("  [0] 跳过下载")

    if not sys.stdin.isatty():
        info("\n非交互环境，默认跳过模型下载（用 --model <key> 指定）")
        return None

    while True:
        try:
            raw = input("\n请选择 [0]: ").strip()
        except EOFError:
            return None
        if raw in ("", "0"):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(MODELS):
            return MODELS[int(raw) - 1]
        info(f"    请输入 0 - {len(MODELS)}")


def download_model(vp: Path, spec: ModelSpec) -> None:
    if model_present(spec.key):
        info(f"    已存在，跳过：{MODEL_DIR / spec.key}")
        return
    step(f"下载模型 {spec.key}（{spec.size}）")
    if run(
        [vp, str(FETCH_MODEL), "--repo", spec.repo, "--local-dir", spec.key, "--source", spec.source]
    ) != 0:
        die(f"模型下载失败：{spec.repo}")
    if not model_present(spec.key):
        die(f"下载完成但没找到 {MODEL_DIR / spec.key / 'openvino_model.bin'}")


def write_shims() -> None:
    step(f"生成命令转发脚本到 {BIN_DIR}")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name, module in COMMANDS:
        target = BIN_DIR / f"{name}.cmd"
        # newline="" 保证 \r\n 原样落盘，不被再转成 \r\r\n
        with open(target, "w", encoding="ascii", newline="") as fh:
            fh.write(SHIM_TEMPLATE.format(module=module))
        info(f"    {target}")


def smoke(vp: Path) -> None:
    # nputweb 与 nputserve 的依赖集合相同（都是 fastapi / uvicorn / cryptography），
    # 一次 import 同时校验两个命令，报错信息里两个名字都要点出来。
    step("校验 nputweb / nputserve 依赖完整性")
    if run([vp, "-c", "import cryptography, fastapi, uvicorn"]) != 0:
        die("nputweb / nputserve 依赖不完整（cryptography / fastapi / uvicorn）")

    step("校验转发脚本可执行")
    for name, _ in COMMANDS:
        shim = BIN_DIR / f"{name}.cmd"
        r = subprocess.run(
            [str(shim), "--help"],
            cwd=str(ROOT), shell=True,
            capture_output=True, text=True, errors="replace",
        )
        if r.returncode != 0:
            info((r.stdout or "")[-2000:])
            info((r.stderr or "")[-2000:])
            die(f"{shim.name} 跑不起来（退出码 {r.returncode}），检查 {VENV_DIR} 是否完整")
        info(f"    {name}.cmd OK")


def print_path_hint() -> None:
    here = str(BIN_DIR)
    ps = (
        "[Environment]::SetEnvironmentVariable("
        "'Path', "
        "[Environment]::GetEnvironmentVariable('Path','User') + ';' + "
        f"'{here}', 'User')"
    )
    step("最后一步：把下面这个目录加进 PATH（只加这一个目录）")
    info(f"\n    {here}\n")
    info("PowerShell（当前用户，不需要管理员）：")
    info(f"    {ps}")
    info("\n或者：设置 -> 系统 -> 高级系统设置 -> 环境变量 -> 用户变量 Path -> 新建")
    info("\n注意：不要用 setx —— 它会把 %USERPROFILE% 这类变量展平成死字符串。")
    info("加完重开终端，验证：")
    for name, _ in COMMANDS:
        info(f"    {name} --help")


# ------------------------------------------------------------------ main


def main() -> int:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description="快速部署 nputr / nputweb / nputserve 到本机")
    ap.add_argument("--python", default=None, help="用来建 venv 的解释器（默认自动找 3.11）")
    ap.add_argument("--model", default=None, help="直接指定模型 key（--list-models 查看）")
    ap.add_argument("--skip-model", action="store_true", help="跳过模型下载")
    ap.add_argument("--list-models", action="store_true", help="列出可用模型后退出")
    ap.add_argument("--no-deps", action="store_true", help="跳过依赖安装（只重建转发脚本）")
    ap.add_argument("--upgrade-pip", action="store_true", help="顺带升级 pip（默认不升，见 install_deps 注释）")
    ap.add_argument("--clean", action="store_true", help="删除 bin\\ 后退出（不动 venv 与模型）")
    args = ap.parse_args()

    if args.list_models:
        for m in MODELS:
            flag = "已下载" if model_present(m.key) else "未下载"
            info(f"{m.key}\t{m.size}\t{m.desc}\t[{flag}]\t{m.repo}")
        return 0

    if args.clean:
        if BIN_DIR.is_dir():
            shutil.rmtree(BIN_DIR)
            info(f"已删除 {BIN_DIR}")
        else:
            info("bin\\ 不存在，无需清理")
        return 0

    info(f"项目根目录：{ROOT}")

    python = pick_interpreter(args.python)
    vp = ensure_venv(python)
    if not args.no_deps:
        install_deps(vp, args.upgrade_pip)

    spec = choose_model(args)
    if spec:
        download_model(vp, spec)
    else:
        info("\n跳过模型下载（之后可用 --model <key> 补下）")

    write_shims()
    smoke(vp)
    info("\n[OK] 部署完成")
    print_path_hint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
