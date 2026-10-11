"""geo.py — 坐标数学底座。

职责
    经纬度 ↔ 局部 ENU 米制平面的双向投影；haversine 距离；方位角。

为什么存在
    噪声是**米制**叠加的（±3 m），而 DVT 注入接口只吃**经纬度**。
    中间必须有且只有一次换算，否则误差会在两套单位之间反复污染。

设计取舍
    用**等距圆柱（equirectangular）近似**而非完整大地测量：纬度 30.6° 附近、
    跨度 < 5 km 时相对误差 < 0.1%，比 Vincenty 快两个数量级，且**可闭式反解**
    —— 这对 ``route.point_at()`` 的 O(log n) 实现很关键。

依赖
    仅标准库 ``math``。标量运算用标准库比 numpy 更快，numpy 在此无收益。
"""

from __future__ import annotations

import math

# IUGG 平均地球半径（米）。与 WGS84 赤道半径 6378137 相差 0.04%，
# 在江安校区尺度上远小于其他误差源；与 haversine 保持同一常数可避免
# 两套距离口径互相打架。
EARTH_RADIUS_M: float = 6_371_008.8

_DEG2RAD = math.pi / 180.0
_RAD2DEG = 180.0 / math.pi


class ENUProjector:
    """以 origin 为原点的局部等距圆柱投影（江安校区尺度误差 < 0.1%）。

    坐标约定：``e`` = 东向（米），``n`` = 北向（米）。
    投影中心可任选；``Route`` 用路线首点，引擎用同一点，保证全链路一致。
    """

    __slots__ = ("origin_lat", "origin_lon", "_m_per_deg_lat", "_m_per_deg_lon")

    def __init__(self, origin_lat: float, origin_lon: float) -> None:
        self.origin_lat = float(origin_lat)
        self.origin_lon = float(origin_lon)
        # 纬度方向：1° ≈ πR/180（与经度无关）
        self._m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
        # 经度方向：必须乘 cos(origin_lat)。
        # 这是等距圆柱唯一容易写错的地方 —— 成都（30.6°N）处该因子为 0.861，
        # 漏乘会让东向距离高估 16%，足以让整条路线长度失真。
        self._m_per_deg_lon = self._m_per_deg_lat * math.cos(self.origin_lat * _DEG2RAD)

    def to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        """经纬度 → (东, 北)，单位米。"""
        e = (lon - self.origin_lon) * self._m_per_deg_lon
        n = (lat - self.origin_lat) * self._m_per_deg_lat
        return (e, n)

    def to_latlon(self, e: float, n: float) -> tuple[float, float]:
        """(东, 北) 米 → 经纬度。必须是 :meth:`to_enu` 的严格逆运算。"""
        lat = self.origin_lat + n / self._m_per_deg_lat
        lon = self.origin_lon + e / self._m_per_deg_lon
        return (lat, lon)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离，单位米。

    用 ``atan2`` 而非 ``asin``：小距离（< 10 m，正是 GPS 噪声的尺度）下
    ``asin`` 的数值稳定性差一档，会在差分速度里注入假抖动。
    """
    p1 = lat1 * _DEG2RAD
    p2 = lat2 * _DEG2RAD
    dp = p2 - p1
    dl = (lon2 - lon1) * _DEG2RAD
    sin_dp = math.sin(dp * 0.5)
    sin_dl = math.sin(dl * 0.5)
    a = sin_dp * sin_dp + math.cos(p1) * math.cos(p2) * sin_dl * sin_dl
    # 数值兜底：浮点误差可能让 a 略微越过 1.0
    if a < 0.0:
        a = 0.0
    elif a > 1.0:
        a = 1.0
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """起点指向终点的方位角，单位度；正北为 0，顺时针为正，值域 [0, 360)。

    仅用于**可选的**调试输出：DVT 的 ``set()`` 不接收 course（works.md §1.2），
    所以方位角不参与位置注入，只帮人读日志。
    """
    p1 = lat1 * _DEG2RAD
    p2 = lat2 * _DEG2RAD
    dl = (lon2 - lon1) * _DEG2RAD
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.atan2(y, x) * _RAD2DEG % 360.0
