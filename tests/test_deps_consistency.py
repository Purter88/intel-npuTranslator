"""依赖清单一致性：`requirements.txt` 与 `pyproject.toml` 不得漂移。

背景（2026-09-15）：`cryptography` 只在 pyproject 的 `web` extra 里声明了，
`requirements.txt` 里漏掉 —— 而 `scripts/setup_env.ps1` 走的是 requirements.txt，
照它从零跑一遍，`nputweb` 起不来（Windows 没有自带 openssl 命令，
cryptography 是自签证书的唯一来源，见 SPEC.md · WebUI（nputweb））。

两层断言：
1. **相对**：两份清单的包集合与版本约束必须一致 —— 抓单向漂移（今天这个 bug 属于此类）
2. **绝对**：运行时必需包必须两份都有 —— 兜底，防将来有人两边同时删掉同一个包
   （注意：今天这个 bug 只有 pyproject 侧有，靠第 1 层就能抓到；
    第 2 层防的是"两边都删"这种第 1 层比不出来的情况）
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 运行时真正会被 import 的包（不含 pytest/ruff/mypy 这类开发工具）。
# 少一个都可能在"装完依赖"之后才炸，所以写成显式清单而不是靠两边互相比较。
REQUIRED_RUNTIME = [
    "openvino",
    "openvino-genai",
    "openvino-tokenizers",
    "typer",
    "modelscope",
    "psutil",
    "fastapi",
    "uvicorn",
    "cryptography",
]

# pyproject 里运行时依赖的所在位置：`dependencies` + `optional-dependencies.web`
_WEB_EXTRA = "web"

_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<spec>[!<>=~;].*)?$"
)


def _normalize(name: str) -> str:
    """PEP 503 规范化：小写、`-` 与 `_` 等价。"""
    return name.strip().lower().replace("-", "_")


def _parse_requirement(line: str) -> tuple[str, str, str] | None:
    """解析一条 PEP 508 需求，返回 (规范名, extras, 版本约束)。

    不做完整 PEP 508 解析 —— 只覆盖本项目用到的形态
    （`pkg`、`pkg==1.2.3`、`pkg[extra]`、`pkg[extra]==1.2.3`）。
    """
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("-"):
        return None
    # 去掉行内注释
    line = line.split(" #", 1)[0].strip()
    if not line:
        return None
    m = _REQ_RE.match(line)
    if not m:
        return None
    return (
        _normalize(m.group("name")),
        (m.group("extras") or "").strip(),
        (m.group("spec") or "").strip(),
    )


def _read_requirements() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        parsed = _parse_requirement(raw)
        if parsed:
            name, extras, spec = parsed
            out[name] = (extras, spec)
    return out


def _read_pyproject() -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})

    def collect(items: list[str]) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for item in items:
            parsed = _parse_requirement(item)
            if parsed:
                name, extras, spec = parsed
                out[name] = (extras, spec)
        return out

    core = collect(project.get("dependencies", []))
    web = collect(project.get("optional-dependencies", {}).get(_WEB_EXTRA, []))
    return core, web


@pytest.fixture(scope="module")
def reqs() -> dict[str, tuple[str, str]]:
    return _read_requirements()


@pytest.fixture(scope="module")
def pyproject_deps() -> dict[str, tuple[str, str]]:
    """pyproject 的运行时依赖全集 = dependencies ∪ optional-dependencies.web。"""
    core, web = _read_pyproject()
    merged = dict(core)
    merged.update(web)
    return merged


# ---------------------------------------------------------------- 第 1 层：相对


def test_requirements_and_pyproject_have_same_packages(reqs, pyproject_deps):
    """两份清单的包集合必须一致。

    任一侧多出来的包都算漂移 —— 少的一方会让"按那份清单装出来的环境"缺东西。
    """
    only_req = sorted(set(reqs) - set(pyproject_deps))
    only_py = sorted(set(pyproject_deps) - set(reqs))
    assert not only_req and not only_py, (
        "requirements.txt 与 pyproject.toml 的包集合不一致："
        f"\n  只在 requirements.txt: {only_req}"
        f"\n  只在 pyproject.toml  : {only_py}"
    )


def test_requirements_and_pyproject_agree_on_constraints(reqs, pyproject_deps):
    """同名包的 extras 与版本约束必须一致。

    版本约束不一致最阴险：两边都能装成功，但装出来的版本不一样。
    """
    mismatched: list[str] = []
    for name in sorted(set(reqs) & set(pyproject_deps)):
        r_extras, r_spec = reqs[name]
        p_extras, p_spec = pyproject_deps[name]
        if _normalize(r_extras) != _normalize(p_extras) or _normalize(r_spec) != _normalize(p_spec):
            mismatched.append(
                f"  {name}: requirements='{r_extras}{r_spec}' vs pyproject='{p_extras}{p_spec}'"
            )
    assert not mismatched, "两份清单的版本约束/extras 不一致：\n" + "\n".join(mismatched)


# ---------------------------------------------------------------- 第 2 层：绝对


@pytest.mark.parametrize("package", sorted(_normalize(p) for p in REQUIRED_RUNTIME))
def test_required_runtime_package_declared_everywhere(package, reqs, pyproject_deps):
    """运行时必需包必须在两份清单里都声明。

    这一层是兜底：第 1 层的"互相比较"比不出"两边同时删掉同一个包"的情况。
    """
    assert package in reqs, (
        f"'{package}' 未出现在 requirements.txt —— "
        "scripts/setup_env.ps1 走的就是这份清单，漏了会导致装完的程序缺依赖"
    )
    assert package in pyproject_deps, (
        f"'{package}' 未出现在 pyproject.toml 的 dependencies 或 [{_WEB_EXTRA}] extra 里"
    )


def test_uvicorn_uses_standard_extra(reqs, pyproject_deps):
    """uvicorn 必须带 [standard] extra。

    裸 uvicorn 不带 websockets / httptools / uvloop，nputweb 的性能与功能都受影响。
    """
    for source, table in (("requirements.txt", reqs), ("pyproject.toml", pyproject_deps)):
        extras, _ = table.get("uvicorn", ("", ""))
        assert "standard" in extras, (
            f"{source} 里的 uvicorn 缺少 [standard] extra（当前: '{extras}'）"
        )
