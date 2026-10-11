"""test_geo.py — 坐标数学底座的测试。

三条核心断言：
1. ``to_enu`` / ``to_latlon`` 严格互逆（往返误差 < 0.1 m）—— 头号断言；
2. 经度方向的尺度因子确实乘了 ``cos(lat)``（漏乘会让东向高估 16%）；
3. ``haversine`` 与投影在米制上一致（避免两套距离口径打架）。
"""

from __future__ import annotations

import math

import pytest

from vrun.geo import EARTH_RADIUS_M, ENUProjector, bearing, haversine

LAT0, LON0 = 30.5535, 103.9910


@pytest.fixture
def proj() -> ENUProjector:
    return ENUProjector(LAT0, LON0)


# ══════════════════════════════════════════════════════════════
#  投影
# ══════════════════════════════════════════════════════════════

class TestProjection:
    def test_origin_maps_to_zero(self, proj: ENUProjector) -> None:
        e, n = proj.to_enu(LAT0, LON0)
        assert abs(e) < 1e-9
        assert abs(n) < 1e-9

    @pytest.mark.parametrize("dlat,dlon", [
        (0.001, 0.0),
        (0.0, 0.001),
        (0.01, 0.01),
        (-0.02, 0.03),
        (0.027, -0.031),
    ])
    def test_roundtrip_under_10cm(self, proj: ENUProjector, dlat: float, dlon: float) -> None:
        lat_in, lon_in = LAT0 + dlat, LON0 + dlon
        e, n = proj.to_enu(lat_in, lon_in)
        lat_out, lon_out = proj.to_latlon(e, n)
        err_m = haversine(lat_in, lon_in, lat_out, lon_out)
        assert err_m < 0.1, f"往返误差 {err_m:.4f} m 超过 0.1 m"

    def test_east_factor_includes_cosine(self, proj: ENUProjector) -> None:
        """经度方向必须乘 cos(origin_lat) —— 等距圆柱唯一容易写错的地方。"""
        dlon = 0.001
        e, _ = proj.to_enu(LAT0, LON0 + dlon)
        per_deg = math.pi * EARTH_RADIUS_M / 180.0
        expected = per_deg * dlon * math.cos(math.radians(LAT0))
        # 容差 1e-9 而非 1e-12：两者的**乘法顺序**不同，浮点结果天然差最后几位。
        assert e == pytest.approx(expected, rel=1e-9)

        # 与「漏乘 cos」的朴素做法相差正好 cos(lat)
        naive = per_deg * dlon
        assert e / naive == pytest.approx(math.cos(math.radians(LAT0)), rel=1e-9)
        assert e / naive < 0.9   # 成都处约 0.861，差距相当可观

    def test_axis_orientation(self, proj: ENUProjector) -> None:
        e, n = proj.to_enu(LAT0, LON0 + 0.001)
        assert e > 0.0 and abs(n) < 1e-6            # 东为正

        e, n = proj.to_enu(LAT0 + 0.001, LON0)
        assert n > 0.0 and abs(e) < 1e-6            # 北为正

    def test_scale_is_metric(self, proj: ENUProjector) -> None:
        """投影出的米制距离应与 haversine 一致（误差 < 0.2%）。"""
        lat, lon = proj.to_latlon(0.0, 500.0)       # 向北 500 m
        assert haversine(LAT0, LON0, lat, lon) == pytest.approx(500.0, rel=0.002)

        lat, lon = proj.to_latlon(500.0, 0.0)       # 向东 500 m
        assert haversine(LAT0, LON0, lat, lon) == pytest.approx(500.0, rel=0.002)


# ══════════════════════════════════════════════════════════════
#  haversine
# ══════════════════════════════════════════════════════════════

class TestHaversine:
    def test_zero_distance(self) -> None:
        assert haversine(LAT0, LON0, LAT0, LON0) == 0.0

    def test_symmetry(self) -> None:
        a = haversine(LAT0, LON0, LAT0 + 0.01, LON0 + 0.01)
        b = haversine(LAT0 + 0.01, LON0 + 0.01, LAT0, LON0)
        assert a == pytest.approx(b, rel=1e-12)

    def test_one_degree_latitude(self) -> None:
        """1° 纬度 = πR/180 米（与所用地球半径自洽）。"""
        d = haversine(30.0, 100.0, 31.0, 100.0)
        assert d == pytest.approx(math.pi * EARTH_RADIUS_M / 180.0, rel=1e-9)

    def test_numeric_stability_at_small_scale(self) -> None:
        """10 cm 尺度不能用 asin 实现 —— 那会丢精度甚至出 NaN。"""
        d = haversine(LAT0, LON0, LAT0, LON0 + 1e-6)
        assert d > 0.0
        assert d == pytest.approx(0.0955, abs=0.002)


# ══════════════════════════════════════════════════════════════
#  方位角
# ══════════════════════════════════════════════════════════════

class TestBearing:
    def test_cardinal_directions(self) -> None:
        assert bearing(LAT0, LON0, LAT0 + 0.01, LON0) == pytest.approx(0.0, abs=1e-6)
        assert bearing(LAT0, LON0, LAT0, LON0 + 0.01) == pytest.approx(90.0, abs=0.5)
        assert bearing(LAT0, LON0, LAT0 - 0.01, LON0) == pytest.approx(180.0, abs=1e-6)
        assert bearing(LAT0, LON0, LAT0, LON0 - 0.01) == pytest.approx(270.0, abs=0.5)

    @pytest.mark.parametrize("dlat,dlon", [
        (1.0, 1.0), (-1.0, 1.0), (1.0, -1.0), (-1.0, -1.0),
    ])
    def test_range_is_0_to_360(self, dlat: float, dlon: float) -> None:
        b = bearing(LAT0, LON0, LAT0 + dlat, LON0 + dlon)
        assert 0.0 <= b < 360.0
