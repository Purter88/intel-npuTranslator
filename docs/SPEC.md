# SPEC.md — Intel NPU 本地多语言翻译程序 · 技术契约

> **本文件是本项目唯一的技术契约**，是外部贡献者唯一需要读的规格书。
> 代码注释里的规格引用一律指回本文件 —— 引用格式见 §0.1。
>
> 最近更新：**2026-09-15**　|　状态：**M0–M2 已完成；nputweb 已交付；nputserve（M3 服务层）已交付**

---

## 0. 地位与稳定性契约

### 0.1 本文件怎么用（改代码前必读）

1. **先读本文件再动手**。与本文冲突的实现，必须先回来改本文，再改代码。
2. **引用格式固定为 `SPEC.md · <章名>`**，其中 `<章名>` 只能是本文件的
   **一级或二级标题原文**（标题前面那个 `N.` 序号**不属于**章名）。

   | 写法 | 判定 | 说明 |
   |---|---|---|
   | `SPEC.md · CLI 管道契约` | ✅ | 二级标题原文 |
   | `SPEC.md · 已知问题与活跃风险 · NPU 被其他进程占用时生成永久挂起` | ✅ | 第二段是**三级标题**，给人看的额外精度，**不参与**校验 |
   | `SPEC.md · CLI 管道契约 的语义` | ❌ | 章名后面追加了说明文字 → 它就不再是标题，断言会判死链 |
   | `SPEC.md · 活跃风险 R13` | ❌ | 编号会漂，且本文件不为编号建章节 |

   🔴 **章名后面一律不得追加说明文字**。需要更细的落点就再开一个三级标题，
   用描述性短句命名（见「已知问题与活跃风险」的写法），不要让引用本身变长。
3. **一律用章名，不用编号**：`§` / `R13` / `D8` / 章节序号都会随结构调整漂移。
   编号撞号会立刻被发现，**语义重复不会** —— 它会潜伏到拍板那天，
   然后同一个问题拍出两个矛盾的结论。
4. **范围判据**：「外部贡献者不知道它，会不会写出一个能过 review 但会踩坑的 PR？」
   会 → 进本文件；不会 → 不写进任何文档，让它留在 PR 讨论里。
   **本文件记决策规则，不记决策过程。**

### 0.2 地位

| 文档 | 位置 | 内容 |
|---|---|---|
| `docs/SPEC.md` | 本文件 | **技术契约**：约束是什么、为什么这么定 |
| `README.md` | 仓库根 | 用户向快速上手 |

### 0.3 稳定性契约（什么东西不许随便动）

**稳定（改动需要回来改本文件，并说明影响面）**

| 项 | 约束 |
|---|---|
| CLI 的 stdout 语义与退出码 | stdout **只有译文**；退出码表见「CLI 管道契约」 |
| `/api/*` 与 `/v1/*` 的错误体形状 | 一律 `{"error": {"code": ..., "message": ...}}`，**不回传 traceback** |
| Prompt 模板 | 严格照抄官方，见「Prompt 与语言」 |
| 语种表与 `prompt_name` | 38 语种；`zh-Hant` 的 prompt 名必须是中文名 |
| 锁定的版本组合 | 见「环境与版本」，勿轻易动 |
| 安全基线 | Host 白名单 / 鉴权 / 限流 / TLS 三态，见「WebUI（nputweb）」与「服务（nputserve）/v1 API 契约」 |
| `import` 不拉起重依赖 | `import npu_translator` 不得拉起 OpenVINO；`import npu_translator.web` 不得拉起 fastapi |

**会变（数字与环境绑定，跨机器不可外推）**

- 所有实测吞吐 / TTFT / 内存数字 —— 见「实测基线」的读法说明，**单次基准不可信**。
- 默认推理设备 —— 待功耗实测拍板，见「已知问题与活跃风险 · CPU 更快导致默认设备缺性能理由」。

### 0.4 一句话现状

**NPU 已跑通，32 tok/s 完全可用；但参考机的 CPU（46 tok/s）反而更快 —— 架构仍按「NPU 优先 + CPU/GPU 回退」做，默认设备的最终取舍待功耗实测。**

---

## 1. 目标与非目标

**目标**：完全离线的本地翻译程序 —— Intel NPU（AI Boost）主推理设备、支持 38 种语言互译、数据不出本机。

**非目标（v1 不做）**：云端级吞吐（NPU 是单流串行设备）；语音/图片翻译；模型微调/训练。

---

## 2. 环境与版本

### 2.1 参考机实测（2026-09-11）

> 这是**测基线用的那一台**，不是最低要求。跨机器不可外推，见「已知问题与活跃风险 · 跨机器默认值不可外推」。

| 项 | 实测值 | 影响 |
|---|---|---|
| CPU | Core Ultra 9 275HX（Arrow Lake-HX，8P+16E / 24T，AVX-VNNI） | CPU 回退路径很强 |
| **NPU** | Intel AI Boost = **NPU4 @ 13 TOPS INT8**；arch **3720**；驱动 **32.0.100.5540**；**`NPU_MAX_TILES=2`** | 不是 Lunar Lake 的 48 TOPS。算力受限是核心约束 |
| iGPU | Intel Graphics（Xe-LPG 4 核）drv 32.0.101.8331 → `GPU.0` | 可作 GPU 回退，但无价值（28 tok/s 且编译 28 s） |
| dGPU | NVIDIA RTX 5060 Laptop → `GPU.1` | **OpenVINO 不用它**：能枚举但不支持 CUDA 后端 |
| 内存 / 系统 | 32 GB DDR5-6400 / Win11 专业工作站版 build 26200 | 1.8B INT4（1.02 GB）绰绰有余 |
| 运行时 | Python **3.11.9**（`.venv`）；git 2.53.0 | 3.11 的 cp311 win wheel 最全 |
| 网络 | 国内环境 | HF 直连不稳 → 走 **ModelScope / hf-mirror.com** |

### 2.2 锁定的版本组合（勿轻易动）

```
openvino==2026.3.1
openvino-genai==2026.3.1.0
openvino-tokenizers==2026.3.1.0
numpy 2.4.6      # 实测配套版本
```

### 2.3 环境搭建与复现命令

```powershell
cd Intel-NPU-Translator          # 项目根；路径一律用相对写法，禁止写死本机目录

# 1) 建环境（用 py 启动器锁定 3.11，别用裸 python）
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 源拉不动就换一个：-i <镜像地址>

# 2) 设备探测
.\.venv\Scripts\python.exe scripts\probe_device.py

# 3) 下载模型（ModelScope，约 1.02 GB / 1–2 分钟）
.\.venv\Scripts\python.exe scripts\fetch_model.py --repo rainhenry/HY-MT1.5-1.8B-int4-ov-npu

# 4) 基准 / 冒烟 / 单测
.\.venv\Scripts\python.exe scripts\bench.py --all --out docs\bench_all.json
.\.venv\Scripts\python.exe scripts\bench_hetero.py
.\.venv\Scripts\python.exe scripts\smoke_test.py --device NPU
.\.venv\Scripts\python.exe -m pytest -q        # 不加载模型，秒级

# 5) 性能基准（跨平台，见「实测基线 · -b 基准实测」）
nputr -b                                     # 全部可用设备，报告上 stdout
nputr -b --bench-json -o docs/bench_cli.json # 额外存档 JSON（带环境指纹）
```

> 开发依赖：`pip install -r requirements-dev.txt`。
> 模型导出（换模型/换量化时才需要）用独立 venv：`requirements-export.txt` + `scripts\export_model.py`。

### 2.4 技术栈（选定路线）

| 层 | 选择 |
|---|---|
| 推理引擎 | **OpenVINO Runtime 2026.3.1**（Intel NPU 官方唯一成熟路径） |
| 生成式封装 | **openvino-genai 2026.3.1.0**（内置 tokenizer、KV-cache、采样、流式） |
| 量化 | **INT4 对称**（`--sym --weight-format int4 --ratio 1.0`）——**当前直接用现成权重，不本地导出** |
| 模型 | **HY-MT1.5-1.8B**（33 语言 + 5 民族语/方言，decoder-only，已在 GenAI 官方支持列表） |
| 服务层 | FastAPI + uvicorn（nputweb 与 nputserve 共用） |
| 前端 | **纯静态 HTML + 原生 JS/CSS**（无框架 / 无构建 / 无 CDN）—— **不用模板引擎**（venv 无 jinja2） |

### 2.5 已排除的方案（别重走）

> ⚠️ **作用域声明**：本表的排除结论默认在 **Windows + NPU** 语境下成立。
> 换平台（Linux / ARM）需重新评估 —— 例：NLLB-200 在 ARM CPU 上未必还是死路。
> 详见「跨平台（Linux / ARM）可行性评审结论」。

| 方案 | 排除理由 |
|---|---|
| **llama.cpp / GGUF** | 其 OpenVINO backend 走通用算子路径，**不走 Intel 为 LLM 优化的 NPUW 静态图**，社区实测在 NPU 上比 CPU 还慢 |
| **NLLB-200**（200 语种） | seq2seq 架构，NPU 上**无官方支持路径**（GenAI 的 NPU 管道只验证 decoder-only 与 Whisper）；且双阶段 + cross-attention 难静态化。若将来要补长尾语种，走 **CPU/GPU + NLLB-600M** 单独通道 |
| **HY-MT1.5-7B** | 13 TOPS NPU 吃力，内存与编译时间高。仅 CPU/GPU 备选 |
| **Qwen3-1.7B / 4B** | GenAI 支持，但翻译非专精 |
| **OPUS-MT** | 逐语对一个模型，不适合「多语言」 |
| **NF4 量化** | 官方声明仅 Core Ultra Series 2（Lunar Lake）及以后；参考机是 NPU4 且 `MAX_TILES=2`，不值得赌 |
| **GPU（iGPU）进 hetero** | 实测 28 tok/s、编译 28 s，无价值。2026-09-13 重测 37.6 / 25.4 tok/s（**波动 ±24%**）—— 数字本身不足以翻案，但 iGPU 与显示/系统抢带宽导致**方差极大**，调度上不可预测 → 结论不变（不进 hetero） |
| **本地 optimum-cli 导出** | 现成 INT4 权重跨版本可用（验证环境 2025.4.1 → 运行 2026.3.1 直接可用），导出链路只在换模型时才走 |

### 2.6 参考链接

- OpenVINO GenAI on NPU 官方指南：https://docs.openvino.ai/2025/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html
- OpenVINO GenAI 支持模型列表（含 HunYuanDenseV1ForCausalLM）：https://openvinotoolkit.github.io/openvino.genai/docs/supported-models/
- OpenVINO 2026.3 Release Notes：https://docs.openvino.ai/2026/about-openvino/release-notes-openvino.html
- HY-MT1.5-1.8B 模型卡（含 prompt 模板）：https://huggingface.co/tencent/HY-MT1.5-1.8B
- 已验证 NPU 权重（ModelScope）：`rainhenry/HY-MT1.5-1.8B-int4-ov-npu`
- NPUW 静态形状与 KV cache 原理解析：https://blog.csdn.net/OpenVINOCC/article/details/159945049

---

## 3. 架构与目录结构

### 3.1 分层

```
┌──────────────────────────────────────────────────────────┐
│  UI 层        Web UI (默认) / 桌面壳 / 剪贴板划词 / CLI   │
└───────────────────────┬──────────────────────────────────┘
                        │ HTTP (localhost) 或直接调用
┌───────────────────────▼──────────────────────────────────┐
│  API 层       nputweb: /api/*     nputserve: /v1/*        │
├──────────────────────────────────────────────────────────┤
│  编排层       orchestrate.Translator（唯一一份编排）       │
│   语言校验 → 切分 → 逐段翻译 → 后处理 → 拼接 → LRU 缓存   │
├──────────────────────────────────────────────────────────┤
│  引擎层       TranslateEngine（LLMPipeline 封装）          │
│   单例 · 全局串行锁 · NPUW 编译缓存 · 流式 · 设备回退      │
├──────────────────────────────────────────────────────────┤
│  设备层       OpenVINO Runtime → NPU / GPU / CPU          │
└──────────────────────────────────────────────────────────┘
```

### 3.2 目录结构

