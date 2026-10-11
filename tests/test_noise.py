"""test_noise.py — 双层 AR(1) GPS 噪声的测试。

核心断言：
1. **同种子逐字节可复现**（这是验收标准，不是锦上添花）；
2. 后验 σ 与设计值相符（±10%）；
3. lag-1 自相关很高 —— 证明它不是白噪声（真实 GPS 误差在时间上强相关）。
"""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest

from vrun.noise import AR1, GPSNoise

# 组合稳态 σ = √(σ_slow² + σ_fast²) = √(3.0² + 0.3²) ≈ 3.015 m
COMBINED_SIGMA = math.sqrt(GPSNoise.SIGMA_SLOW ** 2 + GPSNoise.SIGMA_FAST ** 2)


def _ensemble_sigma(
    level: str,
    n_seeds: int = 16,
    burn_in: int = 2000,
    samples: int = 8000,
) -> float:
    """多种子 + 偏差修正后的稳态 σ 估计。

    有两个坑，不处理就测不准：

    1. **慢分量的长相关**
       τ = 300 s，所以单种子的 2 万点里只有约 67 个「有效独立样本」，
       σ 的估计标准误接近 9%。→ 多种子平均。

    2. **样本方差对相关序列有系统性负偏差**
       对 AR(1)：E[s²] ≈ σ²·[1 − (1/n)·(1+ρ)/(1−ρ)]，
       而 (1+ρ)/(1−ρ) ≈ 2τ/dt（当 dt ≪ τ），即
       **E[s²] ≈ σ²·(1 − 2τ/T)**。
       τ=300 s、T=8000 s 时 σ 被低估 7.5%；T=4000 s 时低估 15%。
       → 按分量权重解析修正。

    这两条都不是实现问题，是**估计量的性质**。把它们写清楚，
    比把容差拍脑袋放宽要诚实得多。
    """
    k = GPSNoise.LEVELS[level]
    var_slow = (k * GPSNoise.SIGMA_SLOW) ** 2
    var_fast = (k * GPSNoise.SIGMA_FAST) ** 2
    w_slow = var_slow / (var_slow + var_fast)

    bias = w_slow * (1.0 - 2.0 * GPSNoise.TAU_SLOW / samples) + \
        (1.0 - w_slow) * (1.0 - 2.0 * GPSNoise.TAU_FAST / samples)

    sigmas: list[float] = []
    for seed in range(n_seeds):
        n = GPSNoise(level, 1.0, seed)
        for _ in range(burn_in):
            n.sample()
        sigmas.append(statistics.pstdev([n.sample()[0] for _ in range(samples)]))

    return statistics.fmean(sigmas) / math.sqrt(bias)


# ══════════════════════════════════════════════════════════════
#  可复现性
# ══════════════════════════════════════════════════════════════

class TestReproducibility:
    def test_same_seed_gives_identical_sequence(self) -> None:
        """逐元素**精确**相等，不用 approx —— 这正是「逐字节可复现」。"""
        a = GPSNoise("realistic", 1.0, 42)
        b = GPSNoise("realistic", 1.0, 42)
        assert [a.sample() for _ in range(500)] == [b.sample() for _ in range(500)]

    def test_reset_rewinds_the_random_stream(self) -> None:
        n = GPSNoise("realistic", 1.0, 42)
        first = [n.sample() for _ in range(300)]
        n.reset()
        assert [n.sample() for _ in range(300)] == first

    def test_reset_after_partial_use_matches_fresh_object(self) -> None:
        used = GPSNoise("realistic", 1.0, 7)
        for _ in range(137):
            used.sample()
        used.reset()
        fresh = GPSNoise("realistic", 1.0, 7)
        assert [used.sample() for _ in range(200)] == [fresh.sample() for _ in range(200)]

    def test_different_seeds_diverge(self) -> None:
        a = GPSNoise("realistic", 1.0, 1)
        b = GPSNoise("realistic", 1.0, 2)
        assert [a.sample() for _ in range(50)] != [b.sample() for _ in range(50)]


# ══════════════════════════════════════════════════════════════
#  档位
# ══════════════════════════════════════════════════════════════

class TestLevels:
    def test_off_returns_exact_zeros(self) -> None:
        n = GPSNoise("off", 1.0, 0)
        assert all(n.sample() == (0.0, 0.0) for _ in range(100))

    def test_level_keys_are_english(self) -> None:
        """键必须是英文 —— 避免中文字符串散落在 Python 与 Swift 两侧。"""
        assert set(GPSNoise.LEVELS) == {"off", "realistic", "harsh"}

    def test_unknown_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知噪声档位"):
            GPSNoise("extreme", 1.0, 0)

    def test_rejects_bad_sample_rate(self) -> None:
        with pytest.raises(ValueError):
            GPSNoise("realistic", 0.0, 0)

    def test_harsh_is_1_5x_realistic(self) -> None:
        real = _ensemble_sigma("realistic")
        harsh = _ensemble_sigma("harsh")
        assert harsh / real == pytest.approx(1.5, rel=0.12)

    def test_from_dict(self) -> None:
        n = GPSNoise.from_dict({"level": "harsh", "sample_rate": 2.0, "seed": 5})
        assert n.level == "harsh"
        assert n.sample_rate == 2.0
        assert n.seed == 5


