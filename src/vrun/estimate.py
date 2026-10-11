"""estimate.py — 配速 / ETA / 安全带 ★

职责
    GUI 预览的**权威数值来源**：
    works.md §3.6 闭式解 + 精确 ETA + §3.5 表观抖动 + §5.3 安全带
    + §5.4 golden 表 + R1–R5 判定预期。**纯计算，不碰设备。**

为什么是 ★
    ``vrun estimate`` 不需要设备 ⇒ **滑块面板的全部逻辑在 M1 就能离线验证完**。
    这也是「没插 iPhone 时面板仍然完全可用」的技术基础（works.md §5.7）。

依赖
    :mod:`vrun.profile` / :mod:`vrun.noise`（只用其常量，不产生随机数）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Final, Optional

from .noise import GPSNoise
from .profile import KMH_TO_MS, SpeedProfile

# ── 校园 App 的规则窗口（works.md §1.1）──────────────────────
V_MIN: Final[float] = 6.0        # R3 速度下限 km/h（= 配速 10:00 /km）
V_MAX: Final[float] = 17.0       # R4 速度上限 km/h（= 配速 3:32 /km）
D_MIN_M: Final[float] = 3000.0   # R1 有效距离下限
D_MAX_M: Final[float] = 15000.0  # R2 单次最大距离
T_MAX_S: Final[float] = 7200.0   # R5 单次最长时长（2 h）

# §5.4 golden 表的采样点（既是预览的期望输出，也是 tests/golden 的种子）
GOLDEN_SPEEDS: Final[tuple[float, ...]] = (
    6.0, 8.0, 9.0, 10.0, 10.2, 12.0, 12.4, 13.0, 14.0, 16.5, 17.0,
)


# ══════════════════════════════════════════════════════════════
#  格式化
# ══════════════════════════════════════════════════════════════

def format_pace(seconds_per_km: float) -> str:
    """配速（秒每公里）→ ``"m:ss"``。"""
    total = int(round(seconds_per_km))
    return f"{total // 60}:{total % 60:02d}"


def format_duration(seconds: float) -> str:
    """时长（秒）→ ``"m:ss"``；超过 1 小时用 ``"h:mm:ss"``。"""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ══════════════════════════════════════════════════════════════
#  §3.5 表观速度抖动
# ══════════════════════════════════════════════════════════════

def sigma_apparent(dt: float, level: str = "realistic") -> float:
    """朴素前后差分下的表观速度标准差 1σ，单位 km/h。

    works.md §3.5::

        单轴一阶差分标准差   dᵢ = σᵢ · √( 2(1 − exp(−Δt/τᵢ)) )
        二维位移误差         D  = √2 · √( d_slow² + d_fast² )
        表观速度误差         Δv = D / Δt

    反直觉但重要的结论：**采样率越高，朴素差分的速度噪声越大（∝ 1/√Δt）**。
    1 Hz 时约 2.0 km/h，5 Hz 时高达 4.7 km/h —— 所以默认 1 Hz，不是越快越好。
    """
    if dt <= 0.0:
        raise ValueError(f"采样间隔必须为正，收到 {dt}")
    if level not in GPSNoise.LEVELS:
        raise ValueError(f"未知噪声档位 {level!r}，可选：{sorted(GPSNoise.LEVELS)}")

    scale = GPSNoise.LEVELS[level]
    if scale == 0.0:
        return 0.0

    d_slow = GPSNoise.SIGMA_SLOW * scale * math.sqrt(
        2.0 * (1.0 - math.exp(-dt / GPSNoise.TAU_SLOW))
    )
    d_fast = GPSNoise.SIGMA_FAST * scale * math.sqrt(
        2.0 * (1.0 - math.exp(-dt / GPSNoise.TAU_FAST))
    )
    displacement = math.sqrt(2.0) * math.sqrt(d_slow * d_slow + d_fast * d_fast)
    return (displacement / dt) * 3.6


# ══════════════════════════════════════════════════════════════
#  §5.3 噪声安全带
# ══════════════════════════════════════════════════════════════

def safety_band(
    sigma_v: float,
    profile: SpeedProfile,
    levels: float = 2.0,
) -> Optional[tuple[float, float]]:
    """返回使 ``levels·σ`` 内**绝不越界**的 ``v̄`` 区间；无解返回 ``None``。

    works.md §5.3::

        下界（最慢） v_lo = v̄ · (1 + δ) − kσ   需 ≥ 6
        上界（最快） v_hi = v̄ · (1 + f) + kσ   需 ≤ 17

    反解出 ``v̄`` 的约束区间。**无解是真实结论，不是 bug** ——
    严苛噪声下（σ = 3.0）约束要求 ``v̄ ≥ 12.18`` 同时 ``v̄ ≤ 10.48``，
    交集为空：噪声大到一定程度，合法速度区间会整个消失。
    """
    lo = (V_MIN + levels * sigma_v) / (1.0 + profile.delta)
    hi = (V_MAX - levels * sigma_v) / (1.0 + profile.f)
    if lo > hi:
        return None
    return (lo, hi)


def safety_color(v_bar: float, sigma_v: float, profile: SpeedProfile) -> str:
    """三级判定：``"green"`` / ``"yellow"`` / ``"red"``（works.md §5.3）。"""
    band2 = safety_band(sigma_v, profile, 2.0)
    if band2 is not None and band2[0] <= v_bar <= band2[1]:
        return "green"
    band1 = safety_band(sigma_v, profile, 1.0)
    if band1 is not None and band1[0] <= v_bar <= band1[1]:
        return "yellow"
    return "red"


# ══════════════════════════════════════════════════════════════
#  §3.6 闭式解（GUI 拖动时的即时反馈）
# ══════════════════════════════════════════════════════════════

def closed_form_eta(
    distance_m: float,
    speed_kmh: float,
    profile: SpeedProfile,
) -> float:
    """§3.6 的一阶近似修正 ETA（秒）。

    GUI 在 ≥ 60 fps 拖动时用它做即时反馈；松手后由 :func:`estimate` 的
    数值积分结果替换。两者差值 > 1 s 时 GUI 会高亮提示（§5.6）。
    """
    t_raw = distance_m / (speed_kmh * KMH_TO_MS)
    return (
        t_raw * (1.0 + abs(profile.delta) / 2.0 - 0.04 * profile.f)
        + profile.t_w * (1.0 - profile.r0) / 2.0
    )


# ══════════════════════════════════════════════════════════════
#  R1–R5 判定预期
# ══════════════════════════════════════════════════════════════

def build_verdict(
    distance_m: float,
    speed_kmh: float,
    eta_s: float,
    sigma_v: float,
    profile: SpeedProfile,
) -> dict[str, str]:
    """按 R1–R5 给出 ✓ / ⚠️ / ✗ 预测，供实测比对（works.md §5.5）。

    ⚠️ 只用于**预期**，不是判决 —— 真实判定由校园 App 做出，本工具不逆向它。
    """
    verdict: dict[str, str] = {}

    verdict["R1"] = "✓" if distance_m >= D_MIN_M else "✗ 不足 3 km"
    verdict["R2"] = "✓" if distance_m <= D_MAX_M else "✗ 超过 15 km"
    verdict["R5"] = "✓" if eta_s <= T_MAX_S else "✗ 超过 2 h"

    if speed_kmh < V_MIN:
        verdict["R3"] = "✗ 低于 6 km/h"
    elif speed_kmh * (1.0 + profile.delta) - 2.0 * sigma_v < V_MIN:
        verdict["R3"] = "⚠️ 噪声下可能过慢"
    else:
        verdict["R3"] = "✓"

    if speed_kmh > V_MAX:
        verdict["R4"] = "✗ 高于 17 km/h"
    elif speed_kmh * (1.0 + profile.f) + 2.0 * sigma_v > V_MAX:
        verdict["R4"] = "⚠️ 噪声下可能过快"
    else:
        verdict["R4"] = "✓"

    return verdict


# ══════════════════════════════════════════════════════════════
#  结果对象与主入口
# ══════════════════════════════════════════════════════════════

@dataclass
class EstimateResult:
    """面板一次预览所需的全部数值。"""

    speed_kmh: float
    distance_m: float
    noise_level: str
    sample_rate: float

    pace_s_per_km: float                      # 配速，秒每公里
    pace_str: str                             # "6:00"
    eta_naive_s: float                        # 纯运动（D / v）
    eta_s: float                              # 修正 ETA（数值积分，权威）
    eta_closed_form_s: float                  # 修正 ETA（一阶近似，GUI 用）
    eta_str: str

    sigma_v: float                            # 表观速度抖动 1σ，km/h
    band_1sigma: Optional[tuple[float, float]]
    band_2sigma: Optional[tuple[float, float]]  # None = 无安全带（严苛档退化）
    safety: str                               # green / yellow / red
    v_lo: float                               # 漂移后慢端 − 2σ
    v_hi: float                               # 冲刺后快端 + 2σ

    verdict: dict[str, str] = field(default_factory=dict)
    clamp_enabled: bool = True

    @property
    def eta_naive_str(self) -> str:
        return format_duration(self.eta_naive_s)

    @property
    def eta_closed_form_str(self) -> str:
        return format_duration(self.eta_closed_form_s)


def estimate(
    speed_kmh: float,
    distance_m: float = D_MIN_M,
    noise_level: str = "realistic",
    sample_rate: float = 1.0,
    profile: Optional[SpeedProfile] = None,
    seed: int = 0,
) -> EstimateResult:
    """滑块面板的**权威**计算入口。

    ``seed`` 目前不参与本函数的确定性计算（它由 :mod:`vrun.noise` 在生成
    轨迹时使用）。保留这个参数是为了让 GUI 侧车两侧的调用签名一致 ——
    ``estimate`` 与 ``engine`` 对同一组参数必须给出可对照的结果。
    """
    if speed_kmh <= 0.0:
        raise ValueError(f"速度必须为正，收到 {speed_kmh}")
    if distance_m <= 0.0:
        raise ValueError(f"距离必须为正，收到 {distance_m}")
    if sample_rate <= 0.0:
        raise ValueError(f"采样率必须为正，收到 {sample_rate}")
    _ = seed  # 见 docstring：签名对齐用

    dt = 1.0 / sample_rate
    sigma_v = sigma_apparent(dt, noise_level)

    base = profile if profile is not None else SpeedProfile()

    # ⚠️ 贴线设定（6.0 / 17.0）必须**关闭夹紧**。
    # 夹紧的本职是防止「剖面本身」把速度推出窗口；但贴线测试正是要观察
    # 越界行为，此时夹紧会让测不出边界（works.md §3.2 末尾的 ⚠️）。
    is_edge = speed_kmh <= V_MIN or speed_kmh >= V_MAX
    clamp = (not is_edge) and base.clamp_enabled
    prof = replace(base, v_bar=speed_kmh, clamp_enabled=clamp)

    eta_naive = distance_m / (speed_kmh * KMH_TO_MS)
    eta_exact = prof.expected_duration(distance_m)
    eta_closed = closed_form_eta(distance_m, speed_kmh, prof)

    band1 = safety_band(sigma_v, prof, 1.0)
    band2 = safety_band(sigma_v, prof, 2.0)
    color = safety_color(speed_kmh, sigma_v, prof)

    v_lo = speed_kmh * (1.0 + prof.delta) - 2.0 * sigma_v
    v_hi = speed_kmh * (1.0 + prof.f) + 2.0 * sigma_v

    return EstimateResult(
        speed_kmh=speed_kmh,
        distance_m=distance_m,
        noise_level=noise_level,
        sample_rate=sample_rate,
        pace_s_per_km=3600.0 / speed_kmh,
        pace_str=format_pace(3600.0 / speed_kmh),
        eta_naive_s=eta_naive,
        eta_s=eta_exact,
        eta_closed_form_s=eta_closed,
        eta_str=format_duration(eta_exact),
        sigma_v=sigma_v,
        band_1sigma=band1,
        band_2sigma=band2,
        safety=color,
        v_lo=v_lo,
        v_hi=v_hi,
        verdict=build_verdict(distance_m, speed_kmh, eta_exact, sigma_v, prof),
        clamp_enabled=clamp,
    )


def golden_rows(
    distance_m: float = D_MIN_M,
    noise_level: str = "realistic",
    sample_rate: float = 1.0,
) -> list[EstimateResult]:
    """算出 works.md §5.4 基准表的全部行（pytest 与 Swift 单测共用）。"""
    return [
        estimate(v, distance_m=distance_m, noise_level=noise_level,
                 sample_rate=sample_rate)
        for v in GOLDEN_SPEEDS
    ]
