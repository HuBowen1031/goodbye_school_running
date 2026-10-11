"""集成测试：``scripts/resolve_python.sh`` 能否解出**真正可用**的 vrun 解释器。

这是 ``docs/findings.md`` **F7 的回归护栏**。

F7 的成因是文档把解释器绝对路径写死成 ``miniconda3/envs/vrun/bin/python``，
而本机 conda 的 ``envs_dirs`` 把环境装在了 ``~/.conda/envs/vrun`` ——
该硬编码路径根本不存在，Swift 侧车会在启动时直接失败。

因此这里断言的不是「等于某个固定字符串」（那本身就是硬编码），
而是**解析出来的解释器确实可用、且确实是本项目的那一个**：

- 存在、可执行、是 Python 3；
- 版本与 ``environment.yml`` 声明的 3.12 一致；
- 能 import 项目声明的依赖（``numpy`` / ``pymobiledevice3``）；
- 环境名不存在时**报错退出**，而不是静默回落到某个错误的解释器。

只要哪天又有人把路径写死回去，这些用例就会红。

> 与 ``tests/`` 下其余用例不同，本文件依赖本机 conda 环境，属集成测试。
> 非 macOS 或系统无 ``/bin/zsh`` 时整体跳过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = REPO_ROOT / "scripts" / "resolve_python.sh"
ZSH = "/bin/zsh"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path(ZSH).exists(),
    reason="resolve_python.sh 是 macOS/zsh 脚本",
)


def _resolve(**env_overrides: str | None) -> subprocess.CompletedProcess:
    """在干净环境里跑一次解析器。

    先把可能干扰的 ``VRUN_*`` 变量摘掉；传 ``None`` 表示该变量保持未设置。
    """
    env = os.environ.copy()
    for key in ("VRUN_PYTHON", "VRUN_ENV_NAME", "VRUN_RESOLVE_VERBOSE"):
        env.pop(key, None)
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    return subprocess.run(
        [ZSH, str(RESOLVER)],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture(scope="module")
def resolved() -> str:
    """本机解析出的解释器路径（模块内只解析一次）。"""
    proc = _resolve()
    assert proc.returncode == 0, (
        f"解析失败（exit={proc.returncode}）\nstderr:\n{proc.stderr}"
    )
    return proc.stdout.strip()


# ── 脚本自身 ──────────────────────────────────────────────────────

def test_resolver_exists_and_is_executable() -> None:
    assert RESOLVER.is_file(), f"缺少解析器：{RESOLVER}"
    assert os.access(RESOLVER, os.X_OK), "resolve_python.sh 没有可执行位"


# ── 正常路径 ──────────────────────────────────────────────────────

def test_output_is_a_single_absolute_path(resolved: str) -> None:
    assert resolved.startswith("/"), f"不是绝对路径：{resolved!r}"
    assert "\n" not in resolved, "输出不止一行，无法安全用于命令替换"
    assert Path(resolved).is_file(), f"解析出的路径不存在：{resolved}"
    assert os.access(resolved, os.X_OK), f"解析出的路径不可执行：{resolved}"


def test_resolved_interpreter_is_python_312(resolved: str) -> None:
    """版本须与 environment.yml 声明的 python=3.12 一致。"""
    proc = subprocess.run(
        [resolved, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "3.12", (
        f"期望 Python 3.12，实际 {proc.stdout.strip()}（{resolved}）"
    )


def test_resolved_interpreter_has_project_dependencies(resolved: str) -> None:
    """真正证明「解出的是本项目那个环境」，而不是恰好是 Python 3 的别的环境。"""
    proc = subprocess.run(
        [resolved, "-c", "import numpy, pymobiledevice3"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"{resolved} 里缺少项目依赖，解错环境了？\n{proc.stderr}"
    )


# ── 失败与覆盖路径 ────────────────────────────────────────────────

def test_unknown_env_name_fails_instead_of_silently_falling_back() -> None:
    """环境名不存在时必须报错 —— 静默换一个解释器比直接失败危险得多。"""
    missing = f"vrun-absent-{uuid.uuid4().hex[:8]}"
    proc = _resolve(VRUN_ENV_NAME=missing)
    assert proc.returncode != 0, f"环境 {missing} 不存在，却解析成功了：{proc.stdout!r}"
    assert missing in proc.stderr, "失败信息里应指出是哪个环境名找不到"


def test_override_pointing_at_non_python_is_rejected() -> None:
    """VRUN_PYTHON 指向非 Python 时必须拒绝，不能让错误推迟到侧车启动才暴露。"""
    proc = _resolve(VRUN_PYTHON=ZSH)
    assert proc.returncode != 0, f"{ZSH} 不是 Python，却被接受了：{proc.stdout!r}"
    assert "VRUN_PYTHON" in proc.stderr


def test_override_takes_precedence_over_env_lookup(resolved: str) -> None:
    """VRUN_PYTHON 应短路全部探测 —— 即使环境名同时是错的，也要成功。"""
    missing = f"vrun-absent-{uuid.uuid4().hex[:8]}"
    proc = _resolve(VRUN_PYTHON=resolved, VRUN_ENV_NAME=missing)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == resolved