# ══════════════════════════════════════════════════════════════
#  统计性质
# ══════════════════════════════════════════════════════════════

class TestStatistics:
    def test_steady_state_sigma_matches_design(self) -> None:
        assert _ensemble_sigma("realistic") == pytest.approx(COMBINED_SIGMA, rel=0.10)

    def test_not_white_noise(self) -> None:
        """★ AR(1) 的 lag-1 自相关应接近 1；白噪声会是 0 附近。"""
        n = GPSNoise("realistic", 1.0, 11)
        for _ in range(3000):
            n.sample()
        vals = [n.sample()[0] for _ in range(10000)]
        mean = statistics.fmean(vals)
        num = sum((vals[i] - mean) * (vals[i + 1] - mean) for i in range(len(vals) - 1))
        den = sum((v - mean) ** 2 for v in vals)
        assert num / den > 0.9

    def test_axes_are_independent(self) -> None:
        n = GPSNoise("realistic", 1.0, 5)
        for _ in range(3000):
            n.sample()
        pairs = [n.sample() for _ in range(10000)]
        es = [p[0] for p in pairs]
        ns = [p[1] for p in pairs]
        me, mn = statistics.fmean(es), statistics.fmean(ns)
        num = sum((e - me) * (x - mn) for e, x in pairs)
        den = math.sqrt(sum((e - me) ** 2 for e in es) * sum((x - mn) ** 2 for x in ns))
        assert abs(num / den) < 0.1

    def test_sample_mean_variance_matches_theory(self) -> None:
        """x̄ 的**分布**才是可检验的对象：Var(x̄) ≈ σ²·(2τ/T)。

        「均值应当接近 0」是**期望**的说法 —— 单次抽样当然不会正好为 0，
        直接断言它小于某个拍脑袋的数只会变成「测运气」。
        改成检验均值的方差是否与理论相符，这才是在测实现。
        """
        t_obs = 4000
        means: list[float] = []
        for seed in range(24):
            n = GPSNoise("realistic", 1.0, seed)
            for _ in range(1500):
                n.sample()
            means.append(statistics.fmean([n.sample()[0] for _ in range(t_obs)]))

        predicted_se = COMBINED_SIGMA * math.sqrt(2 * GPSNoise.TAU_SLOW / t_obs)
        assert statistics.pstdev(means) == pytest.approx(predicted_se, rel=0.40)


# ══════════════════════════════════════════════════════════════
#  跳点
# ══════════════════════════════════════════════════════════════

class TestSpikes:
    def test_disabled_by_default(self) -> None:
        n = GPSNoise("realistic", 1.0, 1)
        vals = [abs(n.sample()[0]) for _ in range(2000)]
        # 没有跳点时不应出现 > 20 m 的离群点
        assert max(vals) < 20.0

    def test_enabled_produces_outliers(self) -> None:
        n = GPSNoise("realistic", 1.0, 1, enable_spikes=True)
        vals = [abs(n.sample()[0]) for _ in range(20000)]
        # 期望触发约 20000/180 ≈ 111 次，出现 > 20 m 的点是正常的
        assert max(vals) > 20.0

    def test_spike_still_reproducible(self) -> None:
        a = GPSNoise("realistic", 1.0, 3, enable_spikes=True)
        b = GPSNoise("realistic", 1.0, 3, enable_spikes=True)
        assert [a.sample() for _ in range(1000)] == [b.sample() for _ in range(1000)]


# ══════════════════════════════════════════════════════════════
#  AR1 本体
# ══════════════════════════════════════════════════════════════

class TestAR1:
    def test_rho_derived_from_dt(self) -> None:
        a = AR1(1.0, 300.0, 1.0, np.random.default_rng(0))
        assert a._rho == pytest.approx(math.exp(-1.0 / 300.0), rel=1e-12)

    def test_resample_updates_rho(self) -> None:
        """ρ 必须按新 Δt 重算，不能缓存常量。"""
        a = AR1(0.3, 1.5, 1.0, np.random.default_rng(0))
        rho_at_1hz = a._rho
        a.resample(0.2)
        assert a._rho > rho_at_1hz          # 更短的 Δt → 更强的自相关
        assert a._rho == pytest.approx(math.exp(-0.2 / 1.5), rel=1e-12)

    def test_scale_keeps_steady_state_sigma(self) -> None:
        """√(1−ρ²)·σ 这个系数保证稳态标准差恰为 σ，不随 Δt 漂移。"""
        for dt in (0.2, 0.5, 1.0, 2.0):
            a = AR1(1.0, 10.0, dt, np.random.default_rng(0))
            for _ in range(5000):
                a.step()
            vals = [a.step() for _ in range(30000)]
            assert statistics.pstdev(vals) == pytest.approx(1.0, rel=0.10)
