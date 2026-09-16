# 贡献指南

谢谢你想动手。**项目小、规则少，但有几条是踩过坑换来的** —— 下面每一条都写了"为什么"，
读一遍能省你一轮 review。

---

## 0. 动手前先读什么

| 文档 | 什么时候读 |
|---|---|
| [`README.md`](README.md) | 想先知道这东西怎么用 |
| **[`docs/SPEC.md`](docs/SPEC.md)** | **动手改代码前必读** —— 技术契约与唯一事实来源 |

`docs/SPEC.md` 是**活契约**，不是归档文档。里面有实测基线、踩坑记录、活跃风险和"已排除的方案（别重走）"。
**与它冲突的实现，先改它再改代码** —— 顺序反了，等于把契约变成了事后追认。

---

## 1. 搭环境

### 方式一：一键部署（推荐）

```powershell
python scripts\build.py                 # 交互式（会问要不要下模型）
python scripts\build.py --skip-model    # 非交互，跳过模型
python scripts\build.py --list-models   # 看可选模型
```

它做四件事：**建 venv → 装依赖 → 选模型 → 生成 `nputr` / `nputweb` 两个命令转发脚本**。

- ⚠️ **第三个命令 `nputserve` 不在 `bin\` 里。** `scripts/build.py` 只导出 `nputr` / `nputweb`
  两个转发脚本（见该文件头第 1 条约定），`nputserve` 只由 `pyproject.toml` 的 console script 提供，
  装完后在 `.venv\Scripts\nputserve.exe`。要用它，把 `.venv\Scripts` 也加进 PATH，
  或者直接 `.\.venv\Scripts\python.exe -m npu_translator.server`。
  它默认 **8766**（`nputweb` 是 8765）—— 两个命令**会**同时起，故意错开。
- 🔴 **它不碰系统 PATH**（也不碰注册表，不需要管理员）。跑完会打印一个目录，你自己加进 PATH。
- 加 PATH 请用 `[Environment]::SetEnvironmentVariable(...)`，**不要用 `setx`** ——
  `setx` 会把 `%USERPROFILE%` 这类变量展平成死字符串，还有 1024 字符截断。细节见 `README.md`。

### 方式二：手动三步

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt            # 运行时依赖
pip install -r requirements-dev.txt        # 开发（跑测试）再加这一份
.\.venv\Scripts\python.exe scripts\fetch_model.py --repo rainhenry/HY-MT1.5-1.8B-int4-ov-npu
```

Python 用 **3.11**（`requirements.txt` 顶部注明了原因：3.11 的 wheel 最全）。

模型约 1.02 GB，**不是所有改动都需要它** —— 跑单测不需要，见下节。

---

## 2. 跑测试

```powershell
pytest tests -q                      # 全量，秒级（约 10 秒），不加载模型
pytest tests -q -k segment           # 只跑名字里带 segment 的
pytest tests -q tests/test_pool.py   # 只跑一个文件
```

套件**不加载模型、不碰 NPU**，纯逻辑。所以：

- 没有 NPU 的机器照样全绿
- 没下模型权重也照样全绿
- 不需要 OpenVINO 也能全绿（下面 CI 那节说了为什么这很重要）

### 🔴 `--basetemp`：最好别给

如果必须给（比如你的 shell 传不了绝对路径），三条：

1. **不要指向仓库内。** 证书相关用例会在 basetemp 里**生成自签私钥**。
   残骸留在仓库里，一次 `git add -A` 就把它提交了 —— **一把私钥进了版本库就是永久泄漏**，
   重写历史也救不回已经 clone 过的人。
2. **不要复用同一个目录。** pytest 的 `tmp_path` 会在 basetemp 下建 `test_xxx0` 这样的子目录，
   复用的话会撞上上一轮的残骸，报出一堆看不懂的错误。
3. **首选是根本不给这个参数**：pytest 默认用系统临时目录，并且**自动加编号**，天然不撞、天然在仓库外。

仓库的 `.gitignore` 已经排除了 `.pytest_tmp*` 与 `.pytest_cache/`，但那是**命名约定**的兜底，
指望不上 —— 真正的保护是"临时目录本来就不在仓库里"。

