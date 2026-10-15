"""preview.py —— 不连手机，先把校园 App 会看到什么算出来。

为什么要有这个文件
------------------

M4 要占用手机 18 分钟。跑之前有三件事**只能靠算**知道，跑完才知道就晚了：

1. **会不会跑出电子围栏**
   圆是 30 m 直径，但注入点带 ±3 m/轴 的 AR(1) 噪声 ——
   离圆心最远能到多少米，取决于具体随机流，**只能逐点算**。
   用户明确要求「圈小点，定位要准确，免得超出电子围栏」，
   那就必须拿一个**数**回答，而不是「应该没事」。

2. **App 会数出多少公里**
   App 数的是**相邻定位点之间的距离**，不是我们推进的弧长。
   噪声把相邻点拉开，App 数出来的**必然比弧长大** ——
   大多少，决定 R1「≥ 3 km」是压线还是稳过。

3. **App 会看到多快的瞬时速度**
   同理，逐点差分会被噪声放大。R4「≤ 17 km/h」判的就是它。

★ 为什么必须「两种口径」都算
    我们**不知道**校园 App 内部有没有做平滑滤波，也**不去分析**它。

    · 不滤波 ⇒ **逐点口径**（最坏情况，噪声全额计入）
    · 做 N 秒滑动平均 ⇒ **平滑口径**（多数 App 的实际做法）

    两种都算出来，跑完看 App 报的数**落在哪一边** ——
    顺带就把它的滤波口径反推出来了。这比任何黑盒分析都直接，
    而且完全合规：我们只看它自己显示的数字。

★ 为什么这份预览是可信的
    它跑的是**同一个** :class:`~vrun.engine.TrackEngine`、**同一份**
    :class:`~vrun.noise.GPSNoise`（人工种子，可复现），
    只是把时钟换成假时钟。真机跑出来的轨迹与它**逐点一致**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .engine import Sample, TrackEngine
from .estimate import closed_form_eta
from .geo import ENUProjector, haversine
from .noise import GPSNoise
from .profile import SpeedProfile
from .route import Route

#: 校园 App 的判定门槛（works.md §6 M4 验收表）。**只作对照，不猜它的实现。**
R_MIN_DISTANCE_M: float = 3_000.0      # R1 每次有效距离 ≥ 3 km
R_MAX_DISTANCE_M: float = 15_000.0     # R2 每次最大锻炼距离 ≤ 15 km
R_MIN_SPEED_KMH: float = 6.0           # R3 最低速度 ≥ 6 km/h
R_MAX_SPEED_KMH: float = 17.0          # R4 最高速度 ≤ 17 km/h
R_MAX_DURATION_S: float = 7_200.0      # R5 每次最长锻炼时间 ≤ 2 h

#: 「App 做了平滑」的假设窗口长度，秒。
DEFAULT_SMOOTH_S: float = 5.0


# ── 数据 ────────────────────────────────────────────────────────────


@dataclass
class PreviewResult:
    """一次离线预览的全部结论。字段刻意全是标量，可直接 JSON 化。"""

    # ── 配置 ──
    center_lat: float = 0.0
    center_lon: float = 0.0
    diameter_m: float = 0.0
    laps: int = 0
    vertices_per_lap: int = 0
    route_m: float = 0.0
    speed_kmh: float = 0.0
    rate_hz: float = 1.0
    noise_level: str = "realistic"
    seed: int = 0
    smooth_s: float = DEFAULT_SMOOTH_S

    # ── 结果 ──
    samples: int = 0
    duration_s: float = 0.0
    planned_eta_s: float = 0.0

    #: 引擎推进的弧长（= 路线长度）。这是「我们以为跑的距离」，
    #: **不是** App 会数的距离 —— 两者差得不少，别混。
    dist_engine_m: float = 0.0

    # ── App 口径 · 逐点（不滤波）──
    dist_raw_m: float = 0.0
    v_min_raw_kmh: float = 0.0
    v_max_raw_kmh: float = 0.0

    # ── App 口径 · 平滑（boxcar 窗口 smooth_s）──
    dist_smooth_m: float = 0.0
    v_min_smooth_kmh: float = 0.0
    v_max_smooth_kmh: float = 0.0

    # ── 电子围栏 ──
    #: 无噪声时离圆心的最远距离。理论值恰好 = 半径，偏差只可能来自折线插值。
    max_radius_clean_m: float = 0.0
    #: 含噪声时离圆心的最远距离 —— **这才是围栏判据**。
    max_radius_noisy_m: float = 0.0
    p95_radius_noisy_m: float = 0.0
    #: 用户告知的围栏半径（米）。``None`` = 未知，不做判定。
    radius_limit_m: Optional[float] = None

    #: 逐点原始速度低于 R_MIN_SPEED_KMH 的采样占比。
    #: R3 是「最低速度」，一个点判负就够；但占比能说明「压线」的成色。
    frac_below_min_raw: float = 0.0
    frac_below_min_smooth: float = 0.0

    #: 平滑窗口 → 该口径下的里程与速度。键是窗口秒数。
    #: 30 m 圈的周期只有 34 s，**窗口越长衰减越狠**（10 s 窗口掉 15%），
    #: 所以「App 到底滤多久」直接决定 R1 能不能过 —— 这张表就是给那个未知量的。
    sweep: dict = field(default_factory=dict)

    def within_fence(self) -> Optional[bool]:
        """含噪声时是否**确定**在围栏内。``None`` = 围栏半径未知。"""
        if self.radius_limit_m is None:
            return None
        return self.max_radius_noisy_m <= self.radius_limit_m

    def to_dict(self) -> dict:
        return {
            "center": [self.center_lat, self.center_lon],
            "diameter_m": self.diameter_m,
            "laps": self.laps,
            "vertices_per_lap": self.vertices_per_lap,
            "route_m": self.route_m,
            "speed_kmh": self.speed_kmh,
            "rate_hz": self.rate_hz,
            "noise_level": self.noise_level,
            "seed": self.seed,
            "smooth_s": self.smooth_s,
            "samples": self.samples,
            "duration_s": self.duration_s,
            "planned_eta_s": self.planned_eta_s,
            "dist_engine_m": self.dist_engine_m,
            "app_raw": {
                "dist_m": self.dist_raw_m,
                "v_min_kmh": self.v_min_raw_kmh,
                "v_max_kmh": self.v_max_raw_kmh,
                "frac_below_min": self.frac_below_min_raw,
            },
            "app_smooth": {
                "dist_m": self.dist_smooth_m,
                "v_min_kmh": self.v_min_smooth_kmh,
                "v_max_kmh": self.v_max_smooth_kmh,
                "frac_below_min": self.frac_below_min_smooth,
            },
            "fence": {
                "max_radius_clean_m": self.max_radius_clean_m,
                "max_radius_noisy_m": self.max_radius_noisy_m,
                "p95_radius_noisy_m": self.p95_radius_noisy_m,
                "limit_m": self.radius_limit_m,
                "within": self.within_fence(),
            },
            "sweep": {str(k): v for k, v in self.sweep.items()},
        }


@dataclass
class Check:
    """一条验收规则的判定结果。"""

    rule: str
    #: 规则的中文描述，如「每次有效距离 ≥ 3 km」。
    label: str
    #: 实际值，带单位，如 ``"3015.9 m"``。
    text: str
    ok: bool
    #: 压线程度：0 = 刚好压线，1 = 离边界还有一整个 ``span`` 的余量。
    #: ⚠️ 负数 = **越界**。
    margin: float = 0.0


# ── 计算 ────────────────────────────────────────────────────────────


def _boxcar(points: List[Tuple[float, float]], width: int) -> List[Tuple[float, float]]:
    """ENU 坐标上的居中滑动平均，两端**截断**到有效范围（不是补零）。

    为什么两端截断而不是补零
        补零会在首尾造出一段假的「向原点收缩」，把开头/结尾的里程吞掉。
        截断只把窗口缩短，首尾共 ``2*(width-1)`` 个点受影响
        （1164 点里占 8 个），可以忽略。

    ⚠️ 是「截断」**不是**「复制边缘值」
        ``i=0`` 的窗口是 ``[0,1,2]``（均值 1.0）；
        复制边缘值会变成 ``[0,0,0,1,2]``（均值 0.6）。
        两者都能用，但代码与文档必须说同一个 —— 早期版本这里写着「复制」、
        实现写着截断，读代码的人会照着「复制」去推算首尾速度。

    为什么用前缀和而不是双重循环
        O(n) 对 O(n·w)。1164 个点时两者都快，但前缀和没有性能悬崖 ——
        万一有人调到 10 Hz / 30 s 窗口，双重循环会变成 3600 万次。
    """
    n = len(points)
    if width <= 1 or n < 2:
        return list(points)

    half = width // 2
    pre_e = [0.0] * (n + 1)
    pre_n = [0.0] * (n + 1)
    for i, (e, n_) in enumerate(points):
        pre_e[i + 1] = pre_e[i] + e
        pre_n[i + 1] = pre_n[i] + n_

    out: List[Tuple[float, float]] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n - 1, i + half)
        cnt = hi - lo + 1
        out.append((
            (pre_e[hi + 1] - pre_e[lo]) / cnt,
            (pre_n[hi + 1] - pre_n[lo]) / cnt,
        ))
    return out


def _boxcar_width(window_s: float, rate_hz: float) -> int:
    """平滑窗口秒数 → 采样点数，**强制为奇数**。

    偶数窗口没法居中，向前或向后多取一个，
    整条轨迹的速度会带一个固定方向的相位偏置 —— 与其引入这种偏差，
    不如把窗口加宽一个采样再居中。
    """
    width = max(1, int(round(window_s * rate_hz)))
    return width if width % 2 == 1 else width + 1


def _distances(ll: Sequence[Tuple[float, float]]) -> List[float]:
    """相邻点大圆距离列表，长度 = ``len(ll) - 1``。"""
    return [
        haversine(ll[i - 1][0], ll[i - 1][1], ll[i][0], ll[i][1])
        for i in range(1, len(ll))
    ]


def _p95(values: Sequence[float]) -> float:
    """第 95 百分位。用最近秩，不引入 numpy —— 这里样本量小、要的是稳健。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[k]


