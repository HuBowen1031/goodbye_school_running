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
from typing import Iterable, Sequence

from .geo import bearing, haversine

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
