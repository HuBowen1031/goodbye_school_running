"""engine.py — 轨迹引擎。

职责
    按**真实时钟**积分弧长，按采样率吐出 ``(t, lat, lon, s, v)`` 序列。

★ 关键设计：``clock`` 可注入
    +--------------+---------------------------+--------------------------------+
    | 场景         | 传入的 clock              | 效果                           |
    +==============+===========================+================================+
    | 真机跑       | ``time.monotonic``（默认）| 按真实流逝时间推进             |
    | 离线测试     | 「每次调用 +1 s」的假时钟 | 18 分钟的仿真 0.1 秒跑完       |
    +--------------+---------------------------+--------------------------------+

    没有这个参数，M1 的验收测试（「离线跑 3 km 并统计平均速度」）根本没法做 ——
    总不能每次跑测试都真的等 18 分钟。

积分规则
    ``s += v · Δt``，其中 ``Δt`` 由 ``clock()`` 的**差值**给出。
    **绝不能用固定 dt**：DVT 往返延迟会让固定 dt 与实际时间脱节，
    速度随之失真（works.md §4.3.1）。

依赖
    :mod:`vrun.route` / :mod:`vrun.profile` / :mod:`vrun.noise`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .geo import ENUProjector, haversine
from .noise import GPSNoise, SpeedJitter
from .profile import KMH_TO_MS, SpeedProfile
from .route import Route


@dataclass
class Sample:
    """一个采样点。"""

    t: float          # 已用时，秒
    lat: float
    lon: float
    s: float          # 已走弧长，米
    v_target: float   # 剖面给出的目标速度，km/h
    v_actual: float   # 本段实际平均速度，km/h（**含噪声位移的影响**）


@dataclass
class RunStats:
    """一次运行的汇总。"""

    duration_s: float
    distance_m: float
    mean_speed_kmh: float
    samples: int
    completed: bool


# 采样点时刻的浮点比较容差：假时钟下 t 与 t_emit 应当严格相等，
# 但真实模式下 clock 的抖动可能带来 1 ulp 级偏差。
_TIME_EPS = 1e-9


class TrackEngine:
    """沿路线推进弧长，产出带 GPS 噪声的位置序列。"""

    def __init__(
        self,
        route: Route,
        profile: SpeedProfile,
        noise: GPSNoise,
        sample_rate: float = 1.0,
    ) -> None:
        if sample_rate <= 0.0:
            raise ValueError(f"sample_rate 必须为正，收到 {sample_rate}")

        self.route = route
        self.profile = profile
        self.noise = noise
        self.sample_rate = float(sample_rate)

        # 投影中心取路线首点。噪声是「米制」的，必须先投影到局部平面再加，
        # 否则在经纬度上直接加偏移会随纬度产生各向异性失真。
        self.projector = ENUProjector(route.points[0][0], route.points[0][1])

        # η(t)：呼吸 / 路况引起的**速度**波动（works.md §3.2）。
        # 与 GPS 位置噪声是两条独立随机流 —— 前者是「跑者速度的自然起伏」，
        # 后者是「测量误差」，物理来源不同，混在一起会让口径含糊。
        # 但两者共享同一个由人工种子派生的流，所以可复现性不受影响。
        self.jitter = SpeedJitter(
            sigma_kmh=profile.sigma_v,
            tau_s=profile.tau_v,
            dt=1.0 / self.sample_rate,
            seed=getattr(noise, "seed", 0),
        )

    # ── 单点构造 ──────────────────────────────────────────────

    def _make_sample(
        self,
        t: float,
        s: float,
        dt: float,
        prev_ll: Optional[tuple[float, float]],
    ) -> tuple[Sample, tuple[float, float]]:
        """构造一个采样点：理想位置 → 叠加米制噪声 → 反投影回经纬度。"""
        total = self.route.length()
        v_target = self.profile.speed_at(t, s, total, self.jitter.step())

        lat, lon, _ = self.route.point_at(s)
        e, n = self.projector.to_enu(lat, lon)
        de, dn = self.noise.sample()
        o_lat, o_lon = self.projector.to_latlon(e + de, n + dn)

        if prev_ll is None or dt <= 0.0:
            # 首个采样点没有「前一点」，无法差分 —— 如实回落到目标速度
            v_actual = v_target
        else:
            moved = haversine(prev_ll[0], prev_ll[1], o_lat, o_lon)
            v_actual = (moved / dt) * 3.6

        sample = Sample(
            t=t,
            lat=o_lat,
            lon=o_lon,
            s=s,
            v_target=v_target,
            v_actual=v_actual,
        )
        return sample, (o_lat, o_lon)

    # ── 运行 ──────────────────────────────────────────────────

    def run(
        self,
        on_sample: Callable[[Sample], None],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> RunStats:
        """推进到路线终点；每到一个采样点调用 ``on_sample``。

        ``sleeper`` 是为可测性留的注入点：离线仿真时传 ``lambda _: None``，
        避免真的等待墙钟时间。
        """
        total = self.route.length()
        period = 1.0 / self.sample_rate

        t0 = clock()
        t_prev_emit = 0.0
        t_emit = 0.0
        s = 0.0
        prev_ll: Optional[tuple[float, float]] = None
        count = 0
        t = 0.0

        while True:
            t = clock() - t0

            if t + _TIME_EPS < t_emit:
                # 还没到下一个采样时刻：睡到那时再醒（真实模式），
                # 假时钟模式下 sleeper 是空函数，会立刻进入下一轮。
                gap = t_emit - t
                sleeper(gap if gap < period else period)
                continue

            dt = t - t_prev_emit
            sample, prev_ll = self._make_sample(t, s, dt, prev_ll)
            on_sample(sample)
            count += 1

            t_prev_emit = t
            t_emit = t + period

            if s >= total - 1e-9:
                break

            # 推进弧长。用「本采样周期」的 dt 积分 —— 绝不用固定 dt。
            s += (sample.v_target * KMH_TO_MS) * dt
            if s > total:
                s = total  # 越界钳制到终点，不做回退

        duration = t
        mean = (s / duration) * 3.6 if duration > 0.0 else 0.0
        return RunStats(
            duration_s=duration,
            distance_m=s,
            mean_speed_kmh=mean,
            samples=count,
            completed=s >= total - 1e-6,
        )

    # ── 离线仿真 ──────────────────────────────────────────────

    def simulate_offline(self) -> list[Sample]:
        """★ 注入假时钟，把整段轨迹一次算完（测试用，秒级完成）。

        假时钟每次调用前进一个采样周期，于是 ``run`` 里每一轮恰好命中一个
        采样点，``Δt`` 恒等于 ``1 / sample_rate``。
        """
        period = 1.0 / self.sample_rate
        counter = [0.0]

        def fake_clock() -> float:
            value = counter[0]
            counter[0] += period
            return value

        out: list[Sample] = []
        # 保证「同种子 ⇒ 同轨迹」，与这个对象是否已经跑过第二次无关
        self.noise.reset()
        self.jitter.reset()
        self.run(
            out.append,
            clock=fake_clock,
            sleeper=lambda _seconds: None,
        )
        return out
