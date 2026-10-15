"""test_preview.py —— 离线预览的判定逻辑

**不需要 iPhone。** 它跑的是假时钟，18 分钟的轨迹 0.2 秒算完。

为什么这些测试值钱
------------------
预览是「占手机 18 分钟之前」唯一的决策依据。它算错了，用户就会
用错误的参数占着手机跑 19 分钟，然后拿到一份没法用的结果。

最容易算错的三件事，这里各有测试守着：

1. **App 的距离 ≠ 我们的弧长**
   噪声让 App 多算，平滑让 App 少算，两个方向相反、量级都不可忽略。
2. **余量的分母**
   第一版用「边界到当前值的距离」作分母，于是余量恒等于 1.0，
   「压线」与「稳过」被判成同一件事 —— 见 ``test余量用边界自身作分母``。
3. **围栏判据要用真圆心**
   引擎内部的投影中心是**路线首点**（在圆的东侧、离圆心 15 m）。
   拿它当参考系会让整个围栏判定偏移一整个半径。
"""

from __future__ import annotations

import math

import pytest

from vrun.preview import (
    DEFAULT_SMOOTH_S,
    R_MAX_SPEED_KMH,
    R_MIN_DISTANCE_M,
    R_MIN_SPEED_KMH,
    _boxcar,
    SWEEP_WINDOWS,
    _boxcar_width,
    checks,
    plan_laps,
    preview,
    preview_lines,
    rule_table_lines,
)
from vrun.route import circle_route

CENTER = (30.628212, 104.084842)
DIAMETER = 30.0
LAPS = 34


@pytest.fixture(scope="module")
def pv():
    """一次算好、模块内共用 —— 单次预览约 0.2 s，没必要反复重算。"""
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    return preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS,
                   speed_kmh=10.0, seed=0, smooth_s=DEFAULT_SMOOTH_S,
                   sweep_windows=SWEEP_WINDOWS)


@pytest.fixture(scope="module")
def pv_clean():
    """关噪声的同一圈 —— 用来把「几何效应」与「噪声效应」分开。"""
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    return preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS,
                   speed_kmh=10.0, seed=0, noise_level="off")


# ── 余量：踩过的坑 ──────────────────────────────────────────────────


def test余量用边界自身作分母() -> None:
    """★ 回归测试：分母必须是边界自身的量级。

    第一版写成 ``gap / (value - lo)``，分子分母是同一个数 ⇒ margin ≡ 1.0。
    于是「3004.7 m 压在 3000 m 线上」被判成「余量 100%」，
    自动搜索直接返回一个骑在线上的方案，换个噪声种子就翻车。
    """
    p = type(pv_stub())(
        dist_raw_m=3004.7, dist_smooth_m=3004.7,
        v_min_raw_kmh=10.0, v_min_smooth_kmh=10.0,
        v_max_raw_kmh=10.0, v_max_smooth_kmh=10.0,
        duration_s=1000.0,
    )
    r1 = {c.rule: c for c in checks(p, "raw")}["R1"]
    # (3004.7 - 3000) / 3000 = 0.00157
    assert r1.margin == pytest.approx(0.00157, abs=1e-4)


def test余量越界时为负() -> None:
    p = type(pv_stub())(dist_raw_m=2500.0, dist_smooth_m=2500.0)
    r1 = {c.rule: c for c in checks(p, "raw")}["R1"]
    assert not r1.ok
    assert r1.margin == pytest.approx((2500.0 - R_MIN_DISTANCE_M) / R_MIN_DISTANCE_M)


def pv_stub():
    """一个字段齐全的 PreviewResult 壳，用来单测判定分支。"""
    from vrun.preview import PreviewResult
    return PreviewResult()


def test压线与稳过必须被判成不同() -> None:
    """余量的存在意义就是把这两者分开。"""
    tight = type(pv_stub())(dist_smooth_m=3001.0, dist_raw_m=3001.0)
    loose = type(pv_stub())(dist_smooth_m=3300.0, dist_raw_m=3300.0)
    mt = {c.rule: c for c in checks(tight, "smooth")}["R1"].margin
    ml = {c.rule: c for c in checks(loose, "smooth")}["R1"].margin
    assert mt < 0.01 < ml


# ── App 口径的距离 ──────────────────────────────────────────────────