def preview(
    route: Route,
    *,
    center: tuple[float, float],
    diameter_m: float,
    laps: int = 1,
    vertices_per_lap: int = 360,
    speed_kmh: float = 10.0,
    rate_hz: float = 1.0,
    noise_level: str = "realistic",
    seed: int = 0,
    smooth_s: float = DEFAULT_SMOOTH_S,
    radius_limit_m: Optional[float] = None,
    sweep_windows: Sequence[float] = (),
) -> PreviewResult:
    """离线跑完整条轨迹，算出校园 App 会看到什么。**不碰设备。**

    :param radius_limit_m: 用户告知的围栏半径。``None`` = 未知，
        此时只报告最远距离，不做「会不会出界」的判定 ——
        拿一个猜的围栏去判「安全」，比不判更危险。
    :param sweep_windows: 额外要试算的平滑窗口（秒）。结果进 ``sweep``。
        轨迹**只模拟一次**，各窗口只是对同一批点做不同的滑动平均 ——
        所以加窗口几乎不要钱。
    """
    profile = SpeedProfile(v_bar=speed_kmh)
    noise = GPSNoise(level=noise_level, sample_rate=rate_hz, seed=seed)
    engine = TrackEngine(route, profile, noise, sample_rate=rate_hz)
    samples: List[Sample] = engine.simulate_offline()

    if len(samples) < 2:
        raise ValueError(
            f"路线太短，只跑出 {len(samples)} 个采样点，算不出 App 口径的距离与速度"
        )

    # ★ 围栏参考系用**真圆心**，不是引擎内部那个「路线首点」。
    #   路线首点在圆的东侧、离圆心 15 m，拿它当原点会整体偏掉一整个半径。
    proj = ENUProjector(center[0], center[1])

    radii: List[float] = []
    clean_radii: List[float] = []
    enu: List[Tuple[float, float]] = []
    for smp in samples:
        e, n = proj.to_enu(smp.lat, smp.lon)
        enu.append((e, n))
        radii.append(math.hypot(e, n))
        # 无噪声位置：由弧长反查路线，绕开引擎的投影中心
        c_lat, c_lon, _ = route.point_at(smp.s)
        ce, cn = proj.to_enu(c_lat, c_lon)
        clean_radii.append(math.hypot(ce, cn))

    # ── 逐点口径 ──
    raw = _distances([(s.lat, s.lon) for s in samples])
    # v_actual 首点是「无前一点」时的回落值，不代表 App 看到的瞬时速度 —— 排除。
    raw_v = [s.v_actual for s in samples[1:]]

    # ── 平滑口径 ──
    # 轨迹只模拟一次；每个窗口只是对同一批 ENU 点做不同的滑动平均。
    sweep: dict = {}
    for window_s in dict.fromkeys([smooth_s, *sweep_windows]):
        width = _boxcar_width(window_s, rate_hz)
        filt = _boxcar(enu, width)
        filt_ll = [proj.to_latlon(e, n) for e, n in filt]
        seg = _distances(filt_ll)
        vel = [d * rate_hz * 3.6 for d in seg]
        sweep[window_s] = {
            "width": width,
            "dist_m": sum(seg),
            "v_min_kmh": min(vel) if vel else 0.0,
            "v_max_kmh": max(vel) if vel else 0.0,
            "frac_below_min": (
                sum(1 for v in vel if v < R_MIN_SPEED_KMH) / len(vel) if vel else 0.0
            ),
        }

    default = sweep[smooth_s]
    v_min_smooth_kmh = default["v_min_kmh"]
    v_max_smooth_kmh = default["v_max_kmh"]
    dist_smooth_m = default["dist_m"]

    return PreviewResult(
        center_lat=center[0],
        center_lon=center[1],
        diameter_m=diameter_m,
        laps=laps,
        vertices_per_lap=vertices_per_lap,
        route_m=route.length(),
        speed_kmh=speed_kmh,
        rate_hz=rate_hz,
        noise_level=noise_level,
        seed=seed,
        smooth_s=smooth_s,
        samples=len(samples),
        duration_s=samples[-1].t,
        planned_eta_s=closed_form_eta(route.length(), speed_kmh, profile),
        dist_engine_m=route.length(),
        dist_raw_m=sum(raw),
        v_min_raw_kmh=min(raw_v),
        v_max_raw_kmh=max(raw_v),
        dist_smooth_m=dist_smooth_m,
        v_min_smooth_kmh=v_min_smooth_kmh,
        v_max_smooth_kmh=v_max_smooth_kmh,
        max_radius_clean_m=max(clean_radii),
        max_radius_noisy_m=max(radii),
        p95_radius_noisy_m=_p95(radii),
        radius_limit_m=radius_limit_m,
        frac_below_min_raw=(
            sum(1 for v in raw_v if v < R_MIN_SPEED_KMH) / len(raw_v) if raw_v else 0.0
        ),
        frac_below_min_smooth=default.get("frac_below_min", 0.0),
        sweep=sweep,
    )