```
Intel-NPU-Translator/
├── README.md                # 用户向快速上手
├── docs/                    # 基准与冒烟实测数据（入库，决策依据）
│   └── SPEC.md              # 本文件（公开技术契约）
├── requirements.txt / -dev.txt / -export.txt
├── src/npu_translator/
│   ├── __init__.py          # 公共 API + 惰性导出（import 不拉起 OpenVINO）
│   ├── config.py            # 常量集中处（全部可用 NPT_* 环境变量覆盖）
│   ├── device.py            # 设备探测与回退链（NPU → GPU(Intel) → CPU），按 vendor 过滤 dGPU
│   ├── engine.py            # LLMPipeline 封装：单例、串行锁、流式、预热、回退
│   ├── languages.py         # 38 语种表（33+5）+ 中文显示名 + prompt_name
│   ├── prompt.py            # HY-MT prompt 模板
│   ├── segment.py           # 长文本分段 + 换行三档（soft/hard/auto）+ 行结构保真
│   ├── postprocess.py       # 去解释性前缀 / 去包裹引号 / 去代码围栏
│   ├── encoding.py          # 编码归一（BOM 探测 / UTF-8 强制 / cp936 兜底 / BrokenPipe）
│   ├── cache.py             # LRU 译文缓存（key 不含设备）
│   ├── pool.py              # 异构调度：动态派活 + 有序重组 + 降级链
│   ├── benchmark.py         # 跨平台性能基准（-b）：环境指纹 + TTFT/tok/s/RSS
│   ├── cli.py               # 命令行入口（短名 nputr）
│   ├── orchestrate.py       # ✅ 共用编排层（CLI / WebUI / 服务 共用）
│   ├── web/                 # ✅ nputweb：WebUI 子包
│   │   ├── cli.py           #    nputweb 入口（独立命令）
│   │   ├── tls.py/auth.py/limits.py  # 证书三态 / 鉴权 / 限流（纯逻辑，可单测）
│   │   ├── app.py/routes.py #    FastAPI 工厂 + /api/* 路由
│   │   └── static/          #    index.html + app.js + style.css（原生，无构建）
│   ├── service.py           # ✅ nputserve 适配层：校验 + 调 Translator + 组装（**不 import fastapi**）
│   └── server.py            # ✅ nputserve：/v1/* 路由 + SSE + uvicorn（**唯一**在模块级 import fastapi 的文件）
├── scripts/
│   ├── build.py             # 快速部署：一键建 venv / 装依赖 / 选模型 / 生成转发脚本（不碰 PATH）
│   ├── fetch_model.py       # 模型下载（ModelScope / HF 镜像）
│   ├── export_model.py      # 导出 + INT4 量化（独立 venv，多数情况不需要）
│   ├── probe_device.py      # 设备探测
│   ├── bench.py             # 三设备基准
│   ├── bench_hetero.py      # 异构基准 + 线程扫描
│   └── smoke_test.py        # 冒烟：5 语种 × 3 句
├── tests/                   # 单元测试（纯逻辑，不加载模型，秒级）
├── models/                  # 本地模型（.gitignore）
└── .npucache/               # NPUW 编译缓存（.gitignore）
```

### 3.3 共用编排层 `orchestrate.py`

CLI / WebUI / 服务共用**一份**编排。抽它的理由：把「批内去重」「hard 打包行数校验」
「批回填」这类**踩过坑**的细节复制多份，必然各自漂移。

```
调用方：CLI / WebUI / service
   │  输入怎么来、输出怎么走、退出码怎么映射 —— 归调用方
   ▼
Translator（有状态：持有 worker 池 + 译文缓存）
   分段 → 查缓存 → 批内去重 → 池翻译 → 回填 → 打包校验 → 按行拼接
   ▼
Outcome（IO 无关，可直接 JSON 化）
```

- **有状态**是刻意的：`prepare()` 一次之后可反复 `translate()`，WebUI / 服务都是长生命周期；
  CLI 一次性用完即弃，行为不变。
- **惰性**：构造不碰模型，`prepare()` 才加载 —— 调用方要把它放进后台线程，和读输入 / 等 HTTP 请求并行。
- **进度**：`on_progress(done, total)` + `Translator.progress` 快照。单并发串行队列下，
  轮询一个共享快照就能拿到**真实**段进度，不必为此引入 SSE。
- **吞吐口径**：`Outcome.chars_per_second` 是**字符/s**，不是 token/s（原因见「实测基线 · 吞吐口径」）。
- **越界判断**：同一件"部分失败"，CLI 映射成退出码 4，WebUI / 服务映射成 200 + `failed` 计数
  （服务的 `strict=true` 才升 500）—— **映射权在调用方**。
- 🔴 **服务层不得重写编排**：分段 / 缓存 / 去重 / 拼接一律在 `orchestrate.Translator` 里。

---

## 4. CLI 管道契约

完整选项表见 `README.md`；本节只写**契约性、不看代码就会踩**的部分。基调：**稳定性优先于速度**。

### 4.1 硬约束

1. **stdout 只有译文**。诊断 / 进度 / 耗时 / 警告一律 stderr
   （**`-b` 基准模式是唯一例外**：此时没有译文，报告本身就是 stdout 的主产出）
2. **编码由程序全权控制**，不依赖 shell 重定向
3. **输出顺序 == 输入顺序**，并行也不例外
4. **单设备模式不加载第二条 pipeline**（`hetero` 才付约 4 GB 内存）
5. 输入优先级：**位置参数 `TEXT` > `--file` > stdin**
6. **`--model` 的校验必须早于「读输入」和「建池」**：
   目录不存在就在参数校验阶段退 2，不要把用户晾在读 stdin 上，更不要先跑一次 NPU 编译再报错

### 4.2 换行三档

见「NPU 实现要点 · 长文本与换行三档」。`--lines` 是 `--newline hard` 的别名。
**帮助菜单必须写明 hard / auto 的风险**（已写）。

### 4.3 编码契约

| 场景 | 实测结论 | 对策 |
|---|---|---|
| stdout（cp936 控制台） | 即使被管道接走，encoding 仍是 `gbk` → 缅甸语 / 阿拉伯语 / 藏语 `UnicodeEncodeError` | 入口 `reconfigure(encoding="utf-8", newline="\n")`，输出写 `sys.stdout.buffer` |
| stdin | 同样 `gbk`；UTF-8 日文喂进来 `UnicodeDecodeError`，部分字节序列会**静默 mojibake**（比崩溃更危险） | `sys.stdin.buffer.read()` → BOM 探测 → utf-8 → `--input-encoding` → 兜底 cp936；**绝不静默 replace** |
| BrokenPipe | 退出码 120，解释器关停时还会往 stderr 喷 `Exception ignored` | 捕获后把 stdout 重定向到 devnull 再静默退出 |
| PowerShell 5.1 `>` `>>` | 产出 **UTF-16LE**（`"ABC"` → `FF FE 41 00 42 00 43 00`） | 主推 `-o/--append`；README 明确警告 |
| cmd.exe `>` `>>` | 纯字节直写，**安全** | 可用 |
| PS 5.1 `$OutputEncoding` | `us-ascii`，`Get-Content f.txt \| nputr` 非 ASCII 会变 `?` | 用 `Get-Content -Encoding UTF8` 或 `<` 重定向 |

### 4.4 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（**含空输入**，管道友好） |
| 1 | 输入错误（`--strict` 下的空输入；`--max-input-mb` 超限） |
| 2 | 参数错误（typer 默认；**含 `--model` 指向的目录不存在**） |
| 3 | 模型加载失败（所有设备均失败） |
| 4 | **部分失败**：有段未译出，保留该段原文，stdout 仍有输出 |
| 120 | 下游关闭管道（`\| more`），已静默处理 |
| 130 | Ctrl+C 中断 |

> 基准模式复用同一套语义：**全部设备失败 → 3**；**部分设备失败 → 4**（报告里已标注失败原因）。

### 4.5 稳定性要求

1. **预热与读 stdin 并行**：后台线程预热（NPU 首次编译约 30 s），主线程同时读输入，stderr 打提示，避免 30 s 零输出被当成卡死
2. **有序重组**：并行必须保证输出顺序 == 输入顺序（index + 有序缓冲）
3. **Ctrl+C**：worker 线程被中断不得带走引擎全局锁（daemon 线程 + `finally` 释放）
4. **输入体积上限** `--max-input-mb`（默认 64），不能无脑 `stdin.read()`
5. **LRU key = (文本, 目标语言, 源语言, 术语)**，**不含设备** —— 否则同一段在 NPU / CPU 上命中不同缓存、给出不同译文
6. **批内去重**：`hard` 打包会把多行合成一个请求，重复行不再产生独立 key；且批量查缓存时缓存必然是空的（先查后翻）→ 必须做批内去重 + 翻完回填
7. **`--cpu-priority` 改的是整个进程**，会连带拖慢 NPU 宿主侧调度，默认 `normal`
8. **退出路径必须可控**：译文输出后进程里仍挂着 OpenVINO 的 daemon 线程，关停阶段会 join 它，
   卡住就永远退不出去 → flush 之后 `os._exit`（`NPT_HARD_EXIT=0` 可关）
9. **`main_entry` 与 `_cli_main` 必须分开**：前者会 `os._exit`，在 pytest / CliRunner 里调用会把
   测试进程一起带走。测试只能调 `_cli_main()` 或 `_build_command()`
10. **预热必须限时**（`NPT_WARMUP_TIMEOUT`，默认 300 s，`0` = 不限）：NPU 被占用时加载可能永不
    返回，表现为「零输出 + 永久挂起」，与卡死无法区分

### 4.6 `--benchmark` 基准模式

给 **OpenVINO 跨平台性能对比**用：一条命令把本机所有能用的设备都测一遍，
报告自带环境指纹（OS / CPU 核数 / Python / OpenVINO / NPU 架构·驱动·tiles），
换机器也能直接横向比。实现在 `benchmark.py`。

```powershell
nputr -b                                      # 自动挑设备（NPU → Intel iGPU → CPU）
nputr -b -d cpu --cpu-threads 8               # 只测 CPU，且带线程属性（线程扫描）
nputr -b --to ja                              # 按指定语向生成 prompt
nputr -b --bench-json -o docs/bench_cli.json  # JSON 存档（跨机器比对用这个）
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `-b/--benchmark` | off | 跑基准而不是翻译；**不读 stdin** |
| `--bench-repeats` | `3` | 每个 prompt 重复次数（`NPT_BENCH_REPEATS`） |
| `--bench-tokens` | `128` | 生成上限；**NPU 上 > `MIN_RESPONSE_LEN`(256) 会被静默截断**，代码会警告 |
| `--bench-json` | off | 输出 JSON（含逐次样本） |
| `--bench-no-warmup` | off | 跳过预热那一次（数字含首次编译，接近冷启动） |

契约性约定（不看代码会踩）：

1. **不复用 `TranslateEngine`** —— engine 有回退链，NPU 加载失败会静默落到 CPU，
   报告就把 CPU 的成绩标成了 NPU。基准必须**钉死设备**，直接 `LLMPipeline(path, device, **cfg)`。
2. **设备选择按 vendor 过滤**：NVIDIA dGPU 会被 OpenVINO 列成 `GPU.1` 但不可用。
3. **预热那一次不计入统计**：NPU 首次推理含编译（30 s vs 稳定 4 s），混进平均值会毁掉结论。
4. **单台设备失败不中断其余设备**：全挂 → 退出码 3；部分失败 → 4（报告里已标注失败原因）。
5. **`-o` 是额外存档**，不像翻译模式那样顶替 stdout —— 屏幕和文件都能拿到报告。
6. **默认 prompt 集与 `scripts/bench.py` 共用同一份**（`benchmark.DEFAULT_PROMPTS`），
   保证 M0 基线（`docs/bench_all.json`）与新数据同口径、可对比。
7. `-d hetero` 在基准里展开成逐个设备（要的是单设备成绩，不是并联那条管道）。

### 4.7 v1 不做

- `--glossary` / 术语干预（一旦启用就必须进 LRU key，否则串味；`CacheKey.glossary` 字段已预留）
- `--stream` 与分段同时启用（先做互斥并报错提示）
- GPU 进 hetero
- `ENABLE_CPU_PINNING` / `ENABLE_CPU_RESERVATION` / `MODEL_DISTRIBUTION_POLICY`（OpenVINO 确有，但跨机器行为不一致）

---

## 5. Prompt 与语言

### 5.1 四套模板（HY-MT1.5，严格照抄官方）

```python
# 中 → 外
ZH_XX = "将以下文本翻译为{target_language},注意只需要输出翻译后的结果,不要额外解释:\n\n{source_text}"

# 外 → 外（源非中文）
XX_XX = "Translate the following segment into {target_language}, without additional explanation.\n\n{source_text}"

# 术语干预
TERM = ("参考下面的翻译:{terminology} 翻译成 {terminology_target_language}\n"
        "将以下文本翻译为{target_language},注意只需要输出翻译后的结果,不要额外解释:\n{source_text}")

# 上下文感知
CTX = ("{context}\n参考上面的信息,把下面的文本翻译成{target_language},"
       "注意不需要翻译上文,也不要额外解释:\n{source_text}")