def test逐点距离略大于弧长(pv) -> None:
    """噪声把相邻点拉开 ⇒ App 数得**比弧长多**。

    注意是「略」：AR(1) 的慢漂移 τ=300 s，一秒内几乎不动，
    所以逐点差分远小于「两次独立采样」的情形。
    """
    assert pv.dist_raw_m > pv.dist_engine_m
    assert pv.dist_raw_m < pv.dist_engine_m * 1.05


def test平滑距离小于弧长(pv) -> None:
    """★ 反直觉但已实测：滤波把 34 s 周期的小圈「缩小」了。

    30 m 圈过一个 5 s 的低通，幅度按 sinc 衰减约 3.6%。
    弧长刚过 3 km 时，App 只数得出 2910 m —— 直接判负。
    """
    assert pv.dist_smooth_m < pv.dist_engine_m
    assert pv.dist_smooth_m == pytest.approx(pv.dist_engine_m * 0.966, rel=0.02)


def test窗口越长衰减越狠(pv) -> None:
    """单圈周期只有 34 s，窗口一旦接近周期，衰减就失控。"""
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    dists = []
    for w in (1.0, 3.0, 5.0, 10.0, 20.0):
        p = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS,
                    speed_kmh=10.0, seed=0, smooth_s=w)
        dists.append(p.dist_smooth_m)
    assert dists == sorted(dists, reverse=True), f"应单调不增，实得 {dists}"


def test关噪声时逐点距离略小于弧长(pv_clean) -> None:
    """★ 即使一个噪声都不加，App 数出的距离也**比弧长短 0.14%**。

    原因是**弦比弧短**：引擎沿圆弧推进，我们算的是弧长，
    而 App 数的是相邻两个注入点的**直线距离**（弦）。
    10 km/h、15 m 半径、1 Hz 时每步跨 2.78 m，对应约 10.6°，
    弦/弧 = 2·sin(θ/2)/θ ≈ 0.99857。

    ⚠️ 这条与噪声无关，是**纯几何**的。把它误判成「噪声让距离变短」
       会把调参方向带偏 —— 加大噪声反而会让距离**变长**。
    """
    assert pv_clean.dist_raw_m < pv_clean.route_m
    assert pv_clean.dist_raw_m == pytest.approx(pv_clean.route_m * 0.99857, rel=1e-3)


def test噪声让逐点距离变长(pv, pv_clean) -> None:
    """两个方向相反的效应必须分开看，别混成一个「噪声影响距离」。"""
    assert pv.dist_raw_m > pv_clean.dist_raw_m


# ── 电子围栏 ────────────────────────────────────────────────────────


def test无噪声时最远距离恰好等于半径(pv_clean) -> None:
    assert pv_clean.max_radius_clean_m == pytest.approx(DIAMETER / 2.0, abs=1e-3)


def test含噪声时最远距离超出半径(pv) -> None:
    assert pv.max_radius_noisy_m > DIAMETER / 2.0
    assert pv.max_radius_noisy_m < DIAMETER  # 噪声是几米级，不是几十米


def test围栏判定未知时不给结论(pv) -> None:
    assert pv.radius_limit_m is None
    assert pv.within_fence() is None
    assert any("围栏半径未知" in ln for ln in preview_lines(pv))


def test围栏够大判在围栏内() -> None:
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    p = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS,
                speed_kmh=10.0, seed=0, radius_limit_m=50.0)
    assert p.within_fence() is True
    assert any("✅ 在围栏内" in ln for ln in preview_lines(p))


def test围栏太小判出界() -> None:
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    p = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS,
                speed_kmh=10.0, seed=0, radius_limit_m=16.0)
    assert p.within_fence() is False
    assert any("超出围栏" in ln for ln in preview_lines(p))


def test围栏参考系用真圆心而不是路线首点(pv_clean) -> None:
    """★ 回归：引擎内部的投影中心是**路线首点**，离圆心 15 m。

    拿它当围栏参考系会让每个点的半径系统性偏掉 15 m。
    ``max_radius_clean_m`` 恰好等于半径，就是「参考系没搞错」的证据 ——
    若参考系错了，这个值会变成 0 或 30。
    """
    assert pv_clean.max_radius_clean_m == pytest.approx(DIAMETER / 2.0, abs=1e-3)
    assert pv_clean.max_radius_clean_m not in (0.0, DIAMETER)


# ── 可复现性 ────────────────────────────────────────────────────────