# ── 判定 ────────────────────────────────────────────────────────────


def checks(p: PreviewResult, model: str) -> List[Check]:
    """按 ``model``（``"raw"`` / ``"smooth"``）逐条判 R1–R5。"""
    if model == "raw":
        dist, v_min, v_max = p.dist_raw_m, p.v_min_raw_kmh, p.v_max_raw_kmh
    elif model == "smooth":
        dist, v_min, v_max = p.dist_smooth_m, p.v_min_smooth_kmh, p.v_max_smooth_kmh
    else:
        raise ValueError(f"未知口径 {model!r}，可选：'raw' / 'smooth'")

    def margin(value: float, lo: Optional[float], hi: Optional[float]) -> float:
        """到最近边界的**相对**余量：0 = 刚好压线，负数 = **越界**。

        ⚠️ 分母必须是**边界自身的量级**，不能是「边界到当前值的距离」。
           两者相同时 margin 会恒等于 1.0 —— 第一版就是这么写的，
           于是「3004.7 m 压 3000 m 线」被算成「余量 100%」，
           余量检查形同虚设，搜索直接返回一个骑在线上的方案。
           由 ``test余量用边界自身作分母`` 守住。
        """
        if lo is None and hi is None:
            return 0.0
        gaps = []
        if lo is not None:
            gaps.append((value - lo) / abs(lo) if lo != 0.0 else value - lo)
        if hi is not None:
            gaps.append((hi - value) / abs(hi) if hi != 0.0 else hi - value)
        return min(gaps)

    return [
        Check("R1", "每次有效距离 ≥ 3 km", f"{dist:.1f} m",
              ok=dist >= R_MIN_DISTANCE_M, margin=margin(dist, R_MIN_DISTANCE_M, None)),
        Check("R2", "每次最大锻炼距离 ≤ 15 km", f"{dist:.1f} m",
              ok=dist <= R_MAX_DISTANCE_M, margin=margin(dist, None, R_MAX_DISTANCE_M)),
        Check("R3", "最低速度 ≥ 6 km/h", f"{v_min:.2f} km/h",
              ok=v_min >= R_MIN_SPEED_KMH, margin=margin(v_min, R_MIN_SPEED_KMH, None)),
        Check("R4", "最高速度 ≤ 17 km/h", f"{v_max:.2f} km/h",
              ok=v_max <= R_MAX_SPEED_KMH, margin=margin(v_max, None, R_MAX_SPEED_KMH)),
        Check("R5", "每次最长锻炼时间 ≤ 2 h", _dur(p.duration_s),
              ok=p.duration_s <= R_MAX_DURATION_S,
              margin=margin(p.duration_s, None, R_MAX_DURATION_S)),
    ]


