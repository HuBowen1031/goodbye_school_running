"""test_circle.py —— 圆形路线的几何保证

为什么这些测试值钱
------------------
用户的要求是「绕着一个 30 m 直径的操场跑个圈，**免得超出电子围栏**」。
这句话背后是一个**几何断言**：手机在任何一个时刻的位置，都必须落在
半径 15 m 的圆内。

这个断言很容易在重构中被悄悄破坏 ——
比如有人把顶点改成用经纬度直接做线性插值（圆会横向拉伸），
或者把「重复 N 圈」改成「画 N 个逐渐偏移的圈」（重跑同一个圈的前提失效）。
两者都不会报错，只会让**电子围栏被悄悄突破**，而手机毫无察觉。

所以这里守的不是「半径对不对」，而是**「最坏情况下也跑不出去」**。
"""

from __future__ import annotations

import math

import pytest

from vrun.geo import ENUProjector
from vrun.route import circle_points, circle_route, circle_route_to

CENTER = (30.628212, 104.084842)   # 四川大学望江校区体育运动中心（用户给的坐标）
DIAMETER = 30.0


# ── 几何保证 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("vertices", [3, 8, 60, 360])
def test圆折线上的任何一点都不会跑出半径(vertices: int) -> None:
    """★ 核心保证：引擎在弦上线性插值，插值结果必须仍在圆内。

    顶点严格在圆上，相邻顶点连的是**弦**，而弦恒在圆内 ——
    所以「跑出去」在几何上不可能发生。这条测试是这个断言的证明。
    """
    route = circle_route(CENTER, DIAMETER, laps=1, vertices=vertices)
    proj = ENUProjector(*CENTER)
    radius = DIAMETER / 2.0
    # 每段取 37 个点 —— 覆盖端点与所有内部位置
    for i in range(0, 37 * (len(route) - 1) + 1):
        s = i / 37.0
        lat, lon, _ = route.point_at(min(s, route.length()))
        e, n = proj.to_enu(lat, lon)
        assert math.hypot(e, n) <= radius + 1e-6, (
            f"s={s:.3f} m 处离圆心 {math.hypot(e, n):.6f} m，超出半径 {radius} m"
        )


def test顶点确实落在圆上() -> None:
    """反向保证：折线不是「保守地画小了」，顶点确实在半径上。

    只守上一条的话，一个半径画成 5 m 的圆也能通过 —— 那会让 App 数出的
    里程严重偏短，而所有几何测试都是绿的。
    """
    radius = DIAMETER / 2.0
    proj = ENUProjector(*CENTER)
    for lat, lon in circle_points(CENTER, DIAMETER, vertices=36):
        e, n = proj.to_enu(lat, lon)
        assert math.hypot(e, n) == pytest.approx(radius, abs=1e-3)


def test圈与圈几何完全重合() -> None:
    """「重复同一个圈」是电子围栏不破的前提，不能变成「画一圈套一圈」。"""
    route = circle_route(CENTER, DIAMETER, laps=4, vertices=36)
    lap = 36
    for k in range(4):
        for j in range(lap):
            assert route.points[k * lap + j] == pytest.approx(route.points[j]), (
                f"第 {k + 1} 圈的第 {j} 个顶点与第 1 圈不一致 —— "
                "圈与圈必须完全重合，否则围栏要按外沿算"
            )


# ── 长度与圈数 ──────────────────────────────────────────────────────


def test一圈长度接近圆周长() -> None:
    """折线是弦，弦 < 弧，所以应当**略短**于 π·d，但不能短太多。"""
    route = circle_route(CENTER, DIAMETER, laps=1, vertices=360)
    ideal = math.pi * DIAMETER
    assert route.length() == pytest.approx(ideal, rel=1e-3)
    assert route.length() < ideal  # 弦确实短于弧


def test总长随圈数线性累加() -> None:
    one = circle_route(CENTER, DIAMETER, laps=1).length()
    for laps in (2, 7, 34):
        assert circle_route(CENTER, DIAMETER, laps=laps).length() == pytest.approx(
            one * laps, rel=1e-12
        )


def test重复到目标距离时圈数向上取整且够长() -> None:
    """★ 本函数的契约是「**弧长** ≥ 目标」，不是「App 数出的距离 ≥ 目标」。

    两者在实测中相差一整圈（3000 m → 32 圈 vs 34 圈）：
    App 的距离要过一遍滤波，30 m 圈过 5 s 低通会衰减约 3.6%。
    需要那个口径的，必须用 :func:`vrun.preview.plan_laps`。

    这里守的是「圈数向上取整、绝不少跑」——少一整圈就是 94 m。
    """
    route, laps = circle_route_to(CENTER, DIAMETER, 3000.0)
    assert laps == 32
    assert route.length() >= 3000.0
    # 再要 1 cm 也仍然向上取整，而不是复用上一圈
    route2, laps2 = circle_route_to(CENTER, DIAMETER, route.length() + 0.01)
    assert laps2 == laps + 1


def test目标距离恰为整圈时不多跑() -> None:
    """``laps`` 必须按**实测折线长**取整，不能用 π·d ——

    用 π·d 算圈数会**偏大**（π·d 比弦长长 0.0013%），
    在「刚好够」的情况下就会多跑一整圈，19 分钟变 20 分钟。
    """
    one = circle_route(CENTER, DIAMETER, laps=1).length()
    route, laps = circle_route_to(CENTER, DIAMETER, one * 3)
    assert laps == 3
    assert route.length() == pytest.approx(one * 3, rel=1e-12)


def test目标距离小于一圈时至少跑一圈() -> None:
    _, laps = circle_route_to(CENTER, DIAMETER, 10.0)
    assert laps == 1


# ── 参数校验 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0.0, -1.0, -30.0])
def test非法直径被拒(bad: float) -> None:
    with pytest.raises(ValueError, match="直径必须为正"):
        circle_route(CENTER, bad)


@pytest.mark.parametrize("bad", [0, 1, 2, -5])
def test顶点太少被拒(bad: int) -> None:
    with pytest.raises(ValueError, match="至少需要 3 个顶点"):
        circle_route(CENTER, DIAMETER, vertices=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test圈数为零或负数被拒(bad: int) -> None:
    with pytest.raises(ValueError, match="圈数至少为 1"):
        circle_route(CENTER, DIAMETER, laps=bad)


def test目标距离非正被拒() -> None:
    with pytest.raises(ValueError, match="目标距离必须为正"):
        circle_route_to(CENTER, DIAMETER, 0.0)


def test零半径圆会构造失败() -> None:
    """所有顶点重合 → 总长 0 → :class:`Route` 必须报错，不能安静地返回。"""
    with pytest.raises(ValueError, match="总长为 0"):
        circle_route(CENTER, 1e-12)
