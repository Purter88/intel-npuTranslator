# Intel NPU 本地多语言翻译程序

完全离线的本地翻译工具，用 **Intel NPU（AI Boost）** 跑 **HY-MT1.5-1.8B（INT4）**，
支持 **38 种语言**互译（33 主流 + 5 民族语/方言），**数据不出本机**（默认只绑回环地址；
用 `--host 0.0.0.0` 开放局域网访问时，流量按你自己的网络走）。

| | |
|---|---|
| 当前状态 | M0 / M1 / M2 ✅ · **nputweb（Web 界面）✅** · **快速部署 ✅** · **M3 `/v1/*` 服务 ✅**（功耗实测、长文本端到端未做）· M4 ⬜ |
| 实测吞吐 | NPU **32.4 tok/s** · CPU **46–54 tok/s** · 单测 **300 项**全过 |
| 主要平台 | Windows 11 + Intel Core Ultra（NPU4） |
| 授权 | **MIT** |

> **没有 Intel NPU 也能用。** 实测 CPU 反而比 NPU 快约 40%（46 vs 32 tok/s）——
> NPU 是项目的初心与主推理设备，但绝不是门槛。详见下方「实测性能」。

---

## 目录

- [文档导航](#文档导航)
- [实测性能](#实测性能)
- [快速开始](#快速开始)
  - [方式一：一键部署（推荐）](#方式一一键部署推荐)
  - [方式二：手动三步](#方式二手动三步)
- [命令行翻译 nputr](#命令行翻译-nputr)
- [Web 界面 nputweb](#web-界面-nputweb)
- [对外服务 nputserve](#对外服务-nputserve)
- [性能基准 `-b`](#性能基准--b)
- [项目状态与路线图](#项目状态与路线图)
- [平台支持](#平台支持)
- [换模型](#换模型)
- [Python API](#python-api)
- [CLI 选项参考](#cli-选项参考)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [注意事项](#注意事项)
- [开发](#开发)
- [License](#license)

---

## 文档导航

| 文档 | 给谁看 | 说明 |
|---|---|---|
| **`README.md`**（本文件） | 使用者 | 上手、用法、常见问题 |
| **[`docs/SPEC.md`](docs/SPEC.md)** | 开发者 / AI 助手 | **技术契约，唯一事实来源**。任何与它冲突的实现，先改它再改代码 |

> `docs/SPEC.md` 是**活**契约：动手前先读它，与它冲突的实现先改它再改代码。
> 代码注释里的规格书引用一律指回 `docs/SPEC.md`（写法见 §0.1），不指向任何别处。

---

## 实测性能

### 单设备解码（本机 Core Ultra 9 275HX / NPU4）

M0 基线（2026-09-11）：

| 设备 | 解码吞吐 | 首字延迟 TTFT | 首次编译 | 缓存后加载 |
|---|---|---|---|---|
| **NPU（默认）** | **32.4 tok/s** | 0.59 s | 约 30 s | **约 4 s** |
| GPU（Intel iGPU） | 28.1 tok/s | 0.19 s | 28 s | — |
| **CPU** | **46.2 tok/s** 🥇 | **0.15 s** 🥇 | 3 s | — |

2026-09-13 用 `nputr -b` 重测两轮（同一台机器、同一份配置）：

| 设备 | 加载 | 第 1 轮 | 第 2 轮 | TTFT | 波动 |
|---|---|---|---|---|---|
| **CPU** | 2.1 s | **54.1 tok/s** | **45.6 tok/s** | 0.13–0.15 s | ±16% |
| NPU（默认） | 3.7–4.2 s | 32.5 tok/s | 32.7 tok/s | 0.59 s | **±0.8%** |
| GPU（Intel iGPU） | 7.8–8.4 s | 37.6 tok/s | 25.4 tok/s | 0.14–0.19 s | ±24% |

三点值得注意：

- **CPU 最快，NPU 最稳。** CPU 吞吐是 NPU 的 1.4–1.7 倍、TTFT 快 4 倍；但 NPU 波动只有 ±0.8%
  （独占算力，不与显示/系统抢资源），CPU/GPU 是 ±16% / ±24%。
- **别拿单轮数据下结论。** 本机噪声真实水平是 **±20–30%**，不是常见的 ±10%。
  任何 1.2× 以内的"加速比"在单轮数据上都不成立。
- **NPU 慢 30–40% 是事实**，选它的理由是功耗与占用（功耗实测尚未做）。

### 整批翻译吞吐（6 段 × 96 tokens，2026-09-12）

| 模式 | 耗时 | 吞吐 | 内存峰值 |
|---|---|---|---|
| NPU 单干 | 8.43 s | 95.9 字符/s | 1.87 GB |
| CPU 单干 | 5.78 s | 140.0 字符/s | 2.20 GB |
| **hetero（NPU+CPU 并行）** | **4.84 s** | **167.0 字符/s** | 3.93 GB |

`--device hetero` 比最快的单设备快 **1.19×**，代价是多付约 1.7 GB 内存。
只建议在"翻大批文档、内存无所谓"时手动开启，**不是默认路径**。

---

## 快速开始

### 方式一：一键部署（推荐）

一条命令建环境、装依赖、下模型、生成 `nputr` / `nputweb` / `nputserve` 三个命令：

```powershell
python scripts\build.py                 # 交互式（会问要不要下模型）
python scripts\build.py --skip-model    # 非交互，跳过模型
python scripts\build.py --list-models   # 看可选模型
```

跑完它会打印一个目录路径，**自己把这个目录加进 PATH**（脚本不碰注册表，不需要管理员）：

```powershell
[Environment]::SetEnvironmentVariable('Path',
  [Environment]::GetEnvironmentVariable('Path','User') + ';' + '<打印出来的目录>', 'User')
```

> ⚠️ **不要用 `setx`** —— 它会把 `%USERPROFILE%` 这类变量展平成死字符串，还会 1024 字符截断。
> 加完重开终端，用 `nputr --help` / `nputweb --help` / `nputserve --help` 验证。

其他选项：`--python <解释器>`（默认自动找 3.11）· `--model <key>` · `--no-deps`（只重建命令脚本）· `--clean`（删掉生成的 `bin\`）。

### 方式二：手动三步

**1. 建环境**

```powershell
# 用 Python 3.11 建 venv（3.11 的 wheel 最全）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 开发（跑测试）再加：`pip install -r requirements-dev.txt`

**2. 下模型**

```powershell
.\.venv\Scripts\python.exe scripts\fetch_model.py --repo rainhenry/HY-MT1.5-1.8B-int4-ov-npu
```

约 1.02 GB，ModelScope 源，耗时约 1–2 分钟。该权重已 INT4 量化且 NPU 就绪，**无需本地导出**。

**3. 跑**

```powershell
.\.venv\Scripts\python.exe -m npu_translator.cli "今天天气不错" --to en
```

---

## 命令行翻译 nputr

命令名是 **`nputr`**（长名 `npu-translate`）。

```powershell
# 直译
nputr "今天天气不错" --to en

# 从文件读，结果写到文件（推荐，见「Windows 上的编码坑」）
nputr --to ja -i input.txt -o output.txt

# 管道进
Get-Content a.txt -Encoding UTF8 -Raw | nputr --to en

# 逐行翻译（日志 / CSV / 字幕，保证行数 1:1 对齐）
nputr --to en -i app.log --newline hard -o app.en.log

# 查看语种 / 设备
nputr --languages
nputr --devices
```

**stdout 只有译文**，诊断、进度、耗时、警告一律走 stderr，可以安全接管道。

---

## Web 界面 nputweb

独立命令 **`nputweb`**，一键启动浏览器界面。**默认 HTTPS + 自签证书**，只绑回环地址。

```powershell
nputweb                                  # 默认 https://127.0.0.1:8765，自动开浏览器
nputweb --port 9000                      # 换端口
nputweb --host 0.0.0.0                   # 允许局域网访问（自动生成 token 并打印）
nputweb --tls off                        # 明文（仅回环地址可用，跨机需再加 --allow-insecure）
nputweb --tls on --cert my.pem --key my.key   # 用你自己的证书
```

启动后会打印：本地/局域网地址、证书指纹（请与首次核对，防中间人）、设备与回退链。

> 理念 **安全性 > 稳定性 > 效率**：非回环绑定或启用 TLS 一律强制 token；
> 「非回环 + 明文」默认**拒绝启动**（退出码 3）。详见 `SPEC.md · WebUI（nputweb）`。

### nputweb 的环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NPT_WEB_HOST` | `127.0.0.1` | 非回环会强制 token |
| `NPT_WEB_PORT` | `8765` | 被占用直接报错（不会悄悄换端口） |
| `NPT_WEB_TLS` | `auto` | `auto` 自签 / `on` 自带证书 / `off` 明文 |
| `NPT_WEB_CERT` / `NPT_WEB_KEY` | — | `--tls on` 时用 |
| `NPT_WEB_TOKEN` | 自动生成 | 不指定则随机生成并打印一次 |
| `NPT_WEB_NO_AUTH` | off | 关闭鉴权（仅回环地址允许） |
| `NPT_WEB_ALLOW_INSECURE` | off | 放行「非回环 + 明文」 |
| `NPT_WEB_OPEN` | on | 设 `0` 不自动开浏览器 |
| `NPT_WEB_MAX_INPUT_CHARS` | `5000` | 单次输入字符上限（超出 413） |
| `NPT_WEB_TIMEOUT` | `120` | 单请求超时秒数（超时 504） |
| `NPT_WEB_QUEUE` | `8` | 队列上限（超出 503） |
| `NPT_WEB_RATE` | `30` | 单 IP 每分钟请求上限（超出 429） |

命令行参数优先级高于环境变量。

---

## 对外服务 nputserve

独立命令 **`nputserve`**，只挂 `/v1/*` 的**程序化**接口 —— 没有页面，是给脚本 / 第三方程序调的。
默认 HTTPS + 自签证书，只绑回环地址。**默认端口 8766**，与 `nputweb` 的 8765 错开
（两个命令**会**同时起，端口撞车会让第二个启动失败）。

```powershell
nputserve                                    # 默认 https://127.0.0.1:8766
nputserve --port 9001                        # 换端口
nputserve --host 0.0.0.0                     # 允许局域网访问（自动生成 token 并打印）
nputserve --tls off                          # 明文（仅回环地址可用，跨机需再加 --allow-insecure）
nputserve --debug                            # 开 /docs /redoc 与 access log
```

只有四个端点：

| 端点 | 说明 |
|---|---|
| `POST /v1/translate` | 翻译一段文本（请求体与 nputweb 的 `/api/translate` 同形）|
| `POST /v1/translate/stream` | 流式翻译（`text/event-stream`，**不分段**）|
| `GET /v1/languages` | 支持的 38 个语种 |
| `GET /v1/health` | 健康与排队状态（引擎加载中会显示 `loading → ready`）|

调用示例（`--tls off` 时把 `https` 换成 `http`、去掉 `-k`）：

```powershell
curl.exe -k https://127.0.0.1:8766/v1/health
curl.exe -k https://127.0.0.1:8766/v1/translate -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"text":"今天天气不错","target":"en"}'
```

> 请求体字段：`text`（必填）· `target` · `source` · `newline` · `strict` · `max_new_tokens`。
> `/v1/openapi.json` 默认开放（给第三方程序读），`/docs` / `/redoc` 默认**关闭**，要 `--debug`。
> ⚠️ PowerShell 5.1 的 `curl` 是 `Invoke-WebRequest` 的别名，所以上面写的是 `curl.exe`。

与 `nputweb` 是**两个独立进程**：各自独立端口、独立 token、独立限流桶，崩一个不影响另一个。
安全策略（非回环强制 token、「非回环 + 明文」默认拒绝启动）与 nputweb 一致，
详见 `SPEC.md · WebUI（nputweb）`。

### nputserve 的环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NPT_SERVE_HOST` | `127.0.0.1` | 非回环会强制 token |
| `NPT_SERVE_PORT` | `8766` | 与 nputweb 的 8765 错开；被占用直接报错 |
| `NPT_SERVE_TLS` | `auto` | `auto` 自签 / `on` 自带证书 / `off` 明文 |
| `NPT_SERVE_CERT` / `NPT_SERVE_KEY` | — | `--tls on` 时用 |
| `NPT_SERVE_TOKEN` | 自动生成 | 不指定则随机生成并打印一次 |
| `NPT_SERVE_NO_AUTH` | off | 关闭鉴权（仅回环地址允许） |
| `NPT_SERVE_ALLOW_INSECURE` | off | 放行「非回环 + 明文」 |
| `NPT_SERVE_DEBUG` | off | 开 `/docs` / `/redoc` 与 access log |

命令行参数优先级高于环境变量。

---

## 性能基准 `-b`

一条命令把本机所有能用的 OpenVINO 设备都测一遍，**换机器也能直接横向比**
（报告自带环境指纹：OS / CPU 核数 / Python / OpenVINO / NPU 架构·驱动·tiles）。

```powershell
nputr -b                                      # 自动挑设备：NPU → Intel iGPU → CPU
nputr -b -d cpu --cpu-threads 8               # 只测 CPU（可配合 --cpu-threads 做线程扫描）
nputr -b --to ja                              # 按指定语向生成 prompt
nputr -b --bench-json -o docs/bench_cli.json  # JSON 存档（跨机器比对用这个）
```

输出示例：

```
设备              加载s      预热s     tok/s          区间    TTFT s      总耗时s   tokens    峰值RSS MB
---------------------------------------------------------------------------------------------------------
NPU            3.68     1.34     32.70     31.5~34.3     0.590     1.178     20.0        1873
GPU.0          7.81     0.96     25.39     20.6~31.1     0.187     0.985     20.7        1989
CPU            2.12     2.14     45.56     33.7~55.2     0.152     0.586     20.0        3472

吞吐最快: CPU 45.56 tok/s
⚠️ GPU.0 波动 20.6~31.1 tok/s（>20%），单次结果不可信，多跑几轮再下结论
```

要点：

- **`-b` 不读 stdin、不翻译**；报告打到 stdout，`-o` 是**额外存档**（不像翻译模式那样顶替 stdout）。
- **区间列是重点**：只看平均值会被骗。波动 >20% 会直接报警。
- **`--bench-tokens` 在 NPU 上别超过 256**（`MIN_RESPONSE_LEN`），否则超窗被静默截断，代码会警告。
- 串行测多设备时 RSS 是**累加的**（前一台内存不立刻归还），单设备常驻请单独 `-d` 跑。

---

## 项目状态与路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | NPU 跑通 + 三设备基准 | ✅ |
| M1 | CLI（`nputr`）：分段、换行三档、编码契约、退出码 | ✅ |
| M2 | 异构并行（`hetero`）、LRU 缓存、长文本分段 | ✅ |
| — | **nputweb（Web 界面）**：独立命令、TLS 三态、原生前端 | ✅ |
| — | **`orchestrate.py` 共用编排层**（CLI / WebUI / 未来的 service 共用一份） | ✅ |
| — | **`--model` 换模型 + `scripts/build.py` 快速部署** | ✅ |
| M3 | **对外 HTTP 服务 `/v1/*`**（`service.py` + `server.py` + `nputserve` 命令）**已交付**；功耗实测、长文本端到端**未做** | 🟡 **部分完成** |
| M4 | 术语表、文档翻译、剪贴板划词、60 句质量回归集自动化 | ⬜ 未开工 |

### 已知的开放问题

- **默认设备选 NPU 还是 CPU**（🔴 未拍板）：纯性能 CPU 赢，纯稳定性 NPU 赢，**只差功耗实测**这一维度。
  在此之前默认 NPU（项目初心 + 省电）。
- **跨平台（Linux / ARM）**：可行性评审已定稿，**13 条决策全部待拍板**，卡在两条问题上：
  「有没有具体的人/场景非要 Linux 不可」与「ARM 目标硬件是哪一档」。
  结论见 `SPEC.md · 跨平台（Linux / ARM）可行性评审结论`。
- **翻译质量**：`en→ko` 寒暄句会失真（模型能力问题）；逐行翻日志时模型会把 `WARN`/`ERROR` 改写成 `INFO`。

---

## 平台支持

> ⚠️ **「可安装」≠「支持」**：只有 ✅ 两行是实测过的。标 🟡 / 🔴 的平台仅保证**能装上、代码路径能跑通**，
> **性能与稳定性均未验证**，请勿用于生产。

| 平台 | 状态 | 说明 |
|---|---|---|
| **Windows 11 x86_64 + Intel NPU** | ✅ **主要平台** | 全部实测数据来自这里（Core Ultra 9 275HX / NPU4） |
| **Windows x86_64 CPU** | ✅ **可用** | 不需要 NPU，且**比 NPU 快**（46–54 tok/s，Windows 实测） |
| Linux x86_64（NPU） | ⬜ 官方支持，未实测 | 需内核 ≥6.6（`intel_vpu`）+ `intel-fw-npu` / `intel-level-zero-npu` / `intel-driver-compiler-npu` + `libze1` + 用户入 `render` 组 |
| Linux x86_64（CPU） | 🟡 代码路径已验证，尚未正式支持 | 零代码改动即可跑通（双向翻译正确、退出码正确、编码无损）；**性能未实测**，是否列为 v1 目标待拍板 |
| **ARM64（CPU）** | 🔴 **实验性 · 未验证性能** | ⚠️ OpenVINO 在 ARM 上以**浮点仿真**执行量化模型：同一份 INT4 权重 x86 常驻 2,169 MB，**ARM 常驻 7,961 MB**。→ **建议内存 ≥16 GB，4/8 GB 板子出局** |
| macOS / Apple Silicon | ❌ 不支持 | 无计划 |

> ⚠️ **NVIDIA 独显用不上**：OpenVINO 能枚举到它（列为 `GPU.1`），但**不走 CUDA 后端**，`device.py` 已按 vendor 过滤。

---

## 换模型

默认模型是 HY-MT1.5-1.8B 的 INT4 NPU 权重。换模型有两条路，**`--model` 优先于环境变量**：

**① `--model / -m`（推荐，单次生效）**

```powershell
nputr -m Qwen3-1.7B-int4-ov "今天天气不错" --to en   # models/ 下的目录名
nputr -m X:/models/Qwen3-1.7B-int4-ov "..." --to en  # 或任意路径
nputr -b -m Qwen3-1.7B-int4-ov                       # 基准也能指定模型
```

解析规则：**纯名字**（不含分隔符、非绝对）当作 `models/` 下的子目录；否则按路径原样用。
路径不存在 → 退出码 **2**，并列出 `models/` 下可用的名字。

**② 环境变量（适合长期切换 / 脚本）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `NPT_MODEL_DIR` | `models` | 模型所在目录（相对项目根；给绝对路径也行） |
| `NPT_MODEL` | `HY-MT1.5-1.8B-int4-ov-npu` | 该目录下的**子目录名**（也可直接给绝对路径） |

最终路径 = `NPT_MODEL_DIR` / `NPT_MODEL`。这两个变量在 `config.py` **导入时**读取，
所以必须在启动 `nputr` 之前设好，同一进程里没法中途换。

### 可用模型

下表体积与量化参数为 **2026-09-15 查阅 ModelScope / HF 页面**所得，随仓库更新会变；标 `—` 的表示当时页面没给。

**A. 翻译专用（HY-MT 系列，与本项目的 prompt / 语种表匹配）**

| 模型 | 仓库 ID | 源 | 体积 | 说明 |
|---|---|---|---|---|
| **HY-MT1.5-1.8B INT4（默认）** | `rainhenry/HY-MT1.5-1.8B-int4-ov-npu` | ModelScope | 1.02 GB | 已 INT4 量化、NPU 就绪，开箱即用 |
| HY-MT1.5-1.8B INT4（通用 OV） | `rainhenry/HY-MT1.5-1.8B-int4-ov` | ModelScope | 1.21 GB | 同一作者的通用版，未标 NPU 优化，建议 CPU/GPU |
| HY-MT1.5-1.8B 官方原版 | `Tencent-Hunyuan/HY-MT1.5-1.8B` | ModelScope（HF 上是 `tencent/HY-MT1.5-1.8B`） | — | 换量化方式时用来本地导出 |
| HY-MT1.5-1.8B FP8 | `Tencent-Hunyuan/HY-MT1.5-1.8B-FP8` | ModelScope | 2.05 GB | 官方量化版 |
| HY-MT1.5-1.8B GPTQ-Int4 | `Tencent-Hunyuan/HY-MT1.5-1.8B-GPTQ-Int4` | ModelScope | 1.34 GB | 官方量化版；GPTQ 需先转 OV IR 才能被 GenAI 加载 |
| HY-MT1.5-7B | `Tencent-Hunyuan/HY-MT1.5-7B` | ModelScope | ~15 GB | **NPU 吃不下**（NPU4 仅 13 TOPS），只建议 CPU/GPU |

官方合集（含全部变体）：<https://www.modelscope.cn/collections/Tencent-Hunyuan/HY-MT15>

**B. 通用 LLM（能加载，但翻译质量没有实测背书）**

| 模型 | 仓库 ID | 源 | 体积 | 量化 |
|---|---|---|---|---|
| Qwen3-1.7B INT4 | `OpenVINO/Qwen3-1.7B-int4-ov` | ModelScope / HF | 1.21 GB | INT4_ASYM，group 128，ratio 0.8 |
| Qwen3-1.7B INT4（NPU 版） | `zhaohb/Qwen3-1.7B-int4-sym-ov-npu` | ModelScope | 1.04 GB | INT4_SYM 通道量化（NPU 推荐格式） |
| Qwen3-4B INT4（NPU 版） | `zhaohb/Qwen3-4B-int4-sym-ov-npu` | ModelScope | 2.24 GB | INT4_SYM 通道量化 |
| Qwen3-8B INT4（NPU 版） | `zhaohb/Qwen3-8B-int4-sym-ov-npu` | ModelScope | ~4.5 GB | 体积与编译时间都上去了，本机 NPU4 不推荐 |

Intel 官方验证过的 NPU 模型合集：<https://huggingface.co/collections/OpenVINO/llms-optimized-for-npu>

> ⚠️ **换 B 类模型前先想清楚**：本项目是**围绕 HY-MT 写的**，三处强耦合——
> ① `prompt.py` 用 HY-MT 官方四套模板，通用 LLM 拿到的是**裸 prompt、没有 chat template**，
>   输出会夹带解释甚至自己接着往下编；
> ② `languages.py` 的 38 个语种名是按 HY-MT 调的（尤其「繁体中文」这个例外），换模型后未必还灵；
> ③ `postprocess.py` 的清洗规则也是按 HY-MT 的输出习惯写的。
> 结论：**B 类能跑起来，但译文质量无保障**——要用请先自己跑 `scripts\smoke_test.py` 验一遍。
>
> 硬件上还有一条：NPU 的静态形状 `MAX_PROMPT_LEN=512` / `MIN_RESPONSE_LEN=256` 是按 1.8B 调的
> 甜点值，换 4B/8B 要重调（`NPT_MAX_PROMPT_LEN` / `NPT_MIN_RESPONSE_LEN`）。

### 调用示例

**① 命令行**

```powershell
# 下载（repo 最后一段自动作为 models/ 下的目录名）
.\.venv\Scripts\python.exe scripts\fetch_model.py --repo zhaohb/Qwen3-1.7B-int4-sym-ov-npu

# 单次指定：--model（目录名 == repo 最后一段）
nputr -m Qwen3-1.7B-int4-sym-ov-npu "今天天气不错" --to en

# 模型放在项目外：直接给路径
nputr -m X:/models/Qwen3-1.7B-int4-sym-ov-npu "今天天气不错" --to en

# 从 HF 镜像拉（默认 modelscope）
.\.venv\Scripts\python.exe scripts\fetch_model.py --repo OpenVINO/Qwen3-1.7B-int4-ov --source hf
```

想让某个模型**长期**生效就用环境变量（`--model` 会覆盖它）：

```powershell
$env:NPT_MODEL = "Qwen3-1.7B-int4-sym-ov-npu"
nputr "今天天气不错" --to en

# 取消设置（没设过也不报错）
Remove-Item Env:NPT_MODEL, Env:NPT_MODEL_DIR -ErrorAction SilentlyContinue
```

> cmd.exe 用 `set NPT_MODEL=xxx`；PowerShell 7+ / bash 用 `NPT_MODEL=xxx nputr ...`。
> 模型目录本身被 `.gitignore` 排除（`models/*`），换模型不会污染仓库。

**② Python API**

```python
from npu_translator import get_engine

# ⚠️ get_engine 是进程级单例：**只有第一次调用的参数生效**
engine = get_engine(model_path=r"X:\models\Qwen3-1.7B-int4-sym-ov-npu", device="npu")
print(engine.translate("今天天气不错", target="en").text)
```

要在同一进程里用两个模型，绕开单例直接构造：

```python
from npu_translator.engine import TranslateEngine

eng = TranslateEngine(model_path=r"models\HY-MT1.5-1.8B-int4-ov", device="cpu")
```

⚠️ 别同时持有两个**已加载**的引擎：NPU 是单流设备，两条 pipeline 只会互相抢
（`hetero` 是唯一例外，代价是约 3.9 GB 内存）。

**③ 只要生成、不走本项目那套 prompt**

```python
import openvino_genai as ov_genai

pipe = ov_genai.LLMPipeline(
    r"models\Qwen3-1.7B-int4-ov", "NPU",
    MAX_PROMPT_LEN=512, MIN_RESPONSE_LEN=256,      # 见「注意事项」
    NPUW_CACHE_DIR=".npucache", GENERATE_HINT="BEST_PERF",
)
tok = pipe.get_tokenizer()
tok.set_chat_template(tok.chat_template)           # Qwen3 这类 instruct 模型需要
print(pipe.generate("把这句话翻译成英文：今天天气不错", max_new_tokens=128))
```

**④ 自己导出一个模型**

现成权重够用就别走这条——要装 torch / optimum-intel，且必须用独立 venv：

```powershell
python -m venv .venv-export
.\.venv-export\Scripts\python.exe -m pip install -r requirements-export.txt
.\.venv-export\Scripts\python.exe scripts\export_model.py --model tencent/HY-MT1.5-1.8B --group-size -1
```

导出落在 `models/HY-MT1.5-1.8B-int4-gq128-ov`，再设 `NPT_MODEL=HY-MT1.5-1.8B-int4-gq128-ov` 即可。
`--group-size -1` = 通道量化（NPU 官方推荐、性能好），`128` = 分组量化（精度好）。

---

## Python API

```python
from npu_translator import get_engine

engine = get_engine(device="npu")       # 进程级单例，惰性加载
result = engine.translate("这个翻译引擎已经可以在 NPU 上工作了。", target="en")
print(result.text, result.device, result.elapsed_s)

# 流式
for chunk in engine.stream("人工智能正在改变世界。", target="ja"):
    print(chunk, end="")
```

`import npu_translator` 不会拉起 OpenVINO 运行时（惰性设计，约 40 ms），
只有真正调用 `translate()` / `load()` 时才加载模型。

长文本与并行走 `segment.py` / `pool.py`：

```python
from npu_translator.pool import build_workers, TranslationPool
from npu_translator.segment import segment

plan = segment(long_text, mode="soft")
pool = TranslationPool(build_workers("hetero"))
pool.prepare()                                    # 并行预热
results = pool.run(plan.texts, target="en")       # 返回顺序 == 输入顺序
print(plan.join([r.text for r in results]))
```

---

## CLI 选项参考

| 选项 | 默认 | 说明 |
|---|---|---|
| `[TEXT]` | — | 待翻译文本；省略则读 `-i` 或 stdin |
| `-t/--to` | `en` | 目标语言代码（如 `en` / `ja` / `zh-Hant`） |
| `-f/--from` | `auto` | 源语言，`auto` 交给模型判断 |
| `-i/--file` | — | 从文件读 |
| `-o/--output` | stdout | 写到文件（UTF-8 无 BOM） |
| `--append` | off | 追加模式 |
| `--bom` | off | 输出写 UTF-8 BOM（与 `--append` 互斥） |
| `-m/--model` | 默认模型 | 指定模型：`models/` 下的目录名，或任意路径。优先于 `NPT_MODEL` |
| `-d/--device` | `npu` | `npu` / `cpu` / `gpu` / `auto` / `hetero` |
| `--cpu-threads` | `0` | `0`=OpenVINO 自动；`half`；正整数 |
| `--cpu-core-type` | `any` | `any` / `pcore` / `ecore`，原样透传 |
| `--cpu-ht` | 默认 | `on` / `off` 超线程 |
| `--cpu-priority` | `normal` | `idle` / `below` / `normal`（⚠️ 改的是整个进程） |
| `--newline` | `soft` | 换行策略，见下 |
| `--lines` | off | `--newline hard` 的别名 |
| `--no-segment` | off | 强制单段（超 KV cache 会被静默截断） |
| `--stream` | off | 流式输出（与分段互斥） |
| `-q/--quiet` | off | 静音 stderr（错误除外） |
| `--strict` | off | 部分失败视为整体失败 |
| `-v/--verbose` | off | 耗时 / 设备 / 分段信息打到 stderr |
| `--input-encoding` | 自动 | 强制输入编码 |
| `--max-input-mb` | `64` | 输入体积上限 |
| `--no-cache` | off | 禁用译文 LRU 缓存 |
| `--no-warmup` | off | 跳过预热 |
| `-b/--benchmark` | off | **跑性能基准而不是翻译** |
| `--bench-repeats` | `3` | 每个 prompt 重复次数 |
| `--bench-tokens` | `128` | 每次生成的 token 上限（NPU 上 >256 会被静默截断） |
| `--bench-json` | off | 报告输出 JSON |
| `--bench-no-warmup` | off | 跳过预热那一次（数字含首次编译） |

### 换行策略 `--newline`

| 模式 | 语义 | 适用 | 风险 |
|---|---|---|---|
| `soft`（默认） | 换行不丢，但不强制在换行处断句 | 散文、普通文档 | 长行会被拆开再拼回 |
| `hard` | 每行独立翻译，**行数 1:1 对齐** | 日志、列表、CSV、字幕 | ⚠️ 会把硬折行的散文从中间劈开，语法破碎、指代丢失 |
| `auto` | 启发式判断硬边界后合并 | 混排文档 | ⚠️ 会判断错，可能译坏 |

> 实测提醒：`hard` 逐行翻日志时，模型把 `WARN` / `ERROR` 都改写成 `INFO` 了
> （逐行丢失上下文，级别词被"顺手修正"）。**日志场景要么人工校对，要么用 `soft`。**

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（**含空输入**） |
| 1 | 输入错误（`--strict` 下的空输入也算） |
| 2 | 参数错误 |
| 3 | 模型加载失败 / `--strict` 下的整体失败 |
| 4 | **部分失败**：有段未译出，原文保留，stdout 仍有输出 |
| 120 | 下游关闭管道（`| head` / `| more`），已静默处理 |
| 130 | Ctrl+C 中断 |

---

## 目录结构

```
src/npu_translator/
├── config.py        # 所有常量集中处（可用 NPT_* 环境变量覆盖）
├── device.py        # 设备探测与回退链 NPU → GPU(Intel) → CPU
├── engine.py        # LLMPipeline 封装：单例、串行锁、流式、回退
├── languages.py     # 38 语种表（33+5）
├── prompt.py        # HY-MT 官方 prompt 模板
├── postprocess.py   # 译文清洗（去解释性前缀、去包裹引号）
├── segment.py       # 长文本分段 + 换行三档（NPU 的 KV cache 只有 768）
├── encoding.py      # 编码归一（BOM / cp936 / BrokenPipe / 字节写）
├── cache.py         # 译文 LRU（key 不含设备，避免同一段两种译文）
├── pool.py          # 异构调度：动态派活 + 有序重组 + 降级链
├── benchmark.py     # 跨平台性能基准（-b）：环境指纹 + TTFT/tok/s/RSS + 波动区间
├── orchestrate.py   # ★ 共用编排层：CLI / WebUI / 未来的 service 共用一份
├── cli.py           # 命令行入口（nputr）
├── service.py       # nputserve 的适配层：请求校验 → 调 orchestrate → 组装响应（不 import fastapi）
├── server.py        # nputserve 入口：只挂 /v1/* 的独立命令（默认端口 8766）
└── web/             # nputweb：Web 界面子包（独立命令 nputweb）
```

---

## 常见问题

**没有 Intel NPU 能用吗？**
能，而且更快。CPU 实测 46–54 tok/s、TTFT 0.15 s，比 NPU 的 32.4 tok/s / 0.59 s 都好。
NPU 的优势是**稳定**（波动 ±0.8%）与功耗，不是速度。用 `-d cpu` 或直接设默认设备。

**第一次为什么这么慢？**
NPU 图首次编译约 30 s。之后命中 `.npucache` 只需约 4 s。缓存与驱动版本强绑定，升级驱动后会自动重建。

**我的 NVIDIA 独显能用吗？**
不能。OpenVINO 能枚举到它（列为 `GPU.1`），但不走 CUDA 后端，`device.py` 已按 vendor 过滤掉。

**繁体中文出不来？**
必须用 `zh-Hant`，且 prompt 里的语言名是中文「繁体中文」——英文 `Traditional Chinese` 模型不认，
会直接回吐英文原文。这是实测结论，详见 `SPEC.md · Prompt 与语言`。

**数据会上传到云端吗？**
不会。模型权重、推理、译文缓存全在本机，运行时零外链零 CDN。

**翻译结果只出了一半 / 退出码 4？**
长文本被分段后某段没译出，原文保留。用 `--strict` 可让它变成整体失败（退出码 3）。

**进程译完了不退出？**
译文输出后 OpenVINO 仍留有 daemon 线程，关停阶段可能卡住。程序默认在 flush 后硬退出。
排查用 `NPT_EXIT_DEBUG=1`（打印残留线程），对比排查用 `NPT_HARD_EXIT=0`。

**为什么 `nputr > out.txt` 出来的文件是乱码？**
PowerShell 5.1 的 `>` / `>>` 会产出 **UTF-16LE**。用 `nputr -o out.txt` 代替，或换 PS 7+ / cmd.exe。

---

## 注意事项

- **首次使用会编译 NPU 图**，约 30 s；之后命中 `.npucache` 只需约 4 s。缓存与驱动版本强绑定，升级驱动后会自动重建。
- **NPU 是单流设备**，引擎内部已用全局锁串行化推理，不要自己开并发，也不会更快。
- **长文本必须先分段**（`segment.py`），单段超过 KV cache 窗口会被静默截断。
- **繁体中文**必须用 `zh-Hant`。
- `hetero` 模式常驻约 **3.9 GB**（两条 pipeline），单设备约 2 GB。
- **进程退出走硬退出**（flush 后 `os._exit`）：见上方「进程译完了不退出」。
- **预热限时** `NPT_WARMUP_TIMEOUT`（默认 300 s，`0` = 不限）：NPU 被其它进程占用时
   不会无限等下去，超时直接报错而不是干等。
- **nputweb 的自签证书**落在 `~/.nputweb/`（**不入库**），同一批绑定地址会复用同一份证书，
   否则浏览器每次重启都要重新点信任。首次启动请把终端里打印的 **SHA-256 指纹**记下来核对。

### Windows 上的编码坑（已处理，但值得知道）

程序的编码**由自己全权控制**，不走 shell：

- **PowerShell 5.1 的 `>` / `>>` 会产出 UTF-16LE**，文件里全是 `FF FE 41 00 42 00...`。
  → 用 `nputr -o out.txt` 而不是重定向；或者在 PS 7+ / cmd.exe 里重定向。
- **cp936 控制台**：即使管道接走，Python 的 stdout 仍是 gbk，缅甸语 / 阿拉伯语 / 藏语
  一输出就 `UnicodeEncodeError`。→ 入口强制 UTF-8，输出直接写字节。
- **stdin 同理**：UTF-8 日文喂进来会 `UnicodeDecodeError`，更糟的是某些字节会被
  cp936 **静默解成乱码**。→ 走字节 → BOM 探测 → utf-8 → cp936，全失败才报错（绝不静默替换）。
- `| head` / `| more` 提前关管道：退出码 120，且**不会往 stderr 喷 traceback**。
- **nputweb 的私钥权限**：自签私钥的"600"在 Windows 上是尽力而为
  （`os.chmod` 对 NTFS 基本无效），实际保护来自用户配置目录本身的 ACL。

---

## 开发

```powershell
pytest tests/ -q                                    # 单元测试 300 项（不含模型，秒级）
.\.venv\Scripts\python.exe scripts\smoke_test.py --device NPU   # 冒烟：5 语种 × 3 句
.\.venv\Scripts\python.exe scripts\bench.py --all               # 三设备基准
.\.venv\Scripts\python.exe scripts\bench_hetero.py              # 异构并行基准 + 线程扫描
nputr -b                                                        # ★ 跨平台性能基准（推荐）
```

**动手前请先读 [`docs/SPEC.md`](docs/SPEC.md)** —— 它是技术契约与唯一事实来源，
里面记了实测基线、踩坑记录、活跃风险和待拍板的决策。与它冲突的实现，先改它再改代码。

---

## License

**MIT**。依赖的 OpenVINO 组件遵循其各自的许可，详见
[OpenVINO 仓库](https://github.com/openvinotoolkit/openvino)。