def _dur(seconds: float) -> str:
    total = int(round(seconds))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def plan_laps(
    center: tuple[float, float],
    diameter_m: float,
    *,
    speed_kmh: float = 10.0,
    rate_hz: float = 1.0,
    noise_level: str = "realistic",
    seed: int = 0,
    smooth_s: float = DEFAULT_SMOOTH_S,
    min_laps: int = 1,
    max_laps: int = 400,
    min_margin: float = 0.02,
) -> PreviewResult:
    """搜出**能让 R1 与 R4 在两种口径下都通过**的最小圈数。

    为什么不能直接按「弧长 ≥ 3 km」定圈数
        实测：30 m 圈按弧长取 32 圈（3015.9 m）时，
        逐点口径 App 数出 3041.5 m（✓），但**做 5 s 平滑的 App 只数出 2910.6 m（✗）**。
        平滑会把圈「缩小」—— 34 s 的圈过一遍 5 s 的低通，幅度按 sinc 衰减约 3.6%。
        只按弧长定圈数，等于**默认 App 不滤波**；而真实 App 几乎都滤波。

    ★ 为什么「多跑几圈」是免费的
        圈与圈**几何完全重合**（:func:`~vrun.route.circle_route`），
        手机自始至终只在半径 ``diameter_m/2`` 内打转 —— 电子围栏一点没变，
        里程却线性累加。所以宁可多跑，不要在 3 km 线上赌 App 的滤波口径。

    ⚠️ 为什么**不**把 R3 纳入搜索
        R3「最低速度 ≥ 6 km/h」在逐点口径下是**采样点越多越糟**：
        噪声差分能抵消前进量，采样越密越容易撞到接近 0 的瞬时速度。
        把它写进搜索条件会得到一个「圈数越多、越不达标」的无解目标。
        R3 的口径必须由用户看 App 界面来定，不能由工具替他猜。
    """
    from .route import circle_route  # 局部导入避免环

    laps = max(1, min_laps)
    best: Optional[PreviewResult] = None
    while laps <= max_laps:
        route = circle_route(center, diameter_m, laps=laps)
        p = preview(
            route, center=center, diameter_m=diameter_m, laps=laps,
            speed_kmh=speed_kmh, rate_hz=rate_hz,
            noise_level=noise_level, seed=seed, smooth_s=smooth_s,
            sweep_windows=SWEEP_WINDOWS,
        )
        if _passes_distance_and_speed(p, min_margin=min_margin):
            return p
        best = p
        laps += 1

    # 搜到上限还没过：把最后一次结果交出去，**由 report 明确说没达标**，
    # 而不是静悄悄返回一个「差一点点也行的」方案。
    assert best is not None
    return best