def test同种子完全可复现() -> None:
    """预览要能当决策依据，就必须和真机跑出**同一条**轨迹。"""
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    a = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS, seed=7)
    b = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS, seed=7)
    assert (a.dist_raw_m, a.v_min_raw_kmh, a.max_radius_noisy_m) == (
        b.dist_raw_m, b.v_min_raw_kmh, b.max_radius_noisy_m
    )


def test不同种子结果不同() -> None:
    route = circle_route(CENTER, DIAMETER, laps=LAPS)
    a = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS, seed=1)
    b = preview(route, center=CENTER, diameter_m=DIAMETER, laps=LAPS, seed=2)
    assert a.max_radius_noisy_m != b.max_radius_noisy_m


# ── 滑动平均 ────────────────────────────────────────────────────────


def test箱式平均窗口强制为奇数() -> None:
    assert _boxcar_width(5.0, 1.0) == 5
    assert _boxcar_width(4.0, 1.0) == 5     # 4 → 5，宁可宽一点也要居中
    assert _boxcar_width(0.0, 1.0) == 1     # 0 → 1，不能是 0
    for w in (0.5, 1.0, 2.5, 3.7, 10.0):
        assert _boxcar_width(w, 1.0) % 2 == 1


def test箱式平均保留完整时间跨度() -> None:
    """★ 窗口两端要**截断**到有效范围，而不是补零。

    补零会在首尾造出「向原点收缩」的假位移，把开头的里程吞掉；
    截断则只把窗口缩短，首尾共 2(w-1) 个点受影响
    （本例 1164 点里占 8 个），可以忽略。

    注意实现是**截断**而非「复制边缘值」：
    i=0 的窗口是 [0,1,2]（3 个点，均值 1.0），
    若真复制边缘值会变成 [0,0,0,1,2]（均值 0.6）。
    两者都能用，但文档与代码必须说同一个 —— 早期版本这里写着「复制」、
    代码写着「截断」，读代码的人会照着「复制」去推算首尾速度。
    """
    pts = [(float(i), 0.0) for i in range(20)]
    out = _boxcar(pts, 5)
    assert len(out) == len(pts), "截断不能改变点数，否则时间跨度就丢了"
    assert out[0] == pytest.approx((1.0, 0.0))    # mean(0,1,2)
    assert out[-1] == pytest.approx((18.0, 0.0))  # mean(17,18,19)
    # 首尾不被拉向负方向（补零会那样）
    assert all(0.0 <= e for e, _ in out)


def test箱式平均窗口为一时原样返回() -> None:
    pts = [(1.0, 2.0), (3.0, 4.0)]
    assert _boxcar(pts, 1) == pts


# ── 自动圈数 ────────────────────────────────────────────────────────


def test自动圈数比按弧长取整多() -> None:
    """★ 这是本模块存在的核心理由。

    按弧长取整得到 32 圈（3015.9 m）—— 逐点口径数出 3041 m（过），
    但做 5 s 平滑的 App 只数出 **2910 m**（不过 3 km）。
    """
    from vrun.route import circle_route_to

    _, naive = circle_route_to(CENTER, DIAMETER, R_MIN_DISTANCE_M)
    planned = plan_laps(CENTER, DIAMETER, speed_kmh=10.0, seed=0)
    assert naive == 32
    assert planned.laps > naive


def test自动圈数的方案两种口径都过且有余量() -> None:
    planned = plan_laps(CENTER, DIAMETER, speed_kmh=10.0, seed=0)
    for model in ("raw", "smooth"):
        got = {c.rule: c for c in checks(planned, model)}
        assert got["R1"].ok, f"{model} 口径 R1 不通过：{got['R1'].text}"
        assert got["R4"].ok, f"{model} 口径 R4 不通过：{got['R4'].text}"
    r1 = {c.rule: c for c in checks(planned, "smooth")}["R1"]
    assert r1.margin >= 0.02


def test自动圈数对噪声种子稳健() -> None:
    """余量存在的意义：换个种子不能翻车。"""
    laps = plan_laps(CENTER, DIAMETER, speed_kmh=10.0, seed=0).laps
    route = circle_route(CENTER, DIAMETER, laps=laps)
    for seed in range(5):
        p = preview(route, center=CENTER, diameter_m=DIAMETER, laps=laps,
                    speed_kmh=10.0, seed=seed)
        assert p.dist_smooth_m >= R_MIN_DISTANCE_M, (
            f"seed={seed} 时平滑口径只数出 {p.dist_smooth_m:.1f} m"
        )


