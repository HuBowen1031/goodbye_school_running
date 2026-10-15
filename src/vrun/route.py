"""route.py — 路线模型与弧长参数化。

职责
    折线路线模型；累计弧长表；``point_at(s)`` 按弧长取点；按目标距离等比缩放；
    JSON 读写。

为什么存在
    引擎只推进一个标量 ``s``（弧长），几何细节全部封在这里。
    这是「速度 = 推进弧长的速率」这一核心设计（works.md §2.2）的落地处 ——
    因为 DVT 的 ``set()`` 没有速度语义，速度只能靠「移动得多快」制造。

依赖
    :mod:`vrun.geo`。
"""

from __future__ import annotations

import bisect
import json
import math
from typing import Iterable, Sequence

from .geo import ENUProjector, bearing, haversine

# 路线文件格式版本。带版本号是为了将来改格式时能识别旧文件（GUI 会写、侧车会读）。
ROUTE_FORMAT_VERSION = 1


class Route:
    """由折线顶点构成的路线，内部维护累计弧长表。

    ``points`` 是 ``[(lat, lon), ...]``，至少 2 个顶点。
    """

    def __init__(self, points: Iterable[Sequence[float]]) -> None:
        pts = [(float(p[0]), float(p[1])) for p in points]
        if len(pts) < 2:
            raise ValueError(f"路线至少需要 2 个顶点，收到 {len(pts)} 个")
        self.points: list[tuple[float, float]] = pts

        # 累计弧长表：_cum[i] = 从起点走到第 i 个顶点的距离。
        # 长度 len(points)，_cum[-1] 即总长。
        cum = [0.0]
        for i in range(1, len(pts)):
            d = haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            cum.append(cum[-1] + d)
        self._cum: list[float] = cum

        if cum[-1] <= 0.0:
            raise ValueError("路线总长为 0（所有顶点重合），无法作为赛道")

    # ── 基本量 ────────────────────────────────────────────────

    def length(self) -> float:
        """总长度，单位米。"""
        return self._cum[-1]

    def __len__(self) -> int:
        return len(self.points)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"Route(vertices={len(self.points)}, length={self.length():.1f}m)"

    # ── ★ 核心接口 ────────────────────────────────────────────

    def point_at(self, s: float) -> tuple[float, float, float]:
        """弧长 ``s``（米）→ ``(lat, lon, bearing)``。

        这是**引擎唯一接触几何的入口**。实现：``bisect`` 二分定位线段 +
        线段内线性插值，复杂度 O(log n)。

        ``s`` 超出 ``[0, length]`` 时**钳制到端点**（不回退、不报错）——
        引擎在终点附近可能因一步积分略微越过总长，钳制是预期行为。
        """
        total = self._cum[-1]
        s = 0.0 if s < 0.0 else (total if s > total else s)

        # bisect_right 返回第一个 > s 的位置；减 1 得到所在线段的左端点下标。
        i = bisect.bisect_right(self._cum, s) - 1
        if i >= len(self.points) - 1:
            i = len(self.points) - 2  # s == total 时落在最后一段

        seg_start = self._cum[i]
        seg_len = self._cum[i + 1] - seg_start
        t = 0.0 if seg_len <= 0.0 else (s - seg_start) / seg_len

        lat1, lon1 = self.points[i]
        lat2, lon2 = self.points[i + 1]
        lat = lat1 + (lat2 - lat1) * t
        lon = lon1 + (lon2 - lon1) * t
        return (lat, lon, bearing(lat1, lon1, lat2, lon2))

    # ── 缩放 ──────────────────────────────────────────────────

    def scaled_to(self, target_m: float) -> "Route":
        """绕质心**等比**缩放，使总长等于 ``target_m``。

        用途：地图上随手画的路线只提供「赛道形状」，长度由面板的目标距离决定。

        注意：缩放会同时改变路线的形状尺度，这是可接受的 —— 不要求与真实
        道路严格对齐。另外，经纬度→米→经纬度不是严格线性（大圆 vs 平面），
        所以这里做几次迭代修正，把残差压到 1e-9 以下。
        """
        if target_m <= 0.0:
            raise ValueError(f"目标长度必须为正，收到 {target_m}")

        n = len(self.points)
        clat = sum(p[0] for p in self.points) / n
        clon = sum(p[1] for p in self.points) / n

        factor = 1.0
        route: Route = self
        for _ in range(8):
            pts = [
                (clat + (lat - clat) * factor, clon + (lon - clon) * factor)
                for lat, lon in self.points
            ]
            route = Route(pts)
            ratio = target_m / route.length()
            if abs(ratio - 1.0) < 1e-12:
                break
            factor *= ratio
        return route

    # ── 持久化 ────────────────────────────────────────────────

    def to_json(self, path: str) -> None:
        """写出路线 JSON（``{"version": 1, "points": [[lat, lon], ...]}``）。"""
        payload = {
            "version": ROUTE_FORMAT_VERSION,
            "points": [[lat, lon] for lat, lon in self.points],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, path: str) -> "Route":
        """读入路线 JSON。未知版本会明确报错，而不是猜着解析。"""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "points" not in data:
            raise ValueError(f"路线文件格式不正确：{path}")
        version = data.get("version", ROUTE_FORMAT_VERSION)
        if version != ROUTE_FORMAT_VERSION:
            raise ValueError(
                f"不支持的路线文件版本 {version}（本程序支持 {ROUTE_FORMAT_VERSION}）：{path}"
            )
        return cls(data["points"])


