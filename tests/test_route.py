"""test_route.py — 路线模型与弧长参数化的测试。

核心断言：
- 总长与手工累加的 haversine 完全一致（不引入第二套距离口径）；
- ``point_at`` 在端点、中点、越界处行为正确；
- ``scaled_to`` 精确命中目标长度，且**保持形状比例**。
"""

from __future__ import annotations

import pytest

from vrun.geo import haversine
from vrun.route import ROUTE_FORMAT_VERSION, Route

LAT0, LON0 = 30.5535, 103.9910


@pytest.fixture
def square() -> Route:
    d = 0.0045
    return Route([
        (LAT0, LON0),
        (LAT0 + d, LON0),
        (LAT0 + d, LON0 + d),
        (LAT0, LON0 + d),
        (LAT0, LON0),
    ])


# ══════════════════════════════════════════════════════════════
#  构造与长度
# ══════════════════════════════════════════════════════════════

class TestConstruction:
    def test_length_matches_manual_haversine(self) -> None:
        pts = [(LAT0, LON0), (LAT0 + 0.01, LON0), (LAT0 + 0.01, LON0 + 0.01)]
        r = Route(pts)
        manual = sum(
            haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            for i in range(1, len(pts))
        )
        assert r.length() == pytest.approx(manual, rel=1e-12)

    def test_rejects_too_few_vertices(self) -> None:
        with pytest.raises(ValueError, match="至少需要 2 个顶点"):
            Route([(LAT0, LON0)])

    def test_rejects_zero_length(self) -> None:
        with pytest.raises(ValueError, match="总长为 0"):
            Route([(LAT0, LON0), (LAT0, LON0), (LAT0, LON0)])

    def test_len_and_repr(self, square: Route) -> None:
        assert len(square) == 5
        assert "Route" in repr(square)


# ══════════════════════════════════════════════════════════════
#  point_at
# ══════════════════════════════════════════════════════════════

class TestPointAt:
    def test_start_and_end(self, square: Route) -> None:
        lat, lon, _ = square.point_at(0.0)
        assert (lat, lon) == pytest.approx(square.points[0])

        lat, lon, _ = square.point_at(square.length())
        assert (lat, lon) == pytest.approx(square.points[-1])

    def test_clamped_outside_range(self, square: Route) -> None:
        assert square.point_at(-1000.0)[:2] == pytest.approx(square.points[0])
        assert square.point_at(square.length() + 1000.0)[:2] == pytest.approx(square.points[-1])

    def test_midpoint_of_straight_line(self) -> None:
        r = Route([(LAT0, LON0), (LAT0 + 0.01, LON0)])
        lat, lon, _ = r.point_at(r.length() / 2.0)
        assert lat == pytest.approx(LAT0 + 0.005, rel=1e-9)
        assert lon == pytest.approx(LON0, abs=1e-12)

    def test_bearing_follows_segment(self) -> None:
        r = Route([(LAT0, LON0), (LAT0, LON0 + 0.01)])   # 正东
        _, _, b = r.point_at(r.length() / 2.0)
        assert b == pytest.approx(90.0, abs=0.5)

    def test_walk_is_monotonic(self, square: Route) -> None:
        """沿 s 均匀采样，相邻两点间距离应为正且近似均匀。"""
        total = square.length()
        pts = [square.point_at(total * i / 200.0)[:2] for i in range(201)]
        steps = [
            haversine(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
            for i in range(1, len(pts))
        ]
        assert all(d > 0.0 for d in steps)
        assert sum(steps) == pytest.approx(total, rel=0.01)

    def test_vertex_hit_exactly(self, square: Route) -> None:
        """在第 2 个顶点处的弧长上取点，应恰好命中该顶点。"""
        d = haversine(*square.points[0], *square.points[1])
        lat, lon, _ = square.point_at(d)
        assert (lat, lon) == pytest.approx(square.points[1], rel=1e-9)


# ══════════════════════════════════════════════════════════════
#  scaled_to
# ══════════════════════════════════════════════════════════════

class TestScaling:
    @pytest.mark.parametrize("target", [1000.0, 3000.0, 5000.0, 15000.0])
    def test_hits_target_length(self, square: Route, target: float) -> None:
        assert square.scaled_to(target).length() == pytest.approx(target, rel=1e-9)

    def test_preserves_shape_ratios(self, square: Route) -> None:
        """等比缩放：各线段占总长的比例必须不变。"""
        def ratios(r: Route) -> list[float]:
            segs = [
                haversine(*r.points[i - 1], *r.points[i])
                for i in range(1, len(r.points))
            ]
            total = sum(segs)
            return [s / total for s in segs]

        scaled = square.scaled_to(3000.0)
        # 容差 1e-4 而非 1e-6：haversine 距离对「经纬度线性缩放」**不是严格线性**
        # （大圆 vs 平面），所以各段占比会有 1e-5 量级的残差。缩放本身是把
        # 总长迭代到 1e-9 精度的，这里测的是「形状比例」而非「总长」。
        assert ratios(scaled) == pytest.approx(ratios(square), rel=1e-4)

    def test_does_not_mutate_original(self, square: Route) -> None:
        before = square.length()
        square.scaled_to(9999.0)
        assert square.length() == pytest.approx(before, rel=1e-12)

    def test_rejects_non_positive(self, square: Route) -> None:
        with pytest.raises(ValueError):
            square.scaled_to(0.0)
        with pytest.raises(ValueError):
            square.scaled_to(-100.0)


# ══════════════════════════════════════════════════════════════
#  JSON 往返
# ══════════════════════════════════════════════════════════════

class TestJson:
    def test_roundtrip(self, square: Route, tmp_path) -> None:
        path = tmp_path / "route.json"
        square.to_json(str(path))
        back = Route.from_json(str(path))
        assert back.points == pytest.approx(square.points)
        assert back.length() == pytest.approx(square.length(), rel=1e-12)

    def test_writes_version(self, square: Route, tmp_path) -> None:
        import json
        path = tmp_path / "route.json"
        square.to_json(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == ROUTE_FORMAT_VERSION
        assert len(data["points"]) == 5

    def test_rejects_unknown_version(self, tmp_path) -> None:
        import json
        path = tmp_path / "route.json"
        path.write_text(
            json.dumps({"version": 99, "points": [[30.0, 104.0], [30.1, 104.1]]}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="不支持的路线文件版本"):
            Route.from_json(str(path))

    def test_rejects_malformed(self, tmp_path) -> None:
        path = tmp_path / "route.json"
        path.write_text('{"nope": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="格式不正确"):
            Route.from_json(str(path))
