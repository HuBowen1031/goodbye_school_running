"""test_profile.py — 速度剖面的测试。

核心断言：
- ramp 段从 ``r₀`` 平滑升到 1，且默认 ``r₀=0.8`` 时**不触发过慢判定**；
- clamp 在窗口内生效、在贴线设定下让位；
- ``expected_duration`` 与 §3.6 的闭式解相差 < 2 s（两者是同一模型的两条路径）。
"""

from __future__ import annotations

import pytest

from vrun.estimate import closed_form_eta
from vrun.profile import PRESETS, SpeedProfile


# ══════════════════════════════════════════════════════════════
#  ramp
# ══════════════════════════════════════════════════════════════

class TestRamp:
    def test_starts_at_r0_ratio(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0, finish_frac=0.0)
        assert p.speed_at(0.0, 0.0, 3000.0) == pytest.approx(8.0, rel=1e-12)

    def test_reaches_target_after_t_w(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0, finish_frac=0.0)
        assert p.speed_at(30.0, 0.0, 3000.0) == pytest.approx(10.0, rel=1e-12)
        assert p.speed_at(300.0, 0.0, 3000.0) == pytest.approx(10.0, rel=1e-12)

    def test_ramp_is_monotonic(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0, finish_frac=0.0)
        vals = [p.speed_at(float(t), 0.0, 3000.0) for t in range(31)]
        assert all(b >= a for a, b in zip(vals, vals[1:]))

    def test_default_ramp_never_dips_below_6(self) -> None:
        """``r₀ = 0.8`` 的刻意选择：起跑瞬间不触发过慢判定（works.md §3.2）。"""
        p = SpeedProfile()  # 默认 v_bar = 10
        for t in range(31):
            assert p.speed_at(float(t), 0.0, 3000.0) >= 6.0

    def test_risky_cold_start_would_dip_below_6(self) -> None:
        """对照：更真实的 r₀=0.6 确实会掉到 6 以下 —— 所以默认没用它。"""
        p = SpeedProfile(v_bar=10.0, r0=0.6, delta=0.0, finish_frac=0.0,
                         clamp_enabled=False)
        assert p.speed_at(0.0, 0.0, 3000.0) == pytest.approx(6.0)

    def test_zero_t_w_means_no_ramp(self) -> None:
        p = SpeedProfile(v_bar=10.0, t_w=0.0, delta=0.0, finish_frac=0.0)
        assert p.speed_at(0.0, 0.0, 3000.0) == pytest.approx(10.0, rel=1e-12)


# ══════════════════════════════════════════════════════════════
#  drift
# ══════════════════════════════════════════════════════════════

class TestDrift:
    def test_linear_decline_to_one_minus_delta(self) -> None:
        p = SpeedProfile(v_bar=10.0, t_w=0.0, finish_frac=0.0)
        t_ref = p.reference_duration(3000.0)
        assert p.drift(t_ref, 3000.0) == pytest.approx(1.0 - 0.015, rel=1e-12)

    def test_starts_at_one(self) -> None:
        p = SpeedProfile()
        assert p.drift(0.0, 3000.0) == pytest.approx(1.0, rel=1e-12)

    def test_positive_delta_means_speeding_up(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.02)
        assert p.drift(500.0, 3000.0) > 1.0

    def test_reference_duration_is_pure_motion(self) -> None:
        p = SpeedProfile(v_bar=10.0)
        # 3 km @ 10 km/h = 1080 s
        assert p.reference_duration(3000.0) == pytest.approx(1080.0, rel=1e-9)


# ══════════════════════════════════════════════════════════════
#  finish
# ══════════════════════════════════════════════════════════════

class TestFinish:
    def test_no_kick_before_final_stretch(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0)
        assert p.finish(0.9 * 3000.0, 3000.0) == pytest.approx(1.0, rel=1e-12)

    def test_kick_peaks_at_the_end(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0)
        assert p.finish(3000.0, 3000.0) == pytest.approx(1.05, rel=1e-12)

    def test_kick_is_linear_inside_stretch(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0, finish_frac=0.10, f=0.10)
        # 冲刺区中点 → 一半幅度
        assert p.finish(0.95 * 3000.0, 3000.0) == pytest.approx(1.05, rel=1e-9)

    def test_disabled(self) -> None:
        p = SpeedProfile(v_bar=10.0, delta=0.0, finish_frac=0.0)
        assert p.finish(3000.0, 3000.0) == pytest.approx(1.0, rel=1e-12)


