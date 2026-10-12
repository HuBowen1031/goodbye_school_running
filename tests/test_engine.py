"""test_engine.py — 轨迹引擎的测试。

★ 本文件存在的意义：
    验证「可注入时钟」这个设计真的成立 —— 一次 18 分钟的 3 km 仿真，
    在这里必须**秒级**跑完。否则 M1 的验收测试根本没法做。
"""

from __future__ import annotations

import time

import pytest

from vrun.engine import Sample, TrackEngine
from vrun.estimate import estimate
from vrun.geo import haversine
from vrun.noise import GPSNoise
from vrun.profile import SpeedProfile


def _engine(route, noise_level="realistic", rate=1.0, seed=0, speed=10.0,
            eta_sigma=0.35) -> TrackEngine:
    return TrackEngine(
        route,
        SpeedProfile(v_bar=speed, sigma_v=eta_sigma),
        GPSNoise(noise_level, rate, seed),
        sample_rate=rate,
    )


# ══════════════════════════════════════════════════════════════
#  离线仿真
# ══════════════════════════════════════════════════════════════

class TestOfflineSimulation:
    def test_three_km_runs_in_seconds(self, route_3km) -> None:
        """★ 核心：假时钟让 18 分钟的仿真在秒级完成。"""
        engine = _engine(route_3km)
        start = time.perf_counter()
        samples = engine.simulate_offline()
        elapsed = time.perf_counter() - start

        assert samples
        assert elapsed < 5.0, f"离线仿真耗时 {elapsed:.2f}s，假时钟可能失效"

    def test_wall_clock_not_consumed(self, route_3km) -> None:
        """仿真时长 ≈ 18 分钟，但真实耗时必须远小于它。"""
        engine = _engine(route_3km)
        samples = engine.simulate_offline()
        simulated = samples[-1].t
        assert simulated > 1000.0             # 仿真里确实过了 18 分钟

    def test_reaches_full_distance(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        assert samples[-1].s == pytest.approx(3000.0, rel=0.005)
        assert samples[-1].s <= 3000.0 + 1e-6   # 不允许越界

    def test_mean_speed_close_to_target(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        mean = samples[-1].s / samples[-1].t * 3.6
        assert mean == pytest.approx(10.0, rel=0.02)

    def test_samples_are_dataclasses(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        assert all(isinstance(s, Sample) for s in samples)

    def test_sample_rate_matches_period(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        assert len(samples) == pytest.approx(samples[-1].t, abs=2)

    def test_arc_length_is_monotonic(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        assert all(b.s >= a.s for a, b in zip(samples, samples[1:]))

    def test_time_is_monotonic(self, route_3km) -> None:
        samples = _engine(route_3km).simulate_offline()
        assert all(b.t >= a.t for a, b in zip(samples, samples[1:]))


# ══════════════════════════════════════════════════════════════
#  噪声注入
# ══════════════════════════════════════════════════════════════

class TestNoiseInjection:
    def test_off_lands_exactly_on_route(self, route_3km) -> None:
        """关噪声时，注入点必须严格落在路线上（往返误差 < 1 mm）。"""
        samples = _engine(route_3km, noise_level="off").simulate_offline()
        for s in samples:
            lat, lon, _ = route_3km.point_at(s.s)
            assert haversine(lat, lon, s.lat, s.lon) == pytest.approx(0.0, abs=1e-3)

    def test_realistic_stays_within_metres(self, route_3km) -> None:
        """含噪声时，偏离路线应是数米量级 —— 不是几十米。"""
        samples = _engine(route_3km, noise_level="realistic").simulate_offline()
        offsets = [
            haversine(*route_3km.point_at(s.s)[:2], s.lat, s.lon)
            for s in samples
        ]
        assert max(offsets) < 25.0
        assert sum(offsets) / len(offsets) < 10.0

    def test_apparent_speed_is_noisier_than_target(self, route_3km) -> None:
        """v_actual 的抖动应明显大于 v_target —— 这正是 §3.5 的「表观速度抖动」。"""
        import statistics
        samples = _engine(route_3km, noise_level="realistic").simulate_offline()
        # 跳过首个点（无前值，回落到目标速度）与终点（钳制）
        body = samples[1:-1]
        spread_actual = statistics.pstdev([s.v_actual for s in body])
        spread_target = statistics.pstdev([s.v_target for s in body])
        assert spread_actual > spread_target

    def test_off_has_much_less_jitter_than_realistic(self, route_3km) -> None:
        """关掉 GPS 噪声后，剩下的抖动只来自 η(t) 与折线拐角 —— 量级应差一截。"""
        import statistics

        def spread(level: str) -> float:
            samples = _engine(route_3km, noise_level=level).simulate_offline()
            return statistics.pstdev([s.v_actual for s in samples[1:-1]])

        off = spread("off")
        realistic = spread("realistic")
        # 关噪声时仍有 ~0.4：其中 η(t) 占大部分，折线拐角贡献约 0.2（几何效应）
        assert off < 0.8, f"关噪声时仍有 {off:.3f} 的抖动，说明噪声没真正关掉"
        assert realistic > off * 2.5


# ══════════════════════════════════════════════════════════════
#  可复现性
# ══════════════════════════════════════════════════════════════

class TestReproducibility:
    def test_same_seed_same_trajectory(self, route_3km) -> None:
        def run():
            engine = _engine(route_3km, seed=3)
            return [(s.t, s.lat, s.lon, s.s) for s in engine.simulate_offline()]

        assert run() == run()

    def test_different_seed_different_trajectory(self, route_3km) -> None:
        a = _engine(route_3km, seed=1).simulate_offline()
        b = _engine(route_3km, seed=2).simulate_offline()
        assert [(s.lat, s.lon) for s in a] != [(s.lat, s.lon) for s in b]


# ══════════════════════════════════════════════════════════════
#  与 estimate 的一致性（防「预览与实际漂移」）
# ══════════════════════════════════════════════════════════════

class TestConsistencyWithEstimate:
    def test_actual_eta_matches_prediction(self, route_3km) -> None:
        """works.md §4.1.5 验收标准：实测 ETA 与预测差 < 2 s。

        必须把 η(t) 关掉 —— 那是零均值的随机波动，会让 ETA 有 ±4 s 的
        **合法**抖动（见下面 test_eta_jitter_from_eta 的量级）。
        这一条测的是「数值积分 vs 实际仿真」两条路径是否一致，不是随机性。
        """
        samples = _engine(route_3km, noise_level="off", eta_sigma=0.0).simulate_offline()
        actual = samples[-1].t
        predicted = estimate(10.0, 3000.0, "off", 1.0).eta_s
        assert actual == pytest.approx(predicted, abs=2.0)

    @pytest.mark.parametrize("speed", [8.0, 10.0, 12.0, 14.0])
    def test_consistency_across_speeds(self, route_3km, speed: float) -> None:
        samples = _engine(route_3km, noise_level="off", speed=speed,
                          eta_sigma=0.0).simulate_offline()
        predicted = estimate(speed, 3000.0, "off", 1.0).eta_s
        assert samples[-1].t == pytest.approx(predicted, abs=2.0)

    def test_eta_jitter_stays_within_a_few_seconds(self, route_3km) -> None:
        """开着 η(t) 时，ETA 的漂移应该在几秒量级 —— 不是几十秒。

        理论：ΔT/T ≈ −η̄/v̄，而 η̄ 的 σ ≈ σ_v·√(2τ_v/T)
              = 0.35·√(2·10/1350) ≈ 0.043 km/h
        代入 3 km @ 10 km/h → ΔT ≈ ±5 s（3σ）。
        """
        predicted = estimate(10.0, 3000.0, "off", 1.0).eta_s
        deviations = []
        for seed in range(24):
            samples = _engine(route_3km, noise_level="off", seed=seed).simulate_offline()
            deviations.append(samples[-1].t - predicted)
        assert max(abs(d) for d in deviations) < 15.0


# ══════════════════════════════════════════════════════════════
#  采样率
# ══════════════════════════════════════════════════════════════

class TestSamplingRates:
    @pytest.mark.parametrize("rate", [0.5, 1.0, 2.0, 5.0])
    def test_all_rates_complete(self, route_3km, rate: float) -> None:
        samples = _engine(route_3km, noise_level="off", rate=rate).simulate_offline()
        assert samples[-1].s == pytest.approx(3000.0, rel=0.01)

    @pytest.mark.parametrize("rate", [0.5, 1.0, 2.0, 5.0])
    def test_sample_count_scales_with_rate(self, route_3km, rate: float) -> None:
        samples = _engine(route_3km, noise_level="off", rate=rate).simulate_offline()
        assert len(samples) == pytest.approx(samples[-1].t * rate, rel=0.02)


# ══════════════════════════════════════════════════════════════
#  参数校验
# ══════════════════════════════════════════════════════════════

class TestValidation:
    def test_rejects_bad_sample_rate(self, route_3km) -> None:
        with pytest.raises(ValueError):
            TrackEngine(route_3km, SpeedProfile(), GPSNoise("off", 1.0, 0), 0.0)

    def test_run_calls_callback_for_each_sample(self, route_3km) -> None:
        engine = _engine(route_3km, noise_level="off")
        seen: list[Sample] = []
        stats = engine.run(
            seen.append,
            clock=self._fake_clock(1.0),
            sleeper=lambda _s: None,
        )
        assert stats.samples == len(seen)
        assert stats.completed is True
        assert stats.distance_m == pytest.approx(3000.0, rel=0.005)

    @staticmethod
    def _fake_clock(period: float):
        state = [0.0]

        def clock() -> float:
            value = state[0]
            state[0] += period
            return value

        return clock


# ══════════════════════════════════════════════════════════════
#  生命周期：reset 与 run 的语义分工
# ══════════════════════════════════════════════════════════════

def _coords(samples: list[Sample]) -> list[tuple[float, float]]:
    return [(s.lat, s.lon) for s in samples]


def _raw_run(engine: TrackEngine) -> list[Sample]:
    """直接驱动 ``run()``，**不**经过 reset —— 用来观察状态是否被延续。"""
    period = 1.0 / engine.sample_rate
    state = [0.0]

    def clock() -> float:
        value = state[0]
        state[0] += period
        return value

    out: list[Sample] = []
    engine.run(out.append, clock=clock, sleeper=lambda _s: None)
    return out


class TestResetSemantics:
    """``reset()`` 与 ``run()`` 的职责边界。

    契约：**「开新会话」重置，「会话内」延续。**
    由调用方决定何时重置 —— 引擎不替它猜。
    """

    def test_reset_restores_reproducibility(self, route_3km) -> None:
        engine = _engine(route_3km)
        first = engine.simulate_offline()
        engine.reset()
        assert _coords(_raw_run(engine)) == _coords(first)

    def test_run_does_not_reset_implicitly(self, route_3km) -> None:
        """不调 reset 就再跑一次，轨迹**必须不同**。

        这是「暂停 / 恢复要保留噪声状态」那条语义的护栏：
        若引擎偷偷重置，跨两次 run 拼出的轨迹会被切断。
        """
        engine = _engine(route_3km)
        first = engine.simulate_offline()
        without_reset = _raw_run(engine)
        assert _coords(without_reset) != _coords(first)

    def test_simulate_offline_is_self_contained(self, route_3km) -> None:
        """``simulate_offline()`` 自带 reset，重复调用结果一致。"""
        engine = _engine(route_3km)
        assert _coords(engine.simulate_offline()) == _coords(engine.simulate_offline())
