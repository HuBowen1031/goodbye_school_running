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


# ══════════════════════════════════════════════════════════════
# 坐标系：WGS-84 / GCJ-02 / BD-09
# ══════════════════════════════════════════════════════════════

# ★ 为什么这里必须有这一节
#
#   DVT 的 ``set()`` 吃的是 **WGS-84**（GPS 原始坐标）。
#   而国内地图 App（高德 / 腾讯 / 微信 / 百度）给的**不是** WGS-84：
#
#   · 高德、腾讯、微信  → **GCJ-02**（国测局「火星坐标」）
#   · 百度              → **BD-09**（在 GCJ-02 基础上再加密一次）
#
#   成都一带的实测偏移：
#   · WGS-84 → GCJ-02  约 **366 m**
#   · WGS-84 → BD-09   约 **2084 m**
#
#   也就是说，把手机地图上抄来的坐标**直接**当 WGS-84 注入，
#   落点会偏出去几百米到两公里 —— 而且**一切看起来都正常**：
#   不报错、不崩溃，只是位置不对。
#
#   ⚠️ 苹果地图（中国区）与 MapKit 的底图来自国内供应商，
#      但 MapKit 内部对瓦片做了**纠偏**，
#      ``MKMapView.convert(_:toCoordinateFrom:)`` 返回的仍是 **WGS-84**。
#      所以**在 VRun 自己的地图上选点取坐标是安全的**，
#      从高德/百度抄则必须先换算。

#: 克拉索夫斯基椭球（GCJ-02/BD-09 用的基准）。
_GCJ_A: float = 6378245.0
#: 第一偏心率平方。
_GCJ_EE: float = 0.00669342162296594323

#: 合法的坐标系名。CLI 与 GUI 都用这几个英文键。
COORD_SYSTEMS: tuple[str, ...] = ("wgs84", "gcj02", "bd09")


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _gcj_offset(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02 的偏移量（度）。国测局的加密算法。

    ⚠️ 纬向的分母是 ``magic * sqrt_magic``，**不是** ``sqrt_magic ** 2``
       前者是 ``(1 - ee·sin²)³ᐟ²``，后者是 ``(1 - ee·sin²)``。
       两者差约 0.08% —— 听起来微不足道，但在成都一带是 **12 cm** 的纬度偏差。
       这个错误是被「独立重写一遍标准算法再逐点比对」抓到的：
       任何**只和自己比**的测试都发现不了它。
    """
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat * _DEG2RAD
    magic = math.sin(rad_lat)
    magic = 1.0 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_GCJ_A * (1.0 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_GCJ_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return d_lat, d_lon


def _out_of_china(lon: float, lat: float) -> bool:
    """是否在国测局加密范围**之外**。

    ★ 标准算法的这一步**不能省**
        国测局的偏移只对中国大陆境内生效。标准实现里都带这个判断，
        省略它的话，境外坐标也会被平移 —— 那是**错的**，
        而且错得很安静（境外用本工具的场景少，但用错时无从察觉）。

        边界取自标准实现：经度 [72.004, 137.8347]，纬度 [0.8293, 55.8271]。
    """
    if lon < 72.004 or lon > 137.8347:
        return True
    if lat < 0.8293 or lat > 55.8271:
        return True
    return False


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（高德 / 腾讯 / 微信）。"""
    if _out_of_china(lon, lat):
        return lat, lon
    d_lat, d_lon = _gcj_offset(lat, lon)
    return lat + d_lat, lon + d_lon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """GCJ-02 → WGS-84。

    用**不动点迭代**而不是「正算一次再减一次」——
    后者在几百米量级上够用，但误差会随位置变化，
    而这里正是「差几百米」的场合，不该再叠一个不确定量。
    迭代 6 次收敛到 1e-9 度（约 0.1 mm）。
    """
    w_lat, w_lon = lat, lon
    for _ in range(6):
        g_lat, g_lon = wgs84_to_gcj02(w_lat, w_lon)
        w_lat += lat - g_lat
        w_lon += lon - g_lon
    return w_lat, w_lon


def wgs84_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    """WGS-84 → BD-09（百度）。"""
    gcj_lat, gcj_lon = wgs84_to_gcj02(lat, lon)
    z = math.sqrt(gcj_lat * gcj_lat + gcj_lon * gcj_lon) + 0.00002 * math.sin(
        gcj_lon * math.pi * 3000.0 / 180.0)
    theta = math.atan2(gcj_lat, gcj_lon) + 0.000003 * math.cos(
        gcj_lon * math.pi * 3000.0 / 180.0)
    sqrt_term = math.sqrt(1.0 + _GCJ_EE * math.sin(theta) ** 2)
    radius = z / sqrt_term + 0.000002 * math.sin(theta * math.pi * 3000.0 / 180.0)
    t = z / sqrt_term + 0.000002 * math.cos(theta * math.pi * 3000.0 / 180.0)
    return t * math.sin(theta) + 0.0065, t * math.cos(theta) + 0.006


def bd09_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """BD-09 → WGS-84。同样用不动点迭代。"""
    w_lat, w_lon = lat, lon
    for _ in range(6):
        b_lat, b_lon = wgs84_to_bd09(w_lat, w_lon)
        w_lat += lat - b_lat
        w_lon += lon - b_lon
    return w_lat, w_lon


def to_wgs84(lat: float, lon: float, system: str = "wgs84") -> tuple[float, float]:
    """任意合法坐标系 → WGS-84。DVT 注入只认这个。"""
    if system == "wgs84":
        return lat, lon
    if system == "gcj02":
        return gcj02_to_wgs84(lat, lon)
    if system == "bd09":
        return bd09_to_wgs84(lat, lon)
    raise ValueError(
        f"未知坐标系 {system!r}，可选：{list(COORD_SYSTEMS)}"
    )


def from_wgs84(lat: float, lon: float, system: str = "wgs84") -> tuple[float, float]:
    """WGS-84 → 任意合法坐标系。"""
    if system == "wgs84":
        return lat, lon
    if system == "gcj02":
        return wgs84_to_gcj02(lat, lon)
    if system == "bd09":
        return wgs84_to_bd09(lat, lon)
    raise ValueError(
        f"未知坐标系 {system!r}，可选：{list(COORD_SYSTEMS)}"
    )