```

### 5.2 语言名的硬知识（必须用英文语言名）

- `{target_language}` 用语言名而非代码；**默认用英文名**（`English` / `Japanese`）
- 🔴 **唯一例外：繁体中文必须用中文名「繁体中文」**。实测同一句只改目标语言名：

| 写法 | 模型输出 |
|---|---|
| `Chinese` | 请让报告在明天上午之前发给我。（简体，正常） |
| `Traditional Chinese` | ❌ `Please send me this report by tomorrow morning.`（**回吐英文原文，未翻译**） |
| `Chinese Traditional` / `Chinese (Traditional)` | 简体 |
| **`繁体中文`** | ✅ `請在明天早上之前把報告發送給我。`（**唯一能出繁体的写法**） |

→ 代码里给 `Language` 加了 `prompt_name` 字段（`zh-Hant` 显式为「繁体中文」）。
取名字一律走 `Language.target_name` / `languages.target_name(code)`，**不要用 `en_name()`**。

**教训：语言名本质是软 prompt，换语种要实测，不要想当然套「用英文名」的规则。**

### 5.3 语种清单（38 = 33 主流 + 5 民族语/方言）

- **主流**：中文(zh) · **繁体中文(zh-Hant)** · English(en) · 日本語(ja) · 한국어(ko) · Français(fr) · Deutsch(de) · Español(es) · Português(pt) · Русский(ru) · العربية(ar) · Italiano(it) · Nederlands(nl) · Polski(pl) · Čeština(cs) · Türkçe(tr) · ไทย(th) · Tiếng Việt(vi) · Bahasa Indonesia(id) · Bahasa Melayu(ms) · Filipino/Tagalog(tl) · हिन्दी(hi) · বাংলা(bn) · தமிழ்(ta) · తెలుగు(te) · मराठी(mr) · ગુજરાતી(gu) · اردو(ur) · فارسی(fa) · עברית(he) · ខ្មែរ(km) · ဗမာ(my) · Українська(uk)
- **民族语/方言**：藏语(bo) · 哈萨克语(kk) · 蒙古语(mn) · 维吾尔语(ug) · 粤语(yue)

> ⚠️ `zh-Hant` 含大写 → `str.islower()` 为 False，**别用它判断「是否已是语言名」**，一律查语种表。
> UI 默认只展示最常用的约 20 种，其余折叠。

---

## 6. 模型

### 6.1 权重

- **ModelScope `rainhenry/HY-MT1.5-1.8B-int4-ov-npu`**，INT4，15 个文件，**1.02 GB**
- 落地路径 `models/HY-MT1.5-1.8B-int4-ov-npu`（`.gitignore`），NPU 侧编译在运行时由 NPUW 完成并落 `.npucache`
- 模型关键事实：1.8B 规模下官方指标超越 Tower-Plus-72B / Qwen3-32B，达 Gemini-3.0-Pro 约 90% 水位；支持**术语干预 / 上下文翻译 / 格式保留**；上下文 16K（但 NPU 上实际受 KV cache 静态形状限制）

### 6.2 静态形状甜点（必读）

| 配置 | TTFT | 吞吐 | KV cache 总容量 |
|---|---|---|---|
| `MAX_PROMPT_LEN=1024` + `MIN_RESPONSE_LEN=512` | 1.247 s | 29.83 tok/s | 1536 |
| **`512` + `256`（当前默认）** | **0.591 s** | **32.41 tok/s** | **768** |

**更小的静态形状同时赢下 TTFT（−53%）与吞吐（+9%）**，代价是 KV cache 只有 768。
代价可接受的前提：严格执行分段策略（单段 ≤256 token），绝不让单段 prompt 逼近上限。
若将来开启「上下文翻译」（前一段译文作提示），需重新评估（届时考虑 `768+256`）。

### 6.3 换模型（`--model`）

**管道原本就通**：`OrchestrateConfig.model_path` → `_default_pool_factory` →
`build_workers(model_path=)` → `TranslateEngine(model_path=)`；`benchmark.run_benchmark(model_path=)`
同样早已有之。CLI 只补了 **解析 / 路径解析 / 校验** 三层，
engine / pool / orchestrate **一行未动** —— 别再去这三处找换模型的开关。

**三条硬性约定（改这块前必读）**：

1. **解析规则唯一入口是 `config.resolve_model_path()`**：纯名字（不含分隔符、非绝对）
   → `MODEL_DIR / <名字>`；含分隔符或绝对路径 → 原样使用。
   这样 `--model Qwen3-1.7B-int4-ov` 与环境变量 `NPT_MODEL` 的语义完全对齐。
   路径拼接逻辑只许出现在这里。
2. **优先级**：`--model` > `NPT_MODEL` / `NPT_MODEL_DIR` > 内置默认。
   `--model` 未指定时向传层传 `None`，让下层自己回落 —— 不要传 `cfg.MODEL_PATH` 进去，
   否则"未指定"和"显式指定为默认值"就分不出来了。
3. **校验与退出码（不对称，但是刻意的）**：
   - **显式给了 `--model` 而目录不存在 → 退出码 2（参数错误）**，并列出 `models/` 下可用名字。
     理由：参数值写错，且能在加载前拦下，不必白等一次 NPU 编译。
   - **未指定 `--model` → 不校验**，失败语义（加载不起来 → 3）**保持原样**。

候选模型清单与调用示例见 `README.md · 换模型`；「环境与版本 · 已排除的方案」在这里依然适用
（NLLB-200 无 NPU 路径、HY-MT1.5-7B 在 13 TOPS 的 NPU4 上不现实、通用 LLM 缺 chat template）。

---

## 7. NPU 实现要点

### 7.1 流水线配置（必写）

```python
import openvino_genai as ov_genai

pipeline_config = {
    "MAX_PROMPT_LEN": 512,         # 静态 prefill 上限（甜点值，见「模型 · 静态形状甜点」）
    "MIN_RESPONSE_LEN": 256,       # 预留给生成的 token
    "NPUW_CACHE_DIR": ".npucache", # ★ 必加：编译结果落盘，二次启动秒开
    "GENERATE_HINT": "BEST_PERF",  # 首次编译慢一点换推理性能
}
pipe = ov_genai.LLMPipeline("models/HY-MT1.5-1.8B-int4-ov-npu", "NPU", **pipeline_config)
```

> `LLMPipeline(path, device, dict)` 的第三参已弃用，**必须写 `**cfg`**。

### 7.2 静态形状约束（NPU 与 CPU/GPU 最大的心智差异）

- NPU 用**静态形状**：KV cache 的 `seq_len` 在编译期固定为 `MAX_PROMPT_LEN + MIN_RESPONSE_LEN`
- 设得越大 → 编译越慢、内存越高；**不要盲目调到 2048+**
- 超窗会被**静默截断**（不报错）→ 长文本必须走分段
- OpenVINO ≥2025.3 支持 `NPUW_LLM_PREFILL_CHUNK_SIZE`（默认 1024）做准动态 prefill；`PREFILL_HINT=STATIC` 可关闭

### 7.3 首次编译慢 → 三板斧

1. `NPUW_CACHE_DIR` 落盘缓存（29.8 s → **4.0 s**）
2. 应用启动时**异步预热**（后台线程跑 dummy prompt），UI 显示「正在唤醒翻译引擎」
3. 首次使用向导明确告知「首次加载需要约 X 秒」

### 7.4 并发：NPU 是单流设备

- 必须全局串行化（`asyncio.Lock` 或线程池单 worker）；**并发不会更快，只会 OOM / 编译冲突**
- 长文本分段顺序执行即可
- `hetero`（NPU+CPU 双 pipeline）是唯一例外

### 7.5 生成参数（翻译场景）

```python
config = ov_genai.GenerationConfig()
config.max_new_tokens = min(2048, int(len(src_tokens) * 1.6) + 32)
config.temperature = 0.0      # 翻译求确定性（官方 README 的 0.7/0.6 是对话设置，不适用）
config.top_p = 1.0
config.do_sample = False      # greedy
config.repetition_penalty = 1.05
```

### 7.6 设备回退链

```
NPU ──编译/推理失败──► GPU(Intel iGPU) ──失败──► CPU
```

- 捕获 `RuntimeError` / `ov.Exception` 后自动降级，并在 `/v1/health` 暴露 `active_device`
- 显式 `device` 参数：`auto | npu | gpu | cpu | hetero`
- GPU 选择必须**按 vendor 过滤**：OpenVINO 会把 NVIDIA dGPU 列成 `GPU.1`，但它不支持 CUDA

### 7.7 长文本与换行三档

- 单段 prompt 上限受 `MAX_PROMPT_LEN` 约束 → **先切段再翻译**
- 切分粒度：按标点/换行分句，贪心合并到 **≤256 token/段**，不跨段落边界
- 无标点长文本（典型是中文）必须有**按固定长度硬切**的兜底，否则切不开会超窗被静默截断
- 输出按原结构拼接，保留空行与缩进

**`\n` 是一等边界，三档语义（CLI `--newline`）**：

| 档 | 语义 | 适用 | 风险 |
|---|---|---|---|
| `soft`（默认） | 换行不丢失，但不强制在换行处断句 | 散文、普通文档 | 长行会被拆开再拼回 |
| `hard` | 每行独立翻译，**行数 1:1 对齐**；空行原样透传不送模型 | 日志、列表、CSV、字幕 | ⚠️ 会把硬折行的散文从中间劈开，语法破碎、指代丢失 |
| `auto` | 启发式判断硬边界（上行以句末标点结尾、或下行以 `-`/`#`/数字/`*` 开头） | 混排文档 | ⚠️ 会判断错，可能译坏 |

`hard` 另有**打包快路径**：把 K 行打包成一个请求（总长 ≤ `SEGMENT_MAX_CHARS`），校验输出行数 == 输入行数，相等则采信（省掉每行一次 TTFT 0.59 s），不等则退回逐行重翻。

`join()` 按**段尾是否已有标点 / 是否 CJK** 决定是否加分隔符，中文段落不会被插多余空格。

---

## 8. 实测基线

数据落在 `docs/`（`bench_all.json` / `bench_npu_512.json` / `bench_hetero.json` / `smoke_result.json` / `bench_cli.json`）。

### 8.1 M0 三设备（2026-09-11，5 prompt × 3 设备，max_new_tokens=128）

| 设备 | 解码吞吐 | TTFT | 首次编译 | 缓存命中加载 |
|---|---|---|---|---|
| **NPU（512+256）** | **32.41 tok/s** | 0.591 s | 32.8 s | **4.0 s** ✅ |
| NPU（1024+512） | 29.83 tok/s | 1.247 s | 29.8 s | — |
| GPU（Intel iGPU） | 28.09 tok/s | 0.185 s | 28.3 s | — |
| **CPU** | **46.2 tok/s** 🥇 | **0.146 s** 🥇 | 2.9 s | — |

读法：**NPU 不是瓶颈**（目标 8 tok/s，实测 32）；**短板是 TTFT（0.59 s）而非吞吐**，短句体验 CPU 更跟手；**GPU 路径没有价值**，仅作回退保留。

### 8.2 M2 异构（2026-09-12，6 段 × max_new_tokens=96）

| 模式 | 推理耗时 | 吞吐 | 加载 | 峰值 RSS |
|---|---|---|---|---|
| NPU 单干 | 8.43 s | 95.9 字符/s | 7.05 s | 1873 MB |
| CPU 单干（`--cpu-threads 0`） | 5.78 s | 140.0 字符/s | 3.15 s | 2198 MB |
| **hetero 动态派活（LPT）** | **4.84 s** | **167.0 字符/s** | 4.82 s | **3931 MB** |

- 加速比 **1.193× vs 最快单设备（CPU）**，越过 1.15× 门槛（vs NPU 单干是 1.74×）
- **静态比例分配已证伪**：50/50 时 CPU 2.51 s 干完闲着，NPU 成短板，实测 4.24 s —— **比 CPU 单干慢 5%**。必须用动态派活（谁空闲谁领下一段，长段优先）
- ⚠️ **代价是内存 +78%**（3.93 GB）。19% 提速不值这个价 → **hetero 保持手动 opt-in，不进推荐路径**
- ⚠️ **本机噪声约 ±10%**（两次 `--threads 0` 分别 5.78 / 6.38 s）→ **1.193× 在噪声边缘，别当精确结论用**

### 8.3 CPU 线程扫描（只作调优建议，不做默认值）

| 线程数 | 0（自动） | 8 | 12 | 16 | 24 |
|---|---|---|---|---|---|
| 耗时 | 5.78 / 6.38 s | 6.09 s | **5.72 s** | 5.75 s | 6.64 s |

**24 线程反而最慢**（超线程争抢）→ `--cpu-threads` 默认 **`0`**（OpenVINO 自动），`--cpu-core-type` 默认 `any`，**一律不写死核心数**。

### 8.4 内存预算

| 模式 | 峰值 RSS | 预算 |
|---|---|---|
| 单设备（NPU / CPU） | 1.87 / 2.20 GB | ≤ 2.5 GB |
| hetero 双 pipeline | **3.93 GB** | ≤ 4.5 GB（单列） |

### 8.5 验收指标

| 指标 | 目标 | 实测 | 结论 |
|---|---|---|---|
| 首次加载（有 NPUW 缓存） | ≤ 10 s | **4.0 s** | ✅ |
| 单句翻译（≤50 token）P95 | ≤ 5 s | **0.8–1.4 s** | ✅ 大幅达成 |
| 解码吞吐 | ≥ 8 tok/s | **32.4**（NPU）/ 46.2（CPU） | ✅ |
| 常驻内存 | 见「内存预算」 | 1.87 / 2.20 / 3.93 GB | ⚠️ hetero 单列 |
| 翻译质量 | 回归集合格率 ≥ 90% | 冒烟 15/15 | 🟡 见「已知问题与活跃风险 · 寒暄句失真」 |
| 1000 字中文长文 | ≤ 60 s | **未测** | ⬜ 待补 |