#: 报告里固定试算这几个平滑窗口。选它们是因为 3 / 5 / 10 s 覆盖了
#: 「几乎不滤 / 常见 / 偏保守」三档，足以看出衰减对 R1 的敏感度。
SWEEP_WINDOWS: tuple = (3.0, 5.0, 10.0)


def _passes_distance_and_speed(p: PreviewResult, *, min_margin: float) -> bool:
    """R1（≥ 3 km）与 R4（≤ 17 km/h）在**两种口径下**都过，且 R1 有余量。

    ★ 为什么 R1 必须留余量，而不只是「大于 3000」
        实测：33 圈、5 s 窗口下，三个噪声种子分别数出
        3004.7 / 2993.0 / 3000.9 m —— **正好骑在 3000 线上，一个种子就翻车**。
        单一种子的「刚好压线」是假稳。余量 2% 覆盖了实测的种子波动（~0.4%）
        还留了五倍空间，而多跑几圈对电子围栏**零成本**（几何完全重合）。
    """
    for model in ("raw", "smooth"):
        got = {c.rule: c for c in checks(p, model)}
        if not got["R4"].ok:
            return False
        r1 = got["R1"]
        if not r1.ok:
            return False
        if model == "smooth" and r1.margin < min_margin:
            return False
    return True