### 真机验证（改了设备 / 模型 / 量化才需要）

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py --device NPU   # 冒烟：5 语种 × 3 句
nputr -b                                                        # 跨平台性能基准
.\.venv\Scripts\python.exe scripts\bench.py --all               # 三设备基准
```

`docs/SPEC.md · 执行约定` 第 4 条：涉及设备/模型/量化的改动，**跑完把结果贴回来**。

---

## 3. 代码约定

### 3.1 惰性导入是硬约束，不是风格偏好

| 模块 | 约束 |
|---|---|
| `import npu_translator` | **不得拉起 OpenVINO** |
| `import npu_translator.web` | **不得拉起 fastapi / uvicorn / cryptography** |
| `service.py` | **不得 import fastapi**（纯逻辑，脱离网络栈可单测）。要复用 `web/routes.py` 的脱敏函数就**延迟 import** |
| `server.py` | **唯一**允许在模块级 import fastapi 的文件；且不被 `__init__.py` 也不被 `web/` 引用 |

**为什么**：OpenVINO 初始化有百毫秒级开销且常驻占内存，只想读个语种表的使用者不该为此买单；
`[web]` / `[server]` 是**可选**依赖，顶层 import 会让"没装 extra 的环境"直接 `ImportError`，
连一句"请先 `pip install -e .[web]`"的友好提示都来不及说。

好处不止是体验：正因为 OpenVINO 是惰性的，**CI 不装它也能跑完全量单测**（见第 5 节）。
加一处模块级的 `import openvino`，CI 就得跟着胖几百 MB —— 别加。

### 3.2 编排只走 `orchestrate.py`

分段 / 批内去重 / 译文缓存 / 回填 / 打包行数校验 / 按行拼接，**一律在 `orchestrate.Translator` 里**。
CLI（`cli.py`）、WebUI（`web/`）、服务（`service.py`）都只是调用方。

**为什么**：这些细节全是踩过坑的（批内去重、hard 模式打包行数校验……），复制多份必然各自漂移。

调用方保留的只有**映射权**：同一件"部分失败"，CLI 映射成退出码 4，
WebUI / 服务映射成 200 + `failed` 计数（服务的 `strict=true` 才升 500）。
见 `docs/SPEC.md · 架构与目录结构`。

### 3.3 注释引用规格书：写章名，不写编号

写法固定为 `SPEC.md` + `·` + **章名**，章名只能是 `docs/SPEC.md` 的**一级或二级标题原文**：

| 写法 | 判定 |
|---|---|
| `SPEC.md · CLI 管道契约` | ✅ |
| `SPEC.md · 已知问题与活跃风险 · 跨机器默认值不可外推` | ✅（第二段取三级标题，只作精度，不参与校验） |
| `SPEC.md · CLI 管道契约 的语义` | ❌ 章名后面追加了说明文字 |
| `SPEC.md · 活跃风险 R13` / `§4.2` / `D8` | ❌ 编号会漂 |

> 上表最后两行是**故意写错**的反例（与 `docs/SPEC.md` 开头的对照表同一套路）。
> 除这两行之外，本仓库每一处规格书引用（`SPEC.md` + `·` + 章名）都必须指向真实存在的标题 ——
> `tests/test_spec_references.py` 会把漂移变成红灯。

三条理由：

- 编号会随结构调整漂移，**撞号会立刻被发现，语义重复不会** —— 后者会潜伏到拍板那天，
  然后同一个问题拍出两个矛盾的结论。
- 章名后面**一律不得追加说明文字**（需要更细的落点就再开一个三级标题），否则引用本身就不再是标题。
- 有 `tests/test_spec_references.py` 守着：指向不存在的章节会**直接红灯**，
  SPEC.md 的章节清单也做了快照 —— 改标题必须显式回来改快照。

完整规则见 `docs/SPEC.md · 地位与稳定性契约`。

### 3.4 注释写中文，写"为什么"

```python
# ❌ 复述代码
tokens += 1