### 8.6 `-b` 基准实测（2026-09-13，5 prompt × 3 次 × max_new_tokens=128）

连跑两轮（A 未存档 / B = `docs/bench_cli.json`，带环境指纹与逐次样本）：

| 设备 | 加载 | A 吞吐 | B 吞吐 | A TTFT | B TTFT | 峰值 RSS |
|---|---|---|---|---|---|---|
| **CPU** | 2.1 s | **54.14** 🥇 | **45.56** 🥇 | **0.126 s** | **0.152 s** | 3461 / 3472 MB ⚠️ |
| **NPU** | 3.7–4.2 s | 32.45 | 32.70 | 0.592 s | 0.590 s | 1870 / 1873 MB |
| GPU.0（iGPU） | 7.8–8.4 s | 37.60 | 25.39 | 0.136 s | 0.187 s | 1989 MB |

**读法（比数字本身更重要）**：

1. **排序稳定的是 CPU 第一、NPU 第二**（CPU 约为 NPU 的 1.4–1.7×）。与 M0 结论一致。
2. **🔴 单次基准不可信**：同一台机器、同一份配置，GPU 37.60 → 25.39（**±24%**）、
   CPU 54.14 → 45.56（±16%）。⚠️「噪声约 ±10%」**偏乐观**，真实是 ±20–30%。
   → `-b` 默认 `repeats=3`，报告带**区间列**，且波动 >20% 会直接打印警告。
   任何 1.2× 以内的"加速比"在单轮数据上都**不能**当结论。
3. **NPU 是唯一稳定的设备**：32.45 / 32.70 tok/s（±0.8%），TTFT 0.592 / 0.590 s（±0.3%）。
   原因是它独占算力、不与显示/系统负载抢资源 —— 这是 NPU 一个**真实但常被忽略**的优势
   （体验可预测），也让"CPU 更快"这件事在参考机上没那么值钱。
4. **旧基线偏低**：M0 的 CPU 46.2 / GPU 28.1 与这里的 B 轮（45.6 / 25.4）接近，
   与 A 轮（54.1 / 37.6）差得多 —— 差异主要来自 `scripts/bench.py` 测量期间开着
   `tracemalloc`（拖慢 10–30%）与系统负载。**跨版本比对数字前先核口径。**
5. **CPU 峰值 RSS 3.4 GB 是假的**：串行测三台设备时内存**累加**（前一台不立刻归还），
   单独 `-d cpu` 实测 2.2 GB。报告里已加脚注。

### 8.7 吞吐口径：字符/s，不是 token/s

`Outcome.chars_per_second` = **输出字符数 ÷ 池内推理秒数**。
GenAI 路径拿不到真 decode 计数（`TranslateEngine.tokens` 本身就是 `len(out)`）；
宁可用口径一致可比的真数，也不用看着像 token/s 的估数。「M2 异构」那张表用的就是字符/s。

---

## 9. WebUI（nputweb）

### 9.1 定位

- 独立命令 **`nputweb`**，与 `nputr` 并列；代码在同仓库子包 `src/npu_translator/web/`
- **不做独立分发包**：必须复用 `engine` / `segment` / `cache` / `languages`，且 OpenVINO 只能共享同一 venv
- 理念 **安全性 > 稳定性 > 效率**（涉及远程访问）
- 与 nputserve 的关系：nputweb 用**私有前缀 `/api/*`**；nputserve 的公开 `/v1/*` 另做，互不干扰

### 9.2 已拍板（勿擅自改）

| 项 | 决策 |
|---|---|
| 默认绑定 / 端口 | `127.0.0.1:8765`（`NPT_WEB_HOST` / `NPT_WEB_PORT`）；**非回环 → 强制 token** |
| TLS 三态 | `--tls auto`（默认，自签）/ `on`（`--cert` + `--key` 必填，缺一 → 码 2）/ `off`（明文） |
| 明文限制 | 非回环 + 明文 → **拒绝启动**（退出码 3），除非再加 `--allow-insecure` |
| 逃生舱 | `--allow-no-auth`：非回环下同时放行「无鉴权 / 明文 / 弱 token」，三条硬拦**降级为警告**（测试 / 可信局域网用）；它**不**自动关鉴权 —— `--no-auth` 仍是唯一意图表达 |
| 认证 | 非回环 **或** 启用 TLS → 强制 token；回环明文可免。校验用 `hmac.compare_digest`（常量时间） |
| 前端 | **纯静态 HTML + 原生 JS/CSS**，无框架 / 无构建 / 无 CDN（venv 无 jinja2，且离线优先） |
| 依赖 | 运行时**零新增**（fastapi / uvicorn / cryptography 已声明为可选依赖）；**dev 需补 httpx**（`TestClient` 依赖） |
| 输入 / 超时 / 队列 | 5000 字符（413）/ 120 s（504）/ 队列 8（503）/ 单 IP 30 次每分（429） |
| **health 豁免** | `/api/health` **豁免限流 + 队列准入**，但**鉴权与 Host 白名单不豁免**；前端心跳三档自适应：空闲 10 s / 忙碌 1.5 s / 加载中 3 s + 失败指数退避 |
| token 强度 | 对**最终生效的** token 无条件校验，与来源无关；弱 token + 非回环 → 拒绝启动（码 2），回环 → 警告放行 |
| 退出码 | 沿用「CLI 管道契约」语义：0 / 2（证书缺一半、非法 host）/ 3（端口占用、证书或引擎加载失败）/ 130 |

### 9.3 契约性约定（不看就会踩）

1. **`engine.translate()` 是同步阻塞的** → 必须 `run_in_executor`，且 **executor 只有 1 个 worker**。
   NPU 是单流设备，多 worker 只会抢全局锁 + OOM。**这是本模块最容易犯的错。**
2. **只绑 127.0.0.1 不等于安全**：恶意网页的 JS 能直接请求本机端口（DNS rebinding）。
   → ① token 走 `Authorization` header（不用 cookie → 天然免 CSRF，恶意页也读不到 `sessionStorage`）；
   ② **必须做 Host 头白名单**（只放行 `localhost` / `127.0.0.1` / `--allow-host` 显式声明的名字），
      否则 400。**本机主机名 / FQDN / `.local` 一个都不自动推导** —— 见 9.7。
3. **uvicorn access log 会记录完整 URL** → URL 里的 `?token=` 会写进日志。
   → 前端 `history.replaceState` 立即抹掉 + 服务端**脱敏 query**（或默认关 access log）。
4. **日志不记录原文**（翻译内容可能敏感，是「数据不出本机」的延伸），只记长度 / 语向 / 耗时。
5. **输入必须有上限**：KV cache 只有 768，不限长则一个 5 MB 请求能独占引擎几十分钟 = **一键 DoS**。
6. **退出路径与 CLI 相反**：常驻服务先优雅 shutdown，**超时 3 s 再 `os._exit` 兜底**。
7. **自签证书必须复用**（落 `~/.nputweb/`，**不入库**），否则浏览器每次重启都要重新信任。
   用 `cryptography` 生成，**不依赖外部 `openssl`**（Windows 没自带）。启动时打印 **SHA-256 指纹**供核对。
8. **静态目录最后挂载**，否则会吞掉 `/api/*`。
9. **错误体统一** `{"error": {...}}`，**不回传 traceback**（会泄漏绝对路径，违反「Git 约定：本机绝对路径禁止入库」）。
10. **`import npu_translator.web` 不得拉起 OpenVINO**（也不得拉起 fastapi / uvicorn / cryptography）；缺依赖给友好提示而非裸 `ImportError`。
11. `--host 0.0.0.0` 时启动横幅必须**解析并打印具体局域网 IP**，别把 `0.0.0.0` 直接丢给用户。
12. **`/api/health` 豁免限流与队列，但**不**豁免 Host 白名单与鉴权**。
    豁免只跳过中间件四步里的第 2 步（限流）与第 4 步（队列准入）。
    🔴 **绝不能**写成在 `path.startswith("/api/")` 处提前 `await self.app(...); return` ——
    那样 host 白名单 + 鉴权一起丢，health 就成了免费的未鉴权探测端点。
    （评审阶段有两位成员在这个点上判错过。「鉴权在限流之后」是**代码顺序、不是条件依赖**，
    跳过第 2 步完全不影响第 3 步照样执行。）
13. **前端心跳频率与限流配额是耦合的**：空闲 10 s = 6 次/分，占默认 30/min 的 1/5。
    改前端节奏**或**改 `DEFAULT_RATE_PER_MIN`，另一边都要重新算。
14. **改证书结构必须同时改 `tls._CERT_LAYOUT` 版本后缀**。证书文件名由 SAN 派生且存在即复用，
    不改文件名的话旧证书会被 `_loadable_pem_cert()` 直接命中 —— 代码改了、磁盘上还是旧的。

### 9.4 默认全开的安全项

CSP `default-src 'self'` · `X-Frame-Options: DENY` · `X-Content-Type-Options: nosniff` ·
`Referrer-Policy: no-referrer` · HTTPS 时 HSTS · `/docs` `/redoc` `/openapi.json` 默认 404（仅 `--debug` 开）·
**无文件上传 / 无路径参数 / 无命令执行 / 无模板渲染** · 页面零外链零 CDN。

> `/openapi.json` 默认关是第 6 条安全措施：**不给探测者地图**。参数化时若让它脱离 `--debug` 门控，
> 就等于把安全基线静默降级 —— 所以「文档与 openapi 是否受 debug 门控」是一个**显式开关**，
> 默认必须保持"受控"。

### 9.5 实现后的实测结论（2026-09-13）

| # | 结论 | 依据 |
|---|---|---|
| W1 | **真机端到端通过 15/15**：HTTPS 自签 + 真 NPU 翻译（"今天天气不错…" → 正确英文）、`zh-Hant` 正确输出繁体、413/400/401/429/503/504 各就各位、`/docs` 与 `/openapi.json` 404、静态页零外链 | 端到端脚本（临时，未入库） |
| W2 | **抽出共用编排层后 CLI 逐字节不变**：11/11 项 stdout 与退出码与重构前**完全一致**（含 hard/soft/auto/no-segment/打包 8 行/空输入/no-warmup/长文本） | 把 HEAD 源码导出到临时目录，`NPT_MODEL_DIR` 指向同一份权重，逐个用例 diff |
| W3 | **吞吐口径改成字符/s** | 见「实测基线 · 吞吐口径」 |
| W4 | **`os.chmod` 在 Windows 上无效**：私钥实际 mode 仍是 `0o666` | 实测 `~/.nputweb/` 下的私钥。POSIX 位不参与 NTFS 访问控制，实际保护来自用户配置目录的 ACL。**有意未改用 `icacls`**：写错反而可能把用户锁在门外，而原 ACL 已足够 —— 这是已知偏差，不是疏漏 |
| W5 | **`typer.Exit` 是 `RuntimeError` 的子类，不是 `SystemExit`** | 实测 `typer.Exit.__mro__ == (Exit, RuntimeError, Exception, BaseException, object)`。`main_entry` 只 catch `SystemExit` 的话会漏掉它；好在 click 的 standalone 路径会接住（真命令行实测退出码 3 正确） |
| W6 | **`JSONResponse` 的第一个位置参数是 content，不是 status_code** | 实测踩到：`JSONResponse(400, payload)` 一直炸到最里层的 `init_headers`（`status_code < 200` 的 TypeError），表现为 500 且响应体不完整。已全部改成关键字参数 |
| W7 | **`from __future__ import annotations` 会把注解变字符串** → 函数体内 import 的 `Request` 解析不到，FastAPI 会把 `request` 当**查询参数**，所有 POST 一律 422 | 实测。`Request`/`JSONResponse` 必须在**模块级** import |
| W8 | 单测全过，秒级，不加载模型 | `pytest -q` |
| W9 | 隐私扫描干净：新代码**无本机绝对路径 / 用户名 / 邮箱**；证书与私钥落 `~/.nputweb/`（仓库内无该目录、无证书或私钥文件） | 全量正则扫描 + `git status` |

### 9.6 可用性打磨轮（2026-09-14）