def preview_lines(p: PreviewResult) -> List[str]:
    """渲染成人能扫的行。"""
    out = [
        f"圆心        {p.center_lat:.6f}, {p.center_lon:.6f}"
        f"（半径 {p.diameter_m / 2:.1f} m）",
        f"路线        同一个圆重复 {p.laps} 圈，几何完全重合",
        f"            每圈 {p.vertices_per_lap} 顶点，合计 {p.route_m:.1f} m",
        f"参数        {p.speed_kmh:g} km/h · {p.rate_hz:g} Hz · 噪声 {p.noise_level}"
        f" · seed {p.seed}",
        f"采样        {p.samples} 点，用时 {_dur(p.duration_s)}"
        f"（预览 ETA {_dur(p.planned_eta_s)}）",
        "",
        f"引擎推进    {p.dist_engine_m:.1f} m —— 这是**我们以为跑的**，不是 App 会数的",
    ]

    out += [
        "",
        "┌─ 电子围栏 ──────────────────────────────────────────────",
        f"│ 无噪声最远  {p.max_radius_clean_m:.2f} m（= 半径，几何保证）",
        f"│ 含噪声最远  {p.max_radius_noisy_m:.2f} m  ← 围栏判据看这个",
        f"│ 含噪声 p95  {p.p95_radius_noisy_m:.2f} m",
    ]
    within = p.within_fence()
    if within is None:
        out.append("│ 判定        **围栏半径未知** —— 上面三个数请自行对照")
    else:
        limit = p.radius_limit_m
        assert limit is not None
        slack = limit - p.max_radius_noisy_m
        out.append(
            f"│ 判定        {'✅ 在围栏内' if within else '❌ **超出围栏**'}"
            f"（余量 {slack:+.2f} m / 限 {limit:g} m）"
        )
    out.append("└─────────────────────────────────────────────────────────")

    for model, title in (
        ("raw", "逐点口径（App 不滤波）"),
        ("smooth", f"平滑口径（App 做 {p.smooth_s:g} s 滑动平均）"),
    ):
        out += ["", f"┌─ {title} " + "─" * max(0, 44 - len(title))]
        for c in checks(p, model):
            out.append(f"│ {'✓' if c.ok else '✗'} {c.rule}  {c.label:<24} {c.text:>14}")
        out.append("└" + "─" * 58)

    out += ["", "★ 两种口径都过 → 大概率判有效；只有逐点口径过 → 取决于 App 有没有滤波。",
            "  跑完看 App 报的里程/速度**落在哪一列**，就反推出它的口径了。"]

    # 窗口敏感性：30 m 圈的周期只有 34 s，滤波窗口每加长一档，衰减就重一档。
    # 这张表是「App 到底滤多久」这个未知量的直接读数。
    if len(p.sweep) > 1:
        out += [
            "",
            "平滑窗口敏感性（圈周期仅约 34 s，窗口越长衰减越狠）",
            f"  {'窗口':>8}  {'App 数出':>12}  {'R1 ≥3km':>8}  {'最低速':>10}",
        ]
        for w in sorted(p.sweep):
            row = p.sweep[w]
            ok = "✓" if row["dist_m"] >= R_MIN_DISTANCE_M else "✗"
            star = "  ← 默认" if abs(w - p.smooth_s) < 1e-9 else ""
            out.append(
                f"  {w:>7.0f} s  {row['dist_m']:>9.1f} m  {ok:>8}"
                f"  {row['v_min_kmh']:>9.2f}{star}"
            )
        worst = max(p.sweep)
        if p.sweep[worst]["dist_m"] < R_MIN_DISTANCE_M:
            out.append(
                f"  ⚠️ 若 App 用 {worst:g} s 窗口，R1 **不达标**（{p.sweep[worst]['dist_m']:.0f} m）。"
                "要么加大圈数，要么先确认它的滤波口径。"
            )
    return out


def rule_table_lines(p: PreviewResult) -> List[str]:
    """R1–R5 的对照表，两种口径并排 —— 这才是要交给人看的东西。"""
    raw = {c.rule: c for c in checks(p, "raw")}
    sm = {c.rule: c for c in checks(p, "smooth")}

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    lines = [f"{'规则':<4} {'内容':<24} {'逐点口径':>18} {'平滑口径':>18}", "-" * 68]
    for key in ("R1", "R2", "R3", "R4", "R5"):
        lines.append(
            f"{key:<4} {raw[key].label:<24} "
            f"{mark(raw[key].ok)} {raw[key].text:>16} "
            f"{mark(sm[key].ok)} {sm[key].text:>16}"
        )
    return lines
