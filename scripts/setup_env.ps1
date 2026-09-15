# 一键搭建运行环境（Windows PowerShell）
# 用法： .\scripts\setup_env.ps1 [-SkipModel] [-Mirror <pip源>]
param(
    [switch]$SkipModel,
    [string]$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# 1. venv
Write-Step "创建虚拟环境 .venv"
if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "venv 创建失败，请确认 Python 3.11 可用" }
} else {
    Write-Host "    已存在，跳过"
}
$py = ".\.venv\Scripts\python.exe"

# 2. 校验 Python 版本
Write-Step "校验 Python 版本"
$v = & $py -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "    Python $v"
if ($v -ne "3.11") { Write-Host "    警告：实测组合是 3.11，其他版本不保证 wheel 齐全" -ForegroundColor Yellow }

# 3. 依赖
Write-Step "安装运行时依赖"
& $py -m pip install --upgrade pip -i $Mirror
& $py -m pip install -r requirements.txt -i $Mirror
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

# 3b. 可编辑安装：让 python -m npu_translator.cli 与 npu-translate 命令可直接用
Write-Step "可编辑安装 npu_translator"
& $py -m pip install -e . --no-deps -i $Mirror

# 4. 模型
if (-not $SkipModel) {
    Write-Step "下载模型（INT4, 1.02 GB）"
    & $py scripts\fetch_model.py --repo rainhenry/HY-MT1.5-1.8B-int4-ov-npu
    if ($LASTEXITCODE -ne 0) { throw "模型下载失败" }
} else {
    Write-Host "`n==> 跳过模型下载（--SkipModel）" -ForegroundColor Yellow
}

# 5. 依赖完整性自检
#    cryptography 曾长期漏在 requirements.txt 之外：缺了它 nputweb 起不来，
#    但 pip install 并不报错，问题会一直拖到运行时才炸 —— 这里提前炸掉。
Write-Step "校验 nputweb 依赖完整性"
& $py -c "import cryptography, fastapi, uvicorn; print('    cryptography', cryptography.__version__)"
if ($LASTEXITCODE -ne 0) { throw "nputweb 依赖不完整（缺 cryptography / fastapi / uvicorn），请检查 requirements.txt" }

# 6. 设备自检 + 预热
Write-Step "设备自检"
& $py -m npu_translator.cli devices

Write-Step "完成"
Write-Host @"

下一步：
  python -m npu_translator.cli translate "今天天气不错" --to en
  pytest tests/ -q
"@