| # | 结论 | 依据 |
|---|---|---|
| W10 | **health 豁免后鉴权与 Host 白名单仍在**：伪造 Host → 400 `bad_host`；无 / 错 token → 401 `unauthorized`；队列满 → health 200 而对照组 `/api/languages` 503；`gate.depth` 不被 health 扰动 | TestClient 13/13 断言 |
| W11 | 前端空闲心跳 **6 次/分**（旧 40 次） | 三档自适应 + 退避实测 |
| W12 | **5 条心跳用例都做了红灯验证**：清空豁免集合后 5/5 全红 | 没有红灯验证的「绿灯」不能算数 |
| W13 | 写「health 打满后 translate 应成功」类用例，**采样点绝不能落在心跳间隔的整数倍** | limiter 的淘汰动作在 `hit()` **内部**先执行，撞上淘汰边界会先掉一格再放行 → **在有 bug 的代码上也是绿的**。主理人扫描 31 个 delta × 4 种点击间隔：整点偏移假绿率最高 57/57 全绿，off-grid 全部 0%。固定间隔还会撞共振 —— 首轮报的「66.7% 成功率」就是这么来的。**评审阶段 5 人次栽在这上面** |
| W14 | 自签证书改非 CA 后仍能被 `ssl.SSLContext.load_cert_chain` 加载 | 实测 `ca=False`、`key_cert_sign=False`、EKU=`serverAuth`、SKI 存在 |
| W15 | 单测数量随每轮交付递增 | `pytest -q` |
| W16 | **一条「纯逻辑模拟」用例被判无效并删除** | 它在测试里自己手写「health 不调 `hit()`」的语义，压根没经过中间件 → 去掉豁免照样绿，红灯验证时暴露。**建模出来的绿灯比没有绿灯更危险**：配额类断言必须走 TestClient 打真实栈 |
| W17 | **组合坏、单件对**：原套件只测了 `RateLimiter` 的纯逻辑（上限 / 滑动 / 按 key 隔离 / 0=不限），单元全对，但没有任何用例验证「health 会吃掉 translate 的配额」 | 这是那个 P0 能溜到用户手里的直接原因 |
| W18 | 前端**零 node 依赖也能测**：只用只读文本断言（心跳常量、aria 属性必须静态存在、无内联 `style`/`on*`） | 引入 playwright 会让「秒级套件」这个前提破产 |

### 9.7 `--allow-host`：按域名访问的唯一入口（2026-09-17）

**用法**：`--allow-host <名字>`（可重复给），或环境变量 `NPT_WEB_ALLOWED_HOSTS`（逗号分隔）。
一个开关同时喂**两处** —— Host 白名单与自签证书的 SAN：

| 生效点 | 代码 | 不补会怎样 |
|---|---|---|
| Host 白名单 | `HostPolicy.build(bind_host, extra=...)` | 400 `bad_host` |
| 自签证书 SAN | `resolve_tls(..., extra_hosts=...)` | 浏览器 **证书名字不匹配**（`ERR_CERT_COMMON_NAME_INVALID`） |

只补第一处会把一道看不懂的错换成另一道看不懂的错。**两处必须同步。**

**不给就什么都不放行**（默认是空元组），行为与「输错 IP」完全一致。
刻意**不**自动推导本机名字，三条理由：

1. **推导不动，也不该推导。** 同一台机器可以被叫短主机名、FQDN、`xxx.local`、`hosts` 里的别名、
   DNS 里的 CNAME —— 自动只加 `socket.gethostname()` 那一个，用户从别的名字访问照样 400，
   于是这个 bug 会反复「复现」。
2. **把控制权交出去。** 自动推导等于把「谁可以访问」交给当时的 DNS 配置，
   包括 **DHCP 下发的搜索后缀**。在不可信的局域网里，那个后缀是谁给的，就等于信任谁。
3. **会自动漂移。** 换网络 / 改计算机名 / DNS 后缀变了就失效，而且失效得毫无征兆。

`nputserve` 同理（`NPT_SERVE_ALLOWED_HOSTS`）。它的 `api_prefix="/"` 是全站受检，
**连 `/v1/health` 与 `/v1/openapi.json` 都要过这一关** —— 这里配错的影响面比 nputweb 更大。

**400 的措辞**（`web.app._bad_host_message`）：复述被拒的那个 Host + 提示 `--allow-host`。
它是请求方自己刚发过来的字符串，复述不构成泄漏；但**完整白名单只在 `--debug` 下回显** ——
不把本机全部 IP 与主机名交给一个连错的陌生人。

> **想用正规域名访问的正确做法**：给这台机器一个**你独占**的名字
> （带 MagicDNS 的私有 overlay 网络、内网 DNS 的 A 记录、或你自己注册的域名），
> 然后 `--allow-host <那个名字>`。
> 公网域名 + Let's Encrypt 这条路也跑得通（`--tls on --cert --key` 早已支持），
> 但那要引入 DNS API token 与 90 天续期，与「完全离线、完全本机」的定位置换不来。

---

## 10. 服务（nputserve）/v1 API 契约

### 10.1 定位与为什么是独立进程

`nputserve` 是**独立命令**（独立进程 / 独立端口 / 独立 token / 独立限流桶），只挂 `/v1/*`。
五条互相独立的理由，任何一条都足以否掉"挂进 nputweb"：

1. **限流语义互斥**：`/api/health` 豁免限流的**唯一理由**是前端心跳节奏；
   程序化调用没有这个节奏，共享一个限流桶必然互相抢配额。
2. **静态挂载互斥**：nputweb 无条件 mount `web/static`；服务不需要也不该有静态面（多一个面就多一个探测点）。
3. **OpenAPI 策略互斥**：`/v1/openapi.json` 默认开（给第三方程序读），`/docs` / `/redoc` 默认关。
   同一进程做不到"一个开一个关"。
4. **生命周期与崩溃隔离**：WebUI 挂着浏览器与常驻心跳；服务被脚本调。一个崩了不该带走另一个。
5. **鉴权面分离**：可以给一枚独立 token，撤销时不影响 WebUI 会话。

**复用了什么（不重写）**：安全中间件栈与 app 工厂（`web.app.create_app`）、鉴权与强度判定（`web.auth`）、
限流 / 队列（`web.limits`）、证书三态（`web.tls`）、路径脱敏与断开判定（`web.routes`）、
绑定判定与退出码（`web.cli.resolve_binding` / `check_port_free`）、**编排（`orchestrate.Translator`）**。

### 10.2 端点总表

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/translate` | 翻译（分段、可缓存） |
| `POST` | `/v1/translate/stream` | 流式翻译（SSE，**不分段**） |
| `GET` | `/v1/languages` | 38 语种清单 |
| `GET` | `/v1/health` | 健康 / 排队 / 通道占用 / 孤儿计数 |

默认端口 **8766**（nputweb 是 8765，两个命令**会**同时起，默认撞车会让第二个启动失败）。

### 10.3 `POST /v1/translate`

**请求体**

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `text` | string | — | 必填，非空；超过 `max_input_chars` → 413 |
| `target` | string | 服务默认值 | 语言代码或名称；不支持 → 400 `bad_target` |
| `source` | string | `"auto"` | 源语言 |
| `newline` | string | 服务默认值 | `soft` / `hard` / `auto`；非法 → 400 `bad_newline` |
| `strict` | bool | `false` | `true` 时有段未译出 → 500 `partial_failure` |
| `max_new_tokens` | int | `null` | ≥ 1；非法 → 400 |

**响应**：编排层 `Outcome.to_dict()` 再加四个键。

| 键 | 含义 |
|---|---|
| `newline` | 本次生效的换行档 |
| `queue_position` | 中间件记录的排队位次 |
| `request_id` | 本次请求的序号（日志 / SSE 定位用） |
| `wall_s` | 服务端墙钟耗时 |

`Outcome.to_dict()` 的键：`text` / `device` / `segments` / `lines` / `chars` / `elapsed_s` /
`infer_s` / `chars_per_second` / `failed` / `model_calls` / `reused` / `cached`。

**504 的语义（必读）**：超时后**底层推理不可取消**，
结果不返回给调用方（**可能仍写入缓存**）；响应头带 `X-NPUT-Orphan: 1` 标记产生了孤儿。
措辞必须说"可能仍写入缓存" —— 说成"丢弃结果"会让人以为缓存里也没有。

### 10.4 `GET /v1/languages`

与 `/api/languages` 同形：`{"total": 38, "common": [...], "others": [...]}`，
每项含 `code` / `zh_name` / `en_name` / `native` / `prompt_name`。
`prompt_name` 是**给模型的名字**（`zh-Hant` 为「繁体中文」），见「Prompt 与语言」。

### 10.5 `GET /v1/health`

比 `/api/health` 多四个服务层字段：

| 键 | 含义 |
|---|---|
| `active_device` / `device` | 当前生效的推理设备。两个键同值：前者是外部要求的字段名，后者兼容 nputweb 口径 |
| `streaming` | 是否有流式请求占着通道 |
| `lane` | 谁占着推理通道：`""` / `"translate"` / `"stream"` |
| `orphans` | 被放弃但仍在跑的推理数。**非零是预期行为，不是 bug**（物理锁在孤儿手里） |

另有 `status` / `devices` / `degraded` / `queue` / `waiting` / `max_pending` / `progress` /
`tls` / `uptime_s` / `limits`。`limits` 里回显 `max_input_chars` / `max_stream_chars` /
`timeout_s` / `queue_size` / `rate_per_min` / `max_streams` / `lane_wait_timeout_s`。

### 10.6 `POST /v1/translate/stream`（SSE）

`Content-Type: text/event-stream; charset=utf-8`，响应头带
`Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`
（关掉反向代理的响应缓冲，否则 SSE 会被攒着一次性发）。

**事件序列**（失败时产出 `error` 后结束）：

```
: nputserve SSE                     ← 注释行，冲掉中间缓冲层
event: ready   data: {"request_id","device","lane","max_stream_chars"}
event: token   data: {"delta": "..."}        ← 0..N 个
: keepalive                         ← 心跳（stream_keepalive_s 默认 15 s）
event: done    data: {"request_id","text","device","chars","elapsed_s","infer_s","chars_per_second"}
event: error   data: {"code","message","abandoned"}
```

- `done` 里的 `infer_s` 与 `elapsed_s` **同值**：流式不走池，没有"池内计时"这一层。
  保留这个字段是为了让两个端点的响应形状一致，别让调用方写两套解析代码。
- `error` 的 `abandoned: true` 表示「调用方已放弃，但底层推理不可取消」。
  这条语义必须每次都说，否则调用方会误以为服务端已经停了。
- **流式不分段**：输入受 `max_stream_chars` 限制（默认 160，实测依据见「服务：并发与超时模型」）。
- **并发上限 `max_streams`（默认 1）**：超了**不排队**，直接 503 `stream_busy`。

### 10.7 统一错误体与错误码

一律 `{"error": {"code": ..., "message": ...}}`，**不回传 traceback**。

| 状态码 | code | 触发 |
|---|---|---|
| 400 | `bad_host` | Host 头不在白名单（DNS rebinding 防护） |
| 400 | `bad_json` / `bad_request` / `empty_input` / `bad_target` / `bad_newline` | 请求体与参数校验 |
| 401 | `unauthorized` | 缺少或错误的 token（请带 `Authorization: Bearer <token>`） |
| 413 | `payload_too_large` | 声明长度或实际长度超上限（`Content-Length` 阶段就拒，不读 body） |
| 429 | `rate_limited` | 单 IP 每分钟超过 `rate_per_min`，带 `Retry-After` |
| 499 | `client_closed` | 客户端在 body 传完前断开（499 非 RFC 标准码，nginx 沿用多年，含义通用） |
| 500 | `internal_error` / `partial_failure` | 兜底 / `strict=true` 且有段未译出 |
| 503 | `queue_full` | 队列已满 |
| 503 | `lane_busy` | 等推理通道超过 `lane_wait_timeout_s`，带 `Retry-After` |
| 503 | `stream_busy` | 并发流已满（**不排队**） |
| 504 | `timeout` | 单请求超过 `timeout_s`；带 `X-NPUT-Orphan: 1` |

### 10.8 继承的安全基线（与 nputweb 同一份实现）

- **D6**：非回环绑定 **或** 启用 TLS → **强制 token**；纯回环明文可免
- **D7**：非回环 + 明文 → **拒绝启动**（退出码 3），除非再加 `--allow-insecure`
- **D11**：对**最终生效的** token 无条件校验，与来源无关；弱 token + 非回环 → 拒绝启动（码 2），回环 → 警告放行
- **逃生舱**：`--allow-no-auth` 把上面三条在**非回环**下的「拒绝启动」一律降级为警告；它**不**等于 `--no-auth` —— 只给逃生舱时 token 照旧强制
- **Host 白名单**：`localhost` / `127.0.0.1` / `--allow-host` 显式声明的名字，否则 400 `bad_host`。
  serve 是 `api_prefix="/"`，**全站受检** —— `/v1/health` 与 `/v1/openapi.json` 也过这一关。
  本机主机名 / FQDN / `.local` **不**自动推导（见 9.7）
- **token 走 `Authorization: Bearer`**（不用 cookie → 天然免 CSRF）
- **限流不认 `X-Forwarded-For`**（按真实对端 IP 计，避免伪造头绕过）
- **access log 默认关**（它会记完整 URL，可能带 token）
- **错误体与响应一律过 `scrub_paths()`**（异常消息里常带本机绝对路径，含用户名）

### 10.9 与 nputweb 的三处差异（且只有这三处）

| 项 | nputweb | nputserve | 理由 |
|---|---|---|---|
| `api_prefix` | `/api/` | **`/`** | 全站受检：未知路径也要先过 Host 白名单与鉴权才拿 404，否则 `/随便什么` 就是一条不需要凭据的探测面 |
| 豁免集合 | `exempt_from_rate` = `exempt_from_queue` = `{/api/health}` | `exempt_from_rate` = **空集**；`exempt_from_queue` = `{/v1/health, /v1/languages}` | 限流豁免的理由是"前端心跳节奏不受服务端控制"（程序化调用没有心跳）；队列豁免的理由是"纯读不该占队列位置，会把排队的翻译挤成 503"（两边都成立）。**两个集合必须分开**，合成一个必然被迫在"两个都免"和"两个都不免"之间选一个错的 |
| 静态面与文档页 | mount `web/static`；`/docs` `/redoc` `/openapi.json` 仅 `--debug` | 不挂载；`/docs` `/redoc` 仍受 debug 门控，但 **`/v1/openapi.json` 默认开** | openapi 是给第三方程序读的契约文件；`/docs` 交互式页面才需要 debug 门控 |

> `/v1/languages` 只拿 `exempt_from_queue`，**不**拿 `exempt_from_rate` —— 它是只读的，
> 但不该成为一条无限速通道。

### 10.10 CLI 与环境变量

命令行：`nputserve [--host] [--port] [--tls auto|on|off] [--cert] [--key] [--token] [--no-auth]
[--allow-insecure] [--allow-no-auth] [--device] [--newline] [--max-input-chars] [--timeout] [--queue-size] [--rate]
[--max-streams] [--lane-wait] [--max-stream-chars] [--debug] [--no-warmup] [--version]`

环境变量（作为**默认值**，命令行显式给值则覆盖）：`NPT_SERVE_HOST` / `NPT_SERVE_PORT` /
`NPT_SERVE_TLS` / `NPT_SERVE_CERT` / `NPT_SERVE_KEY` / `NPT_SERVE_TOKEN` / `NPT_SERVE_NO_AUTH` /
`NPT_SERVE_ALLOW_INSECURE` / `NPT_SERVE_ALLOW_NO_AUTH` / `NPT_SERVE_MAX_INPUT_CHARS` / `NPT_SERVE_TIMEOUT` / `NPT_SERVE_QUEUE` /
`NPT_SERVE_RATE` / `NPT_SERVE_DEBUG` / `NPT_SERVE_MAX_STREAMS` / `NPT_SERVE_LANE_WAIT` /
`NPT_SERVE_MAX_STREAM_CHARS`；另沿用全局的 `NPT_DEVICE` / `NPT_MODEL`。

退出码沿用「CLI 管道契约」语义：0 / 2（参数错误）/ 3（端口占用、证书或引擎加载失败）/ 130。
启动横幅必须打印：端口 / token（只打一次）/ 设备链路 / 四个端点 / openapi 地址 / 各项限制。

### 10.11 与 nputweb 的导入边界

| 模块 | 约束 |
|---|---|
| `import npu_translator` | 不得拉起 OpenVINO |
| `import npu_translator.web` | 不得拉起 fastapi / uvicorn / cryptography |
| `service.py` | **不得 import fastapi**（纯逻辑，脱离网络栈可单测）。需要复用 `web.routes` 的脱敏函数时**延迟 import** |
| `server.py` | **唯一**在模块级 import fastapi 的新文件；且不被 `npu_translator/__init__.py` 也不被 `web/` 引用 |

---

## 11. 服务：并发与超时模型

### 11.1 前提：底层推理不可取消

`engine.generate()` 同步阻塞在原生调用里，`asyncio` 取消不了。
所以本章所有的"超时"都只表示**放弃等待**，不表示**取消推理**。
这条语义要写在四处：504 的 message、响应头 `X-NPUT-Orphan`、SSE 的 `error` 事件、本文档。
漏一处调用方就会误以为"超时 = 服务端已经停了"。

### 11.2 推理通道 `InferenceLane` 是准入凭证，不是物理锁

物理串行由 engine 内部的 `self._lock` 保证（那层不能动，也不够）。
lane 的作用是把"NPU 单流"这件事从 engine 内部的**隐式状态**提升为服务层的**显式对象**，
于是"等"变得：

- **可超时** —— 超时给 503 `lane_busy` + `Retry-After`，而不是让调用方挂到 504
- **可观测** —— `/v1/health` 的 `lane` / `streaming` 字段
- **可释放** —— 超时立刻放

🔴 **释放 lane 之后孤儿线程仍持有 engine 的物理锁**，下一个请求会自然地在**那把真锁**上排队，
不会互相破坏。反过来，如果 lane 要等孤儿跑完才释放，那就是把「504 孤儿」换个地方再犯一次。

> `InferenceLane` 内部的 `asyncio.Lock` **按事件循环懒建**：3.11 的 `asyncio.Lock` 会在首次争用时
> 把自己绑定到当时的事件循环，之后再换 loop 用就抛 "bound to a different event loop"。
> 生产环境一个进程只有一个 loop 无所谓，但**单测里每个 `asyncio.run()` 都是新 loop**。

### 11.3 两个超时，别混

| 开关 | 默认 | 管的是 | 超时结果 |
|---|---|---|---|
| `lane_wait_timeout_s`（`--lane-wait`） | `10.0` | **等通道** | 503 `lane_busy` + `Retry-After`；`0` = 无限等（逃生舱） |
| `timeout_s`（`--timeout`） | `120` | **跑推理** | 504 `timeout` + `X-NPUT-Orphan: 1` |

### 11.4 孤儿（Q2）：记账必须由孤儿自己记平

```
超时 → ticket.abandon()（孤儿 +1）→ 立刻释放 lane → 中间件释放队列位 → 504
                                                              ↓