def test自动圈数不把R3纳入条件() -> None:
    """★ R3 的口径必须由用户看 App 界面来定，不能由工具替他猜。

    逐点最低速在「采样点越多越糟」—— 噪声差分能抵消前进量，
    采样越密越容易撞到接近 0 的瞬时速度。所以只要把 R3 写进搜索条件，
    目标就变成**无解**，搜索会一路撞到 ``max_laps`` 上限。

    证据就是圈数：带 R3 时返回 400（上限），不带时返回 34。
    """
    planned = plan_laps(CENTER, DIAMETER, speed_kmh=10.0, seed=0)
    assert planned.laps < 50, (
        f"返回了 {planned.laps} 圈 —— 搜索多半撞到了 max_laps 上限，"
        "说明 R3 被错误地当成了搜索条件"
    )
    r3 = {c.rule: c for c in checks(planned, "raw")}["R3"]
    # 明确记录当前事实：R3 是判负的，且**不是**搜索的目标
    assert not r3.ok
    assert r3.margin < 0


# ── 报告 ────────────────────────────────────────────────────────────


def test报告每条规则都出现两次(pv) -> None:
    """两种口径都要逐条落到报告里，漏一条就等于没判。"""
    text = "\n".join(preview_lines(pv))
    for key in ("R1", "R2", "R3", "R4", "R5"):
        assert text.count(key) >= 2, f"{key} 在报告里少于两处"
    assert "电子围栏" in text
    assert "平滑窗口敏感性" in text


def test对照表行列对齐(pv) -> None:
    lines = rule_table_lines(pv)
    assert len(lines) == 7   # 表头 + 分隔线 + 5 条规则
    widths = {len(ln) for ln in lines[:1] + lines[2:]}
    assert len(widths) == 1, f"列宽不一致：{widths}"


def test未达标时报告明说而不是静悄悄通过() -> None:
    """搜到上限仍不达标时，返回的最后一个结果必须**如实**显示不达标。"""
    # 直径 100 m → 一圈 314 m，搜到 6 圈也才 1885 m，够不到 3 km
    tiny = plan_laps(CENTER, 100.0, speed_kmh=10.0, seed=0, max_laps=6)
    assert tiny.laps == 6, "应当停在上限，而不是悄悄多跑几圈"
    r1 = {c.rule: c for c in checks(tiny, "raw")}["R1"]
    assert not r1.ok
    assert "✗" in "\n".join(rule_table_lines(tiny))


def test采样点不足时报错而不是返回零值(monkeypatch) -> None:
    """少于 2 个点算不出距离与速度，必须明确报错。

    这条守卫平时够不到（引擎跑到终点至少会发两个点），但**正因如此**
    更要测 —— 够不到的守卫一旦被重构破坏，报出来的是一堆零值，
    而零值在报告里看起来像是「跑得极慢」，不会引起任何怀疑。
    """
    from vrun import preview as pv_mod

    class _OneSample:
        def simulate_offline(self):
            from vrun.engine import Sample
            return [Sample(t=0.0, lat=CENTER[0], lon=CENTER[1],
                           s=0.0, v_target=10.0, v_actual=10.0)]

    monkeypatch.setattr(pv_mod, "TrackEngine", lambda *a, **k: _OneSample())
    route = circle_route(CENTER, DIAMETER, laps=1)
    with pytest.raises(ValueError, match="算不出"):
        pv_mod.preview(route, center=CENTER, diameter_m=DIAMETER)


def test未知口径名被拒(pv) -> None:
    with pytest.raises(ValueError, match="未知口径"):
        checks(pv, "kalman")


def test路线构造失败时不会被预览掩盖() -> None:
    """顶点重合的圆连路线都建不起来，报错必须来自 :class:`Route`。"""
    with pytest.raises(ValueError, match="总长为 0"):
        circle_route(CENTER, 1e-12)


# ── 常量 ────────────────────────────────────────────────────────────


def test门槛与works_md一致() -> None:
    """验收门槛写死在代码里，就必须与 works.md §6 M4 的表一致。"""
    assert R_MIN_DISTANCE_M == 3000.0
    assert R_MIN_SPEED_KMH == 6.0
    assert R_MAX_SPEED_KMH == 17.0
    assert math.isclose(R_MIN_DISTANCE_M, 3_000.0)