# ✅ 写"为什么"：这一段不写清楚，下一个人一定会改回去
# NPU 是单流设备，两条 pipeline 只会互相抢；这里的锁是**显式**的，
# 因为 engine 内部那把隐式锁没法被超时/观测（SPEC.md · NPU 实现要点）
```

"写了什么"代码里已经有了；**"为什么这么写、改掉会怎样"才是注释唯一该承载的信息**。

### 3.5 常量集中在 `config.py`

写死的常量（模型路径、`MAX_PROMPT_LEN`、量化参数、超时）必须能从 `config.py` 覆盖，
**不散落在业务代码里**。见 `docs/SPEC.md · 执行约定` 第 3 条。

### 3.6 改依赖清单要改两份

`requirements.txt` 与 `pyproject.toml` 的运行时依赖必须一致（包集合 + 版本约束都要一致）。
**有 `tests/test_deps_consistency.py` 守着** —— 漂移会直接红灯。

为什么两份并存：`pyproject.toml` 给 pip / 打包用，`requirements.txt` 给 `scripts/*.ps1` 与快速部署用。
历史上就是因为只在 `pyproject` 的 `[web]` extra 里加了 `cryptography`、`requirements.txt` 漏了，
照 `requirements.txt` 从零装一遍 `nputweb` 起不来（Windows 没有自带 openssl 命令）。

### 3.7 Git 约定：本机敏感信息禁止入库

**不得出现**：本机绝对路径（盘符开头的 Windows 路径、POSIX 家目录下的路径）、真实邮箱、PEM 私钥块。
写路径一律用**相对路径**或 `~` 开头的写法。见 `docs/SPEC.md · Git 约定：本机绝对路径禁止入库`。

也要一并排除：`models/` · `.npucache/` · `~/.nputweb/` · `bin/` · 任何 PEM 证书与私钥文件。

🔴 **别用 `git add -A`**。提交前过一遍 `git status` —— 这类泄漏不会报错，只会安静地躺在
JSON 报告、错误信息、临时目录残骸里。

---

## 4. 提交

### 提交前

1. `pytest tests -q` **全绿**
2. 涉及设备 / 模型 / 量化 → 跑 `scripts/smoke_test.py` 或 `nputr -b`，把结果贴进 PR
3. 改了契约 → **先改 `docs/SPEC.md`，再改代码**
4. `git status` 人工过目（见 3.7）

### commit message：写"做了什么 + 为什么"

```
orchestrate: hard 模式打包后校验行数

hard 模式把多行打成一批翻译，模型有时会少返回一行，
原来的实现直接拼接导致行数错位、日志与译文对不上。
现在打包前后各记一次行数，不一致就退回逐行。
```

第一行说**做了什么**（祈使句，别写句号）；正文说**为什么** —— 尤其是"看上去没必要但其实是坑"的那部分。
只写 "fix bug" 的 commit，三个月后连你自己都读不懂。

---

## 5. CI 的说明（改 workflow 前先看）

CI 在 `.github/workflows/ci.yml`。它的核心取舍：**不装 OpenVINO**。

- `requirements.txt` 含 `openvino`（数百 MB），每轮 matrix 都装会非常慢。
- 而**全量单测一个都不需要它** —— 惰性导入（见 3.1）保证 `import npu_translator` 不拉起 OpenVINO 运行时。
- 实测：只装 `pytest` / `typer` / `click` / `fastapi` / `httpx` / `cryptography`，
  406 项里 **405 通过、1 项 skip**。

所以 CI 装的是一份**手写的"最小测试依赖"**，既不装 `requirements.txt`，也不 `pip install -e .`
（后者会把 `pyproject.toml` 里的 openvino 一并拉下来）。
测试靠 `pyproject.toml` 的 `pythonpath = ["src"]` + `tests/conftest.py` 找到源码。

完整依赖能不能装上，另有一个**每周 / 手动触发**的重型 job 去验（那个会真装 openvino）。

🔴 **别为了让 CI 变绿去改测试或放宽断言。** CI 红了先怀疑环境，再怀疑代码，最后才怀疑断言；
确实有跑不了的用例，就明说并给出方案（标记 skip 的理由、或拆分 job），不要偷偷改。

---

## 6. Windows 本机环境的一个坑

如果你在 Windows 上用 bash（PortableGit / Git Bash 之类）：**它的 coreutils 是残缺的**，
`ls` / `grep` / `wc` / `sort` / `head` / `tail` 全都没有。

想过滤、统计、看文件尾部，**写 Python 脚本**，别跟 shell 较劲：

```powershell
python -c "import pathlib,collections; print(collections.Counter(p.suffix for p in pathlib.Path('src').rglob('*')))"
```

PowerShell 是完整的，优先用它。

---

## 7. 报告问题 / 提想法

- Bug / 功能建议：走 [issue 模板](.github/ISSUE_TEMPLATE/)（`--debug` 输出很有用，但**先脱敏**）
- 安全漏洞：**不要开公开 issue**，走 Security Advisories 私密报告，见 [`SECURITY.md`](SECURITY.md)