# ── 圆 ──────────────────────────────────────────────────────────────


def circle_points(
    center: Sequence[float],
    diameter_m: float,
    vertices: int = 360,
) -> list[tuple[float, float]]:
    """圆周上 ``vertices`` 个等分点，**不含**与首点重合的收尾点。

    为什么绕到局部平面再反投影
        经纬度网格不是正交的 —— 直接对经度做线性插值，圆会随纬度**横向拉伸**。
        15 m 半径下这个误差不到 1 mm，但它是**结构性**错误：
        写死在这里（而不是留给调用方各自处理）才能保证所有「圆」都对。
        走 :class:`~vrun.geo.ENUProjector` 才是几何正确的做法。

    ★ 为什么这保证了「不会跑出半径」
        顶点**严格**落在半径 ``diameter_m/2`` 的圆上，相邻顶点连的是**弦**；
        弦恒在圆内，而引擎的插值是线性的、永远落在弦上。
        所以无噪声时任何注入点的离圆心距离 **≤ 半径**，一个字节都不会溢出。
        这是「免得超出电子围栏」的几何保证，
        由 ``test圆折线上的任何一点都不会跑出半径`` 守住。
    """
    if diameter_m <= 0.0:
        raise ValueError(f"直径必须为正，收到 {diameter_m}")
    if vertices < 3:
        raise ValueError(f"圆至少需要 3 个顶点，收到 {vertices}")

    radius = float(diameter_m) / 2.0
    proj = ENUProjector(center[0], center[1])
    step = 2.0 * math.pi / vertices
    return [
        proj.to_latlon(radius * math.cos(i * step), radius * math.sin(i * step))
        for i in range(vertices)
    ]


def circle_route(
    center: Sequence[float],
    diameter_m: float,
    laps: int = 1,
    vertices: int = 360,
) -> Route:
    """同一个圆重复 ``laps`` 圈 —— **几何完全重合**，不是画 ``laps`` 个不同的圈。

    用途：极小场地跑长距离。§6 M4 要 3 km，但场地只有 30 m 直径；
    重复同一个圆让里程累加，而手机自始至终只在一个 30 m 的范围内打转，
    电子围栏一个字节都不会被突破。

    ⚠️ ``laps`` 圈**必须**是整数且完全重复
        若改成一圈套一圈的螺旋（每圈偏移几厘米），几何就不再重合，
        「重跑同一个圈」这个前提随之失效 —— 围栏半径要按外沿算，不是按圆心算。
    """
    if laps < 1:
        raise ValueError(f"圈数至少为 1，收到 {laps}")
    lap = circle_points(center, diameter_m, vertices)
    # 每圈贡献 vertices 段，最后补一个收尾顶点把最后一圈闭合。
    return Route(lap * laps + [lap[0]])


def circle_route_to(
    center: Sequence[float],
    diameter_m: float,
    distance_m: float,
    vertices: int = 360,
) -> tuple[Route, int]:
    """重复同一个圆，直到总长 ≥ ``distance_m``。返回 ``(route, laps)``。

    ★ 圈数用**实测的一圈折线长**算，不是 π·d
        折线是弦，弦 < 弧，π·d 会系统性偏大 0.0013%。
        单看这个误差无所谓，但圈数要**向上取整** ——
        用偏大的估计可能少跑一整圈（本例 94 m），
        而「刚好 3 km」压线时，那一整圈就是生死线。
        所以宁可低估圈数、多跑一点，也不能少跑。

        实测取整还有个副作用：``laps`` 是整数，圈与圈**几何完全重合**，
        手机自始至终只在半径 ``diameter_m/2`` 内打转。
    """
    if distance_m <= 0.0:
        raise ValueError(f"目标距离必须为正，收到 {distance_m}")
    one = circle_route(center, diameter_m, laps=1, vertices=vertices)
    lap_m = one.length()
    laps = max(1, math.ceil(distance_m / lap_m))
    return circle_route(center, diameter_m, laps=laps, vertices=vertices), laps
