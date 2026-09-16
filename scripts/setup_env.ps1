# 一键搭建运行环境（Windows PowerShell）
# 用法： .\scripts\setup_env.ps1 [-SkipModel] [-Mirror <pip源>]
param(
    [switch]$SkipModel,
    [string]$Mirror = "",            # 留空 = 自动探测（见 Resolve-Mirror）
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# pip 源候选：逐个探测，第一个真正能下载的胜出。
#
# 为什么不再写死清华源：本脚本原来默认 `-i https://pypi.tuna.tsinghua.edu.cn/simple`，
# 而清华源在这台机上**直接返回 HTTP 403**（实测：阿里云 / 腾讯云 / 华为云 / PyPI 官方
# 都是 200，只有清华 403）。源挂掉最阴的地方在于「依赖都装过时 pip 压根不联网」——
# 一眼看全是 already satisfied，问题一直拖到 `pip install -e .`：PEP 517 构建隔离
# 要联网拉 setuptools>=68，源不可达就整条构建失败。所以这里逐个探测，
# 别把命运押在单一镜像上。
$Mirrors = @(
    @{ Name = "阿里云";    Url = "https://mirrors.aliyun.com/pypi/simple" },
    @{ Name = "腾讯云";    Url = "https://mirrors.cloud.tencent.com/pypi/simple" },
    @{ Name = "华为云";    Url = "https://mirrors.huaweicloud.com/repository/pypi/simple" },
    @{ Name = "清华";      Url = "https://pypi.tuna.tsinghua.edu.cn/simple" },
    @{ Name = "PyPI 官方"; Url = "https://pypi.org/simple" }
)

function Resolve-Mirror([string]$Explicit) {
    if ($Explicit) { Write-Host "    使用命令行指定的 pip 源：$Explicit"; return $Explicit }
    Write-Step "探测 pip 源"
    foreach ($m in $Mirrors) {
        try {
            Invoke-WebRequest -Uri ($m.Url.TrimEnd('/') + "/setuptools/") -TimeoutSec 8 -UseBasicParsing | Out-Null
            Write-Host "    命中 $($m.Name)：$($m.Url)"
            return $m.Url
        } catch {
            Write-Host "    不可用，跳过 $($m.Name) $($m.Url) -> $($_.Exception.Message)"
        }
    }
    Write-Host "    全部不通，转离线模式：只能复用 venv 里已经装好的东西" -ForegroundColor Yellow
    return ""
}

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
$Mirror = Resolve-Mirror $Mirror
$mirrorArgs = @()
if ($Mirror) { $mirrorArgs = @('-i', $Mirror) }
# 刻意**不**自动升级 pip：升级 pip 对"程序能不能跑起来"没有任何帮助，
# 而 pip 卸载旧版本的那一段被中断（超时 / 关窗口）时，会把 site-packages 里
# 若干包的 .py 一起清空 —— 本机踩过，代价远超收益。真要升自己跑一遍。
& $py -m pip install @mirrorArgs -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

# 3b. 可编辑安装：让 python -m npu_translator.cli 与三个命令可直接用
#
# 两档：先走标准的 PEP 517 构建隔离（干净，但**必须联网**拉 setuptools>=68）；
# 它挂了就退回 --no-build-isolation —— 用 venv 自带的 setuptools，
# 前提是 venv 里有 setuptools>=68 和 wheel，所以先补装再重试。
Write-Step "可编辑安装 npu_translator"
& $py -m pip install @mirrorArgs -e . --no-deps
if ($LASTEXITCODE -ne 0) {
    Write-Host "    构建隔离走不通，改用 venv 自带的构建后端重试" -ForegroundColor Yellow
    & $py -m pip install @mirrorArgs --upgrade "setuptools>=68" wheel
    & $py -m pip install @mirrorArgs -e . --no-deps --no-build-isolation
    if ($LASTEXITCODE -ne 0) { throw "可编辑安装失败" }
}

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
