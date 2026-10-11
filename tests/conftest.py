"""pytest 全局配置。

把 ``src/`` 注入 ``sys.path``，让测试无需 ``pip install -e .`` 就能 ``import vrun``。
这是刻意选择的最小侵入方案：仓库里因此不必多出一个 ``pyproject.toml``。

同时提供几个共享 fixture。
"""

from __future__ import annotations

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