孤儿线程跑完 → _guarded() 的 finally 里 ticket.settle()（孤儿 −1）
```

- `abandon()` 与 `settle()` 都**幂等**；计数不平衡的后果是 `/v1/health` 的 `orphans` 永久非零，
  运维看到会以为设备一直被占着。
- `settle()` 必须由**孤儿自己**在 finally 里调，不依赖任何 future（那个 future 可能已被取消、已没人引用）。
- `_guarded()` 刻意**吞掉异常**：`concurrent.futures` 里没人取回的异常会在 GC 时打一条
  "exception never retrieved" 噪音，而这条请求的调用方早就不存在了。异常本体留在 `ticket.error` 上。

### 11.5 流式为什么不走 executor

`Translator.stream()` 是**同步**生成器，内部还起了一个线程阻塞在队列读取上。
executor 恒为 `max_workers=1`（NPU 单流，多开只会抢锁 + OOM），
一个流式请求进去就占死整个池，`/v1/translate` 全部饿死。

→ 每个流起一个**专用 daemon 线程**，用 `loop.call_soon_threadsafe` 把 token 推进 `asyncio.Queue`，
由 `stream_events()` 的 pump 协程消费。

> ⚠️ 推送必须容忍「循环已经关了」：孤儿线程可能比事件循环活得久（关停、单测里 `asyncio.run()`
> 结束时都常见），此时 `call_soon_threadsafe` 会抛 `RuntimeError('Event loop is closed')`。
> 往一个没人读的队列推东西本来就没意义，但让它变成线程里的**未捕获异常**会污染日志与测试输出。

### 11.6 流槽 `StreamSlots`：快失败，不排队

- 用 `threading.Lock` + 计数器，**刻意不用 `asyncio.Semaphore`**：我们只需要"非阻塞地抢一个位子"，
  而 `Semaphore` 会把状态绑到某个事件循环上（跨 `asyncio.run()` 单测会炸），
  还引入了一个我们根本不需要的等待队列 —— 流式的语义恰恰是"抢不到就走"。
- 无空位 → 503 `stream_busy` + `Retry-After: 2`。**第二个流不排队**：
  排在另一个流后面等于把整段输出缓冲下来，语义已经没了，还白占一条连接。
- 抢到槽但没抢到通道时，**槽必须还回去**，否则流永久堵死。

### 11.7 SSE 的两个时序陷阱

1. **槽位必须在建 `StreamingResponse` 之前抢**。async generator 的第一行代码要等到
   body 开始迭代才执行，那时响应头已经发出去了，503 就没法给了（只能 200 + 半截流）。
2. **心跳间隔到了先问客户端还在不在**（`request.is_disconnected`）。不问的话，
   一条已经断开的连接会把 lane 一直占到总超时。

### 11.8 `max_stream_chars = 160` 的实测依据

硬约束（见「NPU 实现要点 · 静态形状约束」）：KV cache 总容量 = `MAX_PROMPT_LEN`(512) +
`MIN_RESPONSE_LEN`(256) = **768**；且**生成 token 数超过 256 会静默截断**。
批量翻译靠分段规避（单段 ≤ 512 字符），而**流式与分段互斥**，整段原文直接进 prompt，
所以输入必须比批量短得多。

用模型目录里真实的 `openvino_tokenizer.xml` 实测：

| 语向 | 字符 | prompt token | 生成上限 | 合计 | 判定 |
|---|---|---|---|---|---|
| 中→英 | 150 | 81 | 236 | 317 | OK |
| 中→英 | 200 | 106 | 304 | 410 | **截断**（生成 > 256） |
| 日→英 | 150 | 135 | 236 | 371 | OK |
| 日→英 | 200 | 177 | 304 | 481 | **截断**（生成 > 256） |
| 英→中 | 468 | 87 | 256 | 343 | 临界 |

二分得到三条约束全部满足的最大字符数：**中/日源 165、英源 468**。
CJK 源最吃紧（1 字 ≈ 0.85 token，译文通常还比原文长），**165 是全局最坏值**。
取 **160** = 165 再留 3% 余量（160 字上：生成上限 249 ≤ 256，prompt ≈ 86 ≤ 512，合计 335 ≤ 768）。
模板固定开销实测 11–15 token，已含在内。

想放大就用 `--max-stream-chars`，但**超过上面的值就会静默截断** ——
那是 NPU 静态形状的硬约束，服务层救不了。宁可让超限请求拿到明确的 413，
也不要用户拿到一段被静默截断的译文。

### 11.9 关停路径

uvicorn 跑在**后台线程**，主线程专职等 Ctrl+C。顺序：

1. 置 `server.should_exit = True`
2. `thread.join(3.0)` —— 优雅阶段
3. 仍活着 → 打印"强制退出"，`ctx.shutdown(wait=False)`
4. 返回 130

**关停路径一律 `wait=False`**：NPU 那段 generate 可能卡在原生调用里，等它等于永远关不掉。
走到 `main_entry` 再卡就是白白浪费用户时间 → flush 之后 `os._exit`（与 CLI 同理）。

---

## 12. Git 约定：本机绝对路径禁止入库

**规则**：仓库内不得出现本机绝对路径（盘符开头的 Windows 路径、POSIX 家目录下的路径）、
真实邮箱、PEM 私钥块。写路径一律用**相对路径**或 `~` 开头的写法。

**为什么**：本仓库是公开的。绝对路径会把盘符、目录结构、**用户名**一起带出去；
而且这类泄漏不会报错，只会安静地躺在 JSON 报告与错误信息里。

**三个已知泄漏点与收敛手段**：

| 泄漏点 | 收敛手段 |
|---|---|
| 基准报告 JSON（`docs/*.json`） | `benchmark.display_path()` 把模型路径与 `NPUW_CACHE_DIR` 收敛成相对项目根的路径 |
| HTTP 错误响应体 / 日志 | `scrub_paths()` + `_safe_error_message()`：**只回 type + message 的摘要，绝不回 traceback** |
| uvicorn 未捕获异常 | 客户端断开（`ClientDisconnect` / `OSError`）必须接住，否则 traceback 冒到 uvicorn，带绝对路径 |

**也要一并排除的**：`models/`（本地权重）· `.npucache/`（NPUW 编译缓存）· `~/.nputweb/`（证书与私钥）·
`bin/`（`scripts/build.py` 的生成产物）· 任何 PEM 证书与私钥文件。

**自检方式**：提交前跑一遍全量正则扫描（本机用户名 / 邮箱本地部分 / 盘符绝对路径 / PEM 块），
再加一次 `git status` 人工过目。扫描脚本只报**维度名与命中数**，不复述被扫到的值。

---

## 13. 执行约定

1. **先读本文件再动手**，冲突处先改本文件再改代码
2. 新增的重要结论（实测数据、踩坑、版本组合）**追加到对应章节**
3. 每条写死的常量（模型路径、`MAX_PROMPT_LEN`、量化参数）必须能从 `config.py` 覆盖，不散落在业务代码里
4. 涉及设备/模型/量化的改动，**跑 `scripts/bench.py` 与 `scripts/smoke_test.py` 并把结果贴回来**
5. 不确定时优先做**可回退**的设计，而不是赌某个设备一定行
6. 代码注释引用规格书时用**章节名**（如 `SPEC.md · CLI 管道契约`），
   **不要用章节号** —— 编号会随结构调整漂移；**也不要在章名后面追加说明文字**（见本文件开头）

---

## 14. 踩坑记录

### 14.1 环境

1. **某些环境下的 bash（PortableGit）coreutils 残缺**：`ls` / `head` / `tail` / `dirname` / `grep` / `wc` / `sort` / `cut` / `cp` 可能全没有。
   → 凡要过滤/统计/复制的活直接写 Python；调 git 用 `git -C <路径>`。
2. **`pip install` 被 SIGTERM 打断会清空包内容**：实测一次中断把六个包的 `.py`/`.pyc` 全清了（只剩目录骨架），pytest 起不来。
   → 别中途打断；离线恢复去 pip 的 HTTP 缓存目录遍历 `.body`，用 `zipfile` 按 `dist-info/METADATA` 对齐版本后解压回 site-packages。
3. **OpenVINO 不暴露 CPU 拓扑**：`INFERENCE_NUM_THREADS` 恒为 0（表示推理时自动决定），`CPU_THREADS_NUM` 不存在，只有 `FULL_DEVICE_NAME`。
   → **任何硬编码核心数都是错的**，默认值必须是 `0`。
4. **OpenVINO 会枚举 NVIDIA dGPU 为 `GPU.1`**，但不走 CUDA 后端 → 设备选择必须按 vendor 过滤，否则会选到不能用的设备。

### 14.2 编码与管道

5. **UTF-16BE 被解成乱码**：剥掉 BOM 后再用裸 `"utf-16"` codec，它会**自己再吃一次 BOM**，没 BOM 就退回本机字节序（x86 = LE）。
   → BOM 分支必须用带字节序的 `utf-16-le` / `utf-16-be`。
6. **Windows 的管道关闭不是 `BrokenPipeError`**，是 `OSError: [Errno 22] Invalid argument`。只 catch `BrokenPipeError` 的话 `| more` 照样喷 traceback。
   → `encoding.is_broken_pipe()` 统一判定（EPIPE / EINVAL / EBADF）并归一成 `BrokenPipeError`。
7. **PS 5.1 `>` / `>>` 产出 UTF-16LE**，追加时 BOM 只写一次但文件整体是 UTF-16 → 主推 `-o/--append`。
8. **cp936 下 stdin 的静默 mojibake 比崩溃更危险** → 解码失败必须报错，绝不 `errors="replace"`。

### 14.3 模型与 prompt

9. **`Traditional Chinese` 模型不认**，必须用中文名「繁体中文」，否则回吐英文原文（详见「Prompt 与语言」）。
10. **`zh-Hant` 含大写** → `str.islower()` 判断「是否已是英文名」会误判，把语言代码塞进 prompt → 一律查语种表。
11. **旧 `segment._hard_split` 只按逗号/空格切** → 中文无标点长文本切不开，超 KV cache 后被**静默截断**。已补按固定长度硬切的兜底。

### 14.4 CLI 框架（typer / click）

12. **click 的 `MultiCommand.allow_interspersed_args = False`**：遇到第一个位置参数就停止解析选项 → `nputr "文本" --to en` 报 `No such command '--to'`；放开之后 `nputr languages` 的 `languages` 又被位置参数 `text` 吃掉。
    → **语种/设备列表从子命令改成 `--languages` / `--devices` 选项**。
13. **typer 自带一份 click**（`typer._click`）：`ctx.get_parameter_source()` 返回的是 `typer._click.core.ParameterSource`，
    而 `from click.core import ParameterSource` 拿到的是真 click 的那份，**两者 `==` 恒为 False** ——
    用它判断"用户是否真的写了这个选项"会静默判成"写了"。
    → 一律按 `getattr(src, "name", ...)` 比名字，或者干脆用 `None` 当"我没给"的信号。
14. **不该关掉 rich 就忘掉 `--help`**（见第 12 条）—— 同理，`--help` 里写中文没问题，
    但**别用 `§` 编号**，章节号会漂。

### 14.5 并发与调度

15. **pool 的 worker 提前退出**：三层竞态，逐个修 —— ① 见队列空就退，别的 worker 正在重试导致任务放回后无人接手 → 加 `inflight` 计数；② 只有「在飞」不够，任务还在队列里没被领走时不算在飞 → 改用 `remaining`（未落定任务数）作唯一权威；③ 换人重试时用 `q.qsize() > 0` 判据错误，队列只剩这一个任务时会自己领回来，BUSY LOOP 到重试耗尽 → 改用存活线程数 + 让出后 `sleep(2 ms)`。
16. **hard 打包会吃掉 LRU 复用**（见「CLI 管道契约 · 稳定性要求」第 6 条）。

### 14.6 基准

17. **`tracemalloc` 会把基准测慢 10–30%**：旧 `scripts/bench.py` 在测量循环里开着它，于是 M0 基线偏低。
    → 基准里**只用 psutil 读 RSS**，不要用 tracemalloc。跨版本对比数字时先核口径。
18. **串行测多设备时 RSS 是累加的**：同一进程里 NPU → GPU → CPU 依次建管道，前一台的内存不会立刻归还。
    → 报告里加了脚注；要单设备常驻就单独 `-d` 跑一次。
19. **报告默认会带绝对路径**：模型路径与 `NPUW_CACHE_DIR` 都是绝对路径，直接 `json.dump` 就把机器目录写进 `docs/`。
    → `benchmark.display_path()` 统一收敛（见「Git 约定：本机绝对路径禁止入库」）。

### 14.7 退出与线程

20. **「译文打完了但进程不退出」**：关停阶段要 join OpenVINO 留下的 daemon 线程，
    它停在 `concurrent/futures/thread.py` 的 `work_queue.get(block=True)`。
    正常时能被关停钩子唤醒并 join 掉（多种调用方式实测均正常退出，未能复现）；
    但一旦该线程被原生调用卡住（NPU 被别的进程占用 / 驱动态异常），
    `threading._shutdown()` 就永远返回不了。
    → 修法：**显式 flush stdout/stderr 之后 `os._exit(code)`**，把这条路径物理切断。
    代价核对：写文件在 `with` 内已关闭；项目没有任何必须执行的 `atexit`；
    **NPUW 缓存在编译期同步落盘，不受 `os._exit` 影响**（实测冷 32.4 s → 热 7.4 s）。
21. **`--no-warmup` 必崩**：该选项下预热线程从未 `start()`，后面的 `join()` 抛
    `RuntimeError: cannot join thread before it is started`。
    → 用 `thread.ident is not None` 判断是否启动过，再决定 join。

### 14.8 服务层（nputserve）

22. **槽位必须在建 `StreamingResponse` 之前抢** —— 响应头一旦发出，503 就给不出去了（见「服务：并发与超时模型」）。
23. **`asyncio.Lock` 会把自己绑到首次争用的事件循环** —— 单测里每个 `asyncio.run()` 都是新 loop，
    第二个用例直接红。必须按 loop 懒建。
24. **孤儿线程可能比事件循环活得久** —— `loop.call_soon_threadsafe` 会抛
    `RuntimeError('Event loop is closed')`，必须在推送侧容忍。
25. **`openapi_url` 不能脱离 debug 门控**（默认必须受控）—— 让它默认可达等于把
    "不给探测者地图"这条安全措施静默降级。要改就用一个**显式的、默认保守**的开关。

---

## 15. 已知问题与活跃风险

> 本节用**描述性短句**做标题，不用编号 —— 编号会漂，名字不会。

### NPU 并发未实测

🟡 架构约定不变（全局锁 + 队列 + 服务层的推理通道）。需要实测并发排队行为。
⚠️ 若目标平台是服务器级多核（64 核以上），现有**单并发设计会浪费算力** → 此项会升级为阻塞项。

### NPUW 缓存失效未验证

🟡 尚未经历驱动/版本升级。缓存 key 需含版本指纹。

### NPU 被其他进程占用时生成永久挂起

🟡 **已加两道兜底**：预热限时（`NPT_WARMUP_TIMEOUT`）避免「零输出 + 永久挂起」；
关停诊断开关可现场取证。根因与「关停阶段卡死导致进程不退出」同构：**底层推理不可取消**。

### CPU 更快导致默认设备缺性能理由

🔴 **待决策**。参考机两轮 `-b`：CPU 45.6–54.1 > NPU 32.5–32.7 tok/s，NPU 的 TTFT 仍是 CPU 的 4 倍。
**纯性能 NPU 不如 CPU**；但 NPU 是唯一稳定的设备（±0.8% vs CPU ±16%），体验可预测。
→ 缺**功耗实测**（watts / 1k token）作为依据。当前默认 NPU（项目初心 + 省电）。

### 异构并行收益在噪声边缘

🟢 达标但不进默认。动态派活 1.193×（越过 1.15× 门槛），但内存 +78% → 手动 opt-in。该数字在噪声边缘。

### 跨机器默认值不可外推

🟡 **已规避**。参考机「CPU 比 NPU 快 1.9×」不可外推：别的机器可能是 NPU5 48 TOPS + 8 线程，
强弱关系反转 → `--cpu-threads` 默认 `0`，**不写死核心数**。

### 关停阶段卡死导致进程不退出

🟢 **已缓解**。flush 后 `os._exit` 绕开关停阶段；关停诊断开关可现场取证。
**根因未最终确认**（多种调用方式均无法复现），故保留诊断手段而非只改一处。

### 基准噪声 ±20–30%

🟡 **已缓解**。两轮 `-b`：GPU 37.6/25.4、CPU 54.1/45.6，而 NPU 稳定在 32.5/32.7。
→ `repeats` 默认 3 + 报告带区间列 + 波动 >20% 报警，**1.2× 以内的加速比在单轮数据上不成立**。

### health 豁免限流后强度全押在 token 上

`nputweb` 的 `/api/health` 豁免限流后，请求序列变成「Host → 鉴权 → 放行」，
**拿错 token 打 health 不消耗任何配额**，理论上可无限速重试。

- **触发条件很窄**：需用户**自己显式传弱 token 且绑回环**。默认自动生成的是 43 字符 / 256 bit 强 token；
  非回环的弱 token 已被"弱 token + 非回环 → 拒绝启动"拦掉。
- **严重度 Low**：本项目没开 CORS，浏览器里的恶意页能发请求但**读不到** 401/200 的区别，
  构不成可行爆破通道；能读响应的攻击者基本已经能在机器上跑代码了。
- **缓解方案（未实施，待拍板）**：只对豁免路径的 **401 计数**，且**必须用独立桶** ——
  共用那一枚限流器的话，攻击者刷错 token 就能烧光用户自己的翻译配额。
  落点：在第 2 步对豁免路径先做一次廉价的 `checker.accepts()` 预检，不匹配才计数 ——
  不用颠倒「限流早于鉴权」的顺序。

### 已解除的风险

NPU 解码慢（实测 32.4 tok/s，远超 8 目标）· 编译时间长（缓存后 4.0 s）· 版本耦合（组合已锁定 +
现成权重跨版本可用）· NF4（不采用）· 模型下载（ModelScope 稳定，约 1 分 40 秒）·
输出夹带解释（冒烟 15/15 无夹带）· 换行丢失（`segment.py` 重写，真机 6→6 行确认修复）·
关停卡死（flush + `os._exit` 绕开关停）。

### 已知问题（非工程 bug）

- **寒暄句失真**：`"Good morning, how are you today?"` 译韩语时冒出「今天多少岁了？」。
  属模型能力问题，但提示 **质量回归集必须覆盖问候/寒暄类短句**。
- **`hard` 逐行翻日志会把级别词改掉**：模型把 `WARN` / `ERROR` 都写成 `INFO`
  （逐行丢失上下文，级别词被"顺手修正"）。→ 日志场景用 `soft` 或 `--no-segment`，或人工校对（已写进 README）。

### 待拍板决策（未定项）

| 决策项 | 现状 |
|---|---|
| 默认设备选 NPU 还是 CPU | 🔴 **未定**（见「CPU 更快导致默认设备缺性能理由」）。必须补功耗实测才能收口。当前默认 NPU |
| 功耗实测（watts / 1k token） | ⬜ 唯一还缺的维度，是上面那条的前置 |
| 长文本端到端：1000 字中文分段翻译是否 ≤60 s | ⬜ 验收指标里唯一未测项 |
| `--glossary` 术语干预 | ⬜ 启用后必须进 LRU key（`CacheKey.glossary` 已预留） |
| 依赖清单单一事实来源 | ⬜ `requirements.txt` 与 `pyproject.toml` 两处重复维护 |

---

## 16. 回归验收清单

### 16.1 真机清单（CLI，16 项）

| # | 场景 | 期望 |
|---|---|---|
| 1 | stdin 喂 UTF-8 日文 | 正确译出，无 `UnicodeDecodeError` |
| 2 | `--newline hard` 日志 4 行 | 输出 4 行，1:1 对齐 |
| 3 | soft 模式硬折行散文 6 行 | 输出 6 行 |
| 4 / 5 / 6 | `-o` / `--bom` / `--append` | UTF-8 无 BOM / 有 BOM / 追加无 BOM 污染 |
| 7 | 下游关闭管道（`\| more`） | 退出码 120，stderr 无 traceback |
| 8 | 重复段落（4 段中 2 段相同） | 复用 1 段（批内去重生效） |
| 9 | `--max-input-mb` 超限 | 退出码 1 |
| 10 | 空输入 / `--strict` 空输入 | 0 / 1 |
| 11 | hard 打包 8 行 | 输出 8 行，未退回逐行 |
| 12 | 缅甸语 / 阿拉伯语输出到管道 | 无 `UnicodeEncodeError` |
| 13 | `--no-warmup` | 正常译出，无 `RuntimeError` |
| 14 | 关停诊断开关打开 | stderr 出现线程 dump |
| 15 | `NPT_HARD_EXIT=0` | 退出码一致（自然退出路径仍可用） |
| 16 | 冷 NPUW 缓存后硬退出 | 二次加载明显变快（实测 32.4 s → 7.4 s，缓存确实落盘） |

### 16.2 自动化清单（秒级，不加载模型）

- **单测必须全过**：`pytest tests -q`，秒级，不加载模型。
  **不写具体条数** —— 每加一条用例这个数就会变，写死等于埋一颗必然过期的钉子；
  数量让测试自己说。
- **新增安全/配额类断言必须走 TestClient 打真实栈**：纯逻辑模拟的绿灯比没有绿灯更危险
  （见「WebUI（nputweb）· 可用性打磨轮」W16）。
- **配额类用例的采样点不能对齐心跳/淘汰间隔的整数倍**，否则在有 bug 的代码上也是绿的（W13）。
- **改了豁免集合要做红灯验证**：清空豁免后相关用例必须全红，否则说明用例没经过中间件。
- **常驻服务与真机部分**（端口、模型依赖）不适合进秒级套件，用一次性脚本人工确认。

### 16.3 nputweb 端到端（真机，一次性）

裸敲 `nputweb` 一键启动 · 非回环绑定自动生成并打印 token · `--host 0.0.0.0 --tls off` 拒绝启动（码 3）·
加 `--allow-insecure` 放行且仍强制 token · `--tls on` 只给 `--cert` 退 2 · 自有证书指纹按自有证书计算 ·
`--host 0.0.0.0 --no-auth --allow-no-auth` 下明文无鉴权也能起来且横幅红字告警 ·
`NPT_WEB_*` 与命令行等价 · 无 token 401 · 伪造 `Host` 400 · 9000 字符 413 · 超阈值 429 + `Retry-After` ·
队列打满 503 · 冷启动 health 由 `loading` → `ready` · 真机翻译出正确英文 · `zh-Hant` 出繁体 ·
下载 `.txt` 由前端 Blob 生成（服务端无写文件）· `/docs` `/openapi.json` 404 · Ctrl+C 进程退出端口释放 ·
`git status` 无私钥、无绝对路径。

---

## 17. 跨平台（Linux / ARM）可行性评审结论

> 本节只收结论。**决策全部待拍板**，评审过程未改动任何产品代码。

### 17.1 三条颠覆性发现（均为实测，不是推断）

| # | 发现 | 依据 |
|---|---|---|
| **F1** | **ARM 上没有 INT4 红利** —— 同一份 INT4 权重：x86_64 常驻 **2,169 MB**，aarch64 常驻 **7,961 MB**（**3.67×**）。官方原文：「Arm platforms execute quantized models in **simulation mode**: the whole model ... is executed in floating-point precision」。aarch64 的 caps 里**没有 BF16** | Docker 双平台实测 + 官方文档逐字引用 |
| **F2** | **Linux x86_64 上的 Intel NPU 是官方支持项**（NPU 3720 / Arrow Lake 正是参考机这一档）→ **「Linux 支持」≠「只能跑 CPU」**，早先的默认理解需要纠正 | 官方文档 + 内核 ≥6.6（`intel_vpu` 进主线，节点 `/dev/accel/accel0`） |
| **F3** | **Linux x86_64 的 CPU 翻译今天就能跑通，零代码改动** —— 双向翻译正确、退出码 0、编码无损；CLI 回归 10/10、真模型 3/3、nputweb 启停 + HTTPS + 鉴权 + Host 白名单全通 | 各自独立验证 |

**ARM 内存门槛（别被「≥8 GB 且留余量」误导）**：INT4 常驻 7,961 MB → **4 GB / 8 GB 板子直接出局**
（8 GB 内存配 7.9 GB 常驻 = 必然 OOM/swap，不是「留点余量」能解决的）。**建议门槛 ≥16 GB**。
8 GB 设备唯一可能的出路是**为 ARM 单独导出 fp16 权重**（预期约 4 GB 常驻），**但此数未实测** —— 复核通过前一律按「不支持」对外。

> ⚠️ **ARM 上没有「换个更小的模型」这条路**：HY-MT1.5 系列**最小档就是 1.8B**，无更小官方档位。
> 缓解选项只有两个：**fp16 权重**（降内存）或**换引擎**（llama.cpp 等）。

**Docker 的边界（结论句，别改写）**：**CI 工具，不是验收工具 —— 能证明「能装上」，不能证明「能用」**。
QEMU 约 50× 慢，且读不到 CPU 型号寄存器导致 oneDNN 走保守路径 → 性能数字、真机内存压力、NPU 均不可验。

### 17.2 三轨拆分

| 轨 | 目标 | 内容 | 验收 |
|---|---|---|---|
| **A · Linux x86_64（CPU）** | **v1 支持** | 平台感知默认设备、`setup_env.sh`、README 平台支持表、一行测试字面量修正 | `pytest -q` 全过；CLI 回归 10/10；真模型 3/3 |
| **B · Linux x86_64（NPU）** | **checklist 先行** | 内核 ≥6.6 + NPU 固件 / level-zero / 驱动编译器 + `libze1` + 用户入 `render` 组 + udev 规则 | 文档产出；端到端依赖真机 |
| **C · ARM64（CPU）** | **实验性 · 可安装**（常驻 7,961 MB） | 容器内 `pip install` + `pytest` + `--help/--languages/--devices` + `LANG=C` 编码验证；**不加载模型、不测性能** | 容器内全绿；**三处标注必须齐全** |
| **S0 · 跨平台既有 bug** | 无条件先修 | `/api/health` 上报**实际生效**设备（现在报的是请求的设备，ARM 上 100% 显示 `"NPU"`）+ 一行测试里写死的盘符字面量（改成 POSIX 风格即可，两边都返回同一结果，**不需要平台分支**） | 约半天封顶 |

> A+B+C+S0 合计约 3–4 天，**除 B 的端到端外不需要任何新硬件**。

### 17.3 待拍板决策

| 决策项 | 建议 |
|---|---|
| Linux x86_64 是否列 v1 支持目标 | **是**（成本已证明极低） |
| Linux x86_64 NPU 是否纳入本轮 | 只写 checklist，端到端等真机 |
| ARM64 对外定位 | **实验性 · 未验证性能 · 仅 CPU · 建议 ≥16 GB** |
| ARM 目标硬件档位 | 🔴 **硬阻塞，必须本人回答** |
| ARM 上 NPU / GPU / hetero 开关 | **禁用**，只列 CPU |
| 后端 / 引擎可插拔是否现在做 | **否**（单实现时抽象层是纯成本，且最难回退）。重开门槛 = 真机实测 < 8 tok/s |
| 非 NPU 平台默认设备改 `auto` + health 上报实际设备 | **是** —— 但见「硬约束」第 1 条，**顺序不能反** |
| Linux 安装与分发方式 | venv + `setup_env.sh` |
| Linux 模型目录默认值 | `NPT_MODEL_DIR` 已够，文档明示即可 |
| aarch64 镜像 / 轮子缺口处理 | **容器内改用阿里云或官方 PyPI**（清华对容器请求返 **403**，宿主直连是 200 且 aarch64 轮子 6/6 齐全） |
| ARM 晋升门槛（对齐「实测基线」：≥8 tok/s / 单句 P95 ≤5 s） | 待真机 |
| arm64 CI 是否常驻 | **降级为交接条目**。重开条件：已有 x86_64 CI 基座，或出现分发需求 |

### 17.4 必答问题（一律用稳定短名）

| 短名 | 问题 | 优先级 |
|---|---|---|
| **`Q-DEMAND`** | **有没有一个具体的人或场景非要 Linux 不可？** 目前只听到技术诉求，没听到需求。这条不定，整个 Linux 投入的方向可能就是错的 | 🔴 **最高** |
| **`Q-ARM-HW`** | ARM 目标硬件是哪一档？型号 + 内存多大？ | 🔴 阻塞全部 ARM 工作 |
| **`Q-X86-REAL`** | Linux 跑在什么机器上？当前参考机双系统，还是另一台？ | 🟠 阻塞轨 B 验收 |
| `Q-ACCEPT` | 「跑通」的定义。建议沿用「实测基线」的 ≥8 tok/s + 单句 P95 ≤5 s 作为「支持」门槛，达不到只标「可安装」 | 🟡 |
| `Q-FORM` | Linux 上是 CLI 还是常驻 HTTP 服务？（答「服务」→ 佐证服务层应排在 Linux CLI 之前） | 🟡 |
| `Q-LABEL` | 能否接受 ARM 在 README / `--devices` / `/api/health` **三处**都标 Experimental / Unverified？ | 🟡 |
| `Q-GLIBC` | 目标发行版能自己选吗？glibc ≥ 2.35 是**硬门槛**（Ubuntu 22.04 ✓ / Debian 12 ✓ / RHEL 9 ✓；Ubuntu 20.04 ✗ / Debian 11 ✗ / Alpine-musl ✗） | 🟡 |
| `Q-CODE` | 能否接受「本轮先不动产品代码，只改规格书 + 文档 + S0 两个小修」？ | 🟡 |

> **`Q-DEMAND` 不能丢**：若答案是「想在 ARM 小盒子上常驻给家人用」，
> 则 7.9 GB 常驻 + 单板机出局 → **ARM 整块当场死亡**，后面所有 ARM 问题都不必问。
> 另：Ubuntu 24.04 默认 Python 3.12，而 aarch64 轮子目前只确认到 cp311 → 安装文档必须写明自行装 3.11。

### 17.5 硬约束（与既有决策的耦合，改之前必须先读）

1. 🔴 **「默认设备改 `auto`」不能绕过「CPU 更快导致默认设备缺性能理由」那一条**。
   `config.py` 默认设备写死 `npu`，改成 `auto` 看着是顺手的平台适配，
   但默认设备选 NPU 还是 CPU 这件事**未定**（卡在缺功耗实测）。
   → **没拍板之前改默认值 = 从后门绕开决策**。
   正确顺序：先拍板，再谈默认值；或**只做运行时行为**（探测不到 NPU 就降级 CPU），**不动默认值配置**。
2. 🔴 **arm64 CI 的前提不成立**：本仓库**没有任何 CI 基础设施**（无 `.github/`、无 `Makefile` / `tox.ini` /
   `Jenkinsfile` / `.gitlab-ci.yml` / `.pre-commit-config.yaml`，只有 `pyproject.toml` 里的 pytest 配置）。
   → 「纳入 CI」实际是**从零搭 CI**（另一个量级的工程），不是顺手加一项。
3. **排期的正确表述**：S0–S4（不需真机，可先做）→ **服务层按自己的节奏推进，不接受「等硬件」作为阻塞理由**
   → 轨 B / 轨 C（拿到硬件才动）。理由：轨 B/C 依赖 `Q-X86-REAL` / `Q-ARM-HW`，
   **这两台硬件可能永远不来**；而服务层是平台无关的、不需要任何硬件。
4. **轨 C 的三处标注必须齐全**：① README 顶部写明「可安装 ≠ 支持」；② `--devices` 输出带 `experimental`；③ `/api/health` 返回 `platform` + `verified: false`。
5. **S3（ARM 可安装取证）设为条件触发**：挂在 `Q-DEMAND` 之后 —— 若它让 ARM 整块出局，S3 就不必做。
6. **IR 交叉导出仍是未验证的前提**，不是已确认事实。「ARM 导出链路不是阻塞」这个缓解点整条建立在它之上 → 先标未验证。
7. **`hetero` 在 ARM 上会开两条 CPU pipeline ≈16 GB 且零加速** → 可用设备族 < 2 时直接拒绝 hetero。
8. **非 NPU 平台上 `[FAIL]` 是误导**：`probe_device.py` 在无 NPU 时报 `[FAIL]` + `return 2`，
   把「本平台无 NPU」报错成「环境坏了」→ 应降为 WARN。

### 17.6 顺带修正的两条既有认知

- **「清华镜像缺 aarch64 轮子」不成立**：根因是**镜像对容器请求返 403**（宿主直连 200 且轮子齐全）。
  这是**安装取源问题，不是运行期缺失、也不是平台能力缺失** —— 换个源就能装上。
- **「已排除的方案」表需要加作用域声明**：该表的排除结论默认在 **Windows + NPU** 语境下成立，
  换平台（Linux / ARM）需重新评估（例：NLLB-200 在 ARM CPU 上未必还是死路）。
