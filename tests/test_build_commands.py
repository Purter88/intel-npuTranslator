"""守着「`scripts/build.py` 的 `COMMANDS` 必须跟着 `pyproject.toml` 的 console scripts 走」。

## 为什么需要这条测试

`nputserve`（M3 的第三个命令）在 `pyproject.toml` 里早就定义好了 console script，
但 `scripts/build.py` 的 `COMMANDS` 忘了加它 —— 结果一键部署生成不出
`nputserve.cmd`，用户把 `bin\\` 加进 PATH 之后**拿不到这个命令**。

这种缺口的恶劣之处在于：它的表现是「**少了东西**」而不是「报错」。跑测试不会红、
看代码也不会觉得哪里不对，只能靠一条断言把不变式钉住。

## 断言的是不变式，不是具体数字

这里**不**写死「3 个命令」，也**不**写死元组内容。写死的话，下次新增命令时这条测试
就会变成噪音红灯 —— 而噪音红灯的下场通常是被人删掉，等于没有测试。

真正要守的不变式是：

* 每个 console script target（`module:func`）**只导出一个命令名**；
* 导出的是**短名**（长名别名如 `npu-translate` 只保留兼容，不进 PATH，
  见 build.py 文件头第 1 条约定）。

这两条可以直接从 `pyproject.toml` 推出来，所以加新命令时不需要改本文件。
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = ROOT / "scripts" / "build.py"
PYPROJECT = ROOT / "pyproject.toml"

# build.py 是脚本不是包，只能按文件路径加载。
_MODULE_NAME = "_nput_build_under_test"


def _load_build_module() -> Any:
    """按文件路径加载 scripts/build.py。

    ⚠️ **必须先在 `sys.modules` 里注册，再 `exec_module`**：
    被加载的模块里有 `from __future__ import annotations`，`@dataclass` 会按
    `cls.__module__` 回查 `sys.modules`，没预注册会炸
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    而报错点落在**被加载脚本**的 `@dataclass` 那一行，看着像脚本本身有语法问题，
    极难排查（software-engineer-2-2 踩过这个坑）。
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, BUILD_PY)
    assert spec is not None, f"无法为 {BUILD_PY} 构造 module spec"
    assert spec.loader is not None, f"{BUILD_PY} 没有 loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module  # ← 必须在 exec_module 之前注册
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(_MODULE_NAME, None)
    return module


@pytest.fixture(scope="module")
def build_mod() -> Any:
    """加载被测的 build.py（模块级，只加载一次）。"""
    return _load_build_module()


def _console_scripts() -> dict[str, str]:
    """读 pyproject.toml 的 [project.scripts]。tomllib 是 3.11 自带，不引第三方库。"""
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return dict(data["project"]["scripts"])


def _expected_exports(scripts: dict[str, str]) -> dict[str, str]:
    """从 pyproject 推出「应该被导出哪些命令」：`{命令名: 模块路径}`。

    * 按 target（`module:func`）分组，**每个 target 只导出一个命令名**；
    * 同一 target 的多个别名里取**最短**的那个 = 短名优先
      （`nputr` 胜出、`npu-translate` 只留作兼容）。
    """
    aliases_by_target: dict[str, list[str]] = {}
    for name, target in scripts.items():
        aliases_by_target.setdefault(target, []).append(name)
    return {
        min(names, key=len): target.split(":", 1)[0]
        for target, names in aliases_by_target.items()
    }


def test_commands_match_pyproject_console_scripts(build_mod: Any) -> None:
    """COMMANDS 与 pyproject 的 console scripts 必须一致 —— 多了少了都要红。"""
    expected = _expected_exports(_console_scripts())
    actual = dict(build_mod.COMMANDS)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    assert actual == expected, (
        "scripts/build.py 的 COMMANDS 与 pyproject.toml 的 [project.scripts] 不一致。\n"
        f"  pyproject 期望导出: {sorted(expected)}\n"
        f"  build.py 实际导出: {sorted(actual)}\n"
        f"  少了: {missing}\n"
        f"  多了: {extra}\n"
        "  少一个 = 用户一键部署后拿不到这个命令（M3 的 nputserve 就是这么漏的）；\n"
        "  多一个 = 把不该进 PATH 的东西导出去了。"
    )


def test_every_command_module_matches_pyproject(build_mod: Any) -> None:
    """逐项比对模块路径：pyproject 写 `module:func`，COMMANDS 只存 `module`。"""
    scripts = _console_scripts()
    for name, module in build_mod.COMMANDS:
        assert name in scripts, (
            f"COMMANDS 里的 {name!r} 在 pyproject 的 [project.scripts] 里没有对应条目"
        )
        target_module = scripts[name].split(":", 1)[0]
        assert module == target_module, (
            f"{name}: build.py 写的是 {module!r}，pyproject 里是 {target_module!r}"
        )


def test_docstring_lists_every_exported_command(build_mod: Any) -> None:
    """文件头 docstring 必须点名每个导出的命令 —— 守「文档与代码一起改」。

    单独加这条是因为：只改 COMMANDS 而忘了改文档，不会有任何测试变红，
    但文件里会留下「约定说只导出两个、代码却导出三个」这种自相矛盾的说法。
    """
    doc = build_mod.__doc__ or ""
    for name, _ in build_mod.COMMANDS:
        assert name in doc, (
            f"scripts/build.py 的 COMMANDS 导出了 {name!r}，但文件头 docstring 没提到它 —— "
            "加命令时请同步改文档，以及那条「只导出 N 个命令」的硬约定"
        )