# ══════════════════════════════════════════════════════════════
#  夹紧
# ══════════════════════════════════════════════════════════════

class TestClamp:
    def test_floor_applies(self) -> None:
        # 6.5 · 0.8 = 5.2 < 6.3 → 夹到 6.3
        p = SpeedProfile(v_bar=6.5, delta=0.0, finish_frac=0.0)
        assert p.speed_at(0.0, 0.0, 3000.0) == pytest.approx(6.3)

    def test_ceiling_applies(self) -> None:
        # 17 · 1.05 = 17.85 > 16.7 → 夹到 16.7
        p = SpeedProfile(v_bar=17.0, t_w=0.0, delta=0.0)
        assert p.speed_at(3000.0, 3000.0, 3000.0) == pytest.approx(16.7)

    def test_disabled_lets_profile_run_free(self) -> None:
        p = SpeedProfile(v_bar=6.5, delta=0.0, finish_frac=0.0, clamp_enabled=False)
        assert p.speed_at(0.0, 0.0, 3000.0) == pytest.approx(5.2, rel=1e-9)

    def test_no_clamping_in_normal_range(self) -> None:
        p = SpeedProfile(v_bar=10.0)
        for t in range(0, 1200, 30):
            v = p.speed_at(float(t), t * 2.5, 3000.0)
            assert p.v_floor <= v <= p.v_ceil


# ══════════════════════════════════════════════════════════════
#  精确 ETA
# ══════════════════════════════════════════════════════════════

class TestExpectedDuration:
    def test_zero_distance(self) -> None:
        assert SpeedProfile().expected_duration(0.0) == 0.0

    def test_default_three_km_is_about_18_minutes(self) -> None:
        """§3.6：朴素 18:00，修正后约 18:09。"""
        assert SpeedProfile().expected_duration(3000.0) == pytest.approx(1089.0, abs=10.0)

    def test_agrees_with_closed_form_within_2s(self) -> None:
        """两条路径（数值积分 vs 一阶近似）应互相印证。"""
        p = SpeedProfile()
        exact = p.expected_duration(3000.0)
        approx = closed_form_eta(3000.0, 10.0, p)
        assert exact == pytest.approx(approx, abs=2.0)

    @pytest.mark.parametrize("v", [7.0, 8.5, 10.0, 12.0, 14.0, 16.5])
    def test_agrees_with_closed_form_across_presets(self, v: float) -> None:
        p = SpeedProfile(v_bar=v)
        exact = p.expected_duration(3000.0)
        approx = closed_form_eta(3000.0, v, p)
        assert exact == pytest.approx(approx, abs=3.0)

    def test_longer_distance_takes_longer(self) -> None:
        p = SpeedProfile()
        assert p.expected_duration(5000.0) > p.expected_duration(3000.0)

    def test_faster_speed_takes_less_time(self) -> None:
        assert SpeedProfile(v_bar=14.0).expected_duration(3000.0) < \
               SpeedProfile(v_bar=10.0).expected_duration(3000.0)

    def test_step_size_does_not_change_result(self) -> None:
        """步长从 0.05 s 减半，结果变化应远小于 1 s（中点法已足够）。"""
        p = SpeedProfile()
        assert p.expected_duration(3000.0, dt=0.05) == pytest.approx(
            p.expected_duration(3000.0, dt=0.01), abs=0.5
        )


# ══════════════════════════════════════════════════════════════
#  预设与参数校验
# ══════════════════════════════════════════════════════════════

class TestPresets:
    def test_key_values(self) -> None:
        assert PRESETS["标准"] == 10.0
        assert PRESETS["贴近上限"] == 16.5

    def test_all_inside_legal_window(self) -> None:
        for name, v in PRESETS.items():
            assert 6.0 <= v <= 17.0, f"预设 {name} = {v} 落在合法窗口外"

    def test_six_presets(self) -> None:
        assert len(PRESETS) == 6


class TestValidation:
    @pytest.mark.parametrize("kwargs", [
        {"v_bar": 0.0},
        {"v_bar": -5.0},
        {"r0": 0.0},
        {"r0": 1.5},
        {"t_w": -1.0},
        {"finish_frac": 1.0},
        {"finish_frac": -0.1},
    ])
    def test_rejects_bad_params(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            SpeedProfile(**kwargs)
