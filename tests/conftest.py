"""pytest 全局配置。

把 ``src/`` 注入 ``sys.path``，让测试无需 ``pip install -e .`` 就能 ``import vrun``。
这是刻意选择的最小侵入方案：仓库里因此不必多出一个 ``pyproject.toml``。

同时提供几个共享 fixture。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vrun.noise import GPSNoise  # noqa: E402
from vrun.profile import SpeedProfile  # noqa: E402
from vrun.route import Route  # noqa: E402

# 江安校区附近的参考点（与 cli.DEFAULT_ORIGIN 一致）
LAT0, LON0 = 30.5535, 103.9910


@pytest.fixture
def square_route() -> Route:
    """一个约 2 km 的方形环，测试里最常用的赛道形状。"""
    d = 0.0045
    return Route([
        (LAT0, LON0),
        (LAT0 + d, LON0),
        (LAT0 + d, LON0 + d),
        (LAT0, LON0 + d),
        (LAT0, LON0),
    ])


@pytest.fixture
def route_3km(square_route: Route) -> Route:
    """正好 3.00 km 的赛道（R1 的下限）。"""
    return square_route.scaled_to(3000.0)


@pytest.fixture
def default_profile() -> SpeedProfile:
    return SpeedProfile()


@pytest.fixture
def noise_realistic() -> GPSNoise:
    return GPSNoise(level="realistic", sample_rate=1.0, seed=0)


# ── async 测试的零依赖支持 ──────────────────────────────────
#
# 为什么不用 pytest-asyncio / anyio
#     本项目**不为测试引入新的第三方依赖**（file-manifest §4.5 依赖纪律：
#     纯计算层只允许 numpy，测试依赖也不额外增加）。pytest-asyncio 会让
#     `requirements.txt` 多一个开发期依赖，而它做的事只有「把协程跑起来」。
#
# 为什么不用每个测试自己 asyncio.run()
#     设备层（device.py / safety.py）是 async 的，测试会成片地需要它。
#     而且**一个测试里的多个 await 必须共享同一个 event loop** ——
#     SafetyGuard 在 arm() 时会抓 loop 句柄，跨循环调用会掩盖真实行为。
#     包装器按「每个测试一个 loop」执行，正好符合这个要求。
#
# 现有的同步测试全部走原来的路径，不受影响。

def pytest_pyfunc_call(pyfuncitem):
    """遇到 ``async def test_*`` 就用 ``asyncio.run`` 跑掉。

    返回 ``None`` 表示「不是 async 测试」，交回 pytest 的默认处理。
    """
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name]
              for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True
