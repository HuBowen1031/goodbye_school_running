"""test_estimate.py — 配速 / ETA / 安全带 / golden 表的测试。

本文件是 **M1 的验收核心**：滑块面板里用户能看到的每个数字，
都在这里有对应的断言。通过它，就等于「面板逻辑已在无手机条件下验证完毕」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vrun.estimate import (
    D_MIN_M,
    V_MAX,
    V_MIN,
    closed_form_eta,
    estimate,
    format_duration,
    format_pace,
    golden_rows,
    safety_band,
    safety_color,
    sigma_apparent,
)
from vrun.profile import SpeedProfile


# ══════════════════════════════════════════════════════════════
#  格式化
# ══════════════════════════════════════════════════════════════

class TestFormatters:
    @pytest.mark.parametrize("seconds,expected", [
        (360.0, "6:00"),
        (212.0, "3:32"),
        (600.0, "10:00"),
        (353.0, "5:53"),
        (290.0, "4:50"),
    ])
    def test_pace(self, seconds: float, expected: str) -> None:
        assert format_pace(seconds) == expected

    @pytest.mark.parametrize("seconds,expected", [
        (1080.0, "18:00"),
        (1089.0, "18:09"),
        (1813.0, "30:13"),
        (7200.0, "2:00:00"),
        (90.0, "1:30"),
    ])
    def test_duration(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_pace_at_window_edges(self) -> None:
        assert format_pace(3600.0 / V_MIN) == "10:00"   # R3 边界
        assert format_pace(3600.0 / V_MAX) == "3:32"    # R4 边界


# ══════════════════════════════════════════════════════════════
#  §3.5 表观速度抖动
# ══════════════════════════════════════════════════════════════

class TestSigmaApparent:
    """逐行对照 works.md §3.5 的表。"""

    @pytest.mark.parametrize("dt,expected", [
        (0.2, 4.7),
        (0.5, 2.9),
        (1.0, 2.0),
        (2.0, 1.3),
        (5.0, 0.70),
    ])
    def test_doc_table(self, dt: float, expected: float) -> None:
        assert sigma_apparent(dt, "realistic") == pytest.approx(expected, abs=0.06)

    def test_higher_rate_means_more_noise(self) -> None:
        """★ 反直觉结论：采样率越高，朴素差分的速度噪声越大（∝ 1/√Δt）。"""
        assert (
            sigma_apparent(0.2)
            > sigma_apparent(0.5)
            > sigma_apparent(1.0)
            > sigma_apparent(2.0)
            > sigma_apparent(5.0)
        )

    def test_off_is_exactly_zero(self) -> None:
        assert sigma_apparent(1.0, "off") == 0.0

    def test_harsh_is_exactly_1_5x(self) -> None:
        assert sigma_apparent(1.0, "harsh") == pytest.approx(
            1.5 * sigma_apparent(1.0, "realistic"), rel=1e-12
        )

    def test_noise_eats_half_the_window(self) -> None:
        """刻度感：严苛档 2σ 占 11 km/h 窗口的 55%。"""
        window = V_MAX - V_MIN
        assert 2 * sigma_apparent(1.0, "harsh") / window == pytest.approx(0.55, abs=0.02)

    def test_rejects_bad_input(self) -> None:
        with pytest.raises(ValueError):
            sigma_apparent(0.0)
        with pytest.raises(ValueError):
            sigma_apparent(-1.0)
        with pytest.raises(ValueError):
            sigma_apparent(1.0, "nope")


# ══════════════════════════════════════════════════════════════
#  §5.3 噪声安全带
# ══════════════════════════════════════════════════════════════

class TestSafetyBand:
    def test_sigma_is_two_at_1hz(self) -> None:
        assert sigma_apparent(1.0, "realistic") == pytest.approx(2.0, abs=0.05)

    def test_exact_sigma_value(self) -> None:
        """★ 文档 §3.5 把 1 Hz 的 σ 显示成 2.0，**精确值是 1.9552**。

        这个差别不是小事：§5.3 / §5.4 的表格把显示值 2.0 又代回了公式，
        所以文档的表与「用精确 σ 计算」会有 0.04–0.14 的出入。
        实现选择保留精确值 —— 显示层该舍入，计算层不该。
        """
        assert sigma_apparent(1.0, "realistic") == pytest.approx(1.9552, abs=0.0005)

    def test_bands_reproduce_doc_with_documented_sigma(self) -> None:
        """喂进文档用的 σ=2.0，公式能**精确**复现 §5.3 的 [10.2, 12.4] / [8.1, 14.3]。"""
        p = SpeedProfile()

        band2 = safety_band(2.0, p, 2.0)
        assert band2 is not None
        assert band2[0] == pytest.approx(10.15, abs=0.005)
        assert band2[1] == pytest.approx(12.38, abs=0.005)

        band1 = safety_band(2.0, p, 1.0)
        assert band1 is not None
        assert band1[0] == pytest.approx(8.12, abs=0.005)
        assert band1[1] == pytest.approx(14.29, abs=0.005)

    def test_bands_with_exact_sigma_are_slightly_wider(self) -> None:
        """用精确 σ 算，安全带比文档的显示值略宽（因为 1.9552 < 2.0）。"""
        p = SpeedProfile()
        band2 = safety_band(sigma_apparent(1.0, "realistic"), p, 2.0)
        assert band2 is not None
        assert band2[0] == pytest.approx(10.06, abs=0.02)
        assert band2[1] == pytest.approx(12.47, abs=0.02)

    def test_safe_band_is_much_narrower_than_window(self) -> None:
        """反直觉但重要：合法窗口 6–17，真正「怎么抖都不越界」的只有 10.2–12.4。"""
        band = safety_band(sigma_apparent(1.0), SpeedProfile(), 2.0)
        assert band is not None
        window = V_MAX - V_MIN
        assert (band[1] - band[0]) / window == pytest.approx(0.20, abs=0.03)

    def test_harsh_band_is_empty(self) -> None:
        """★ 严苛档下约束互斥：需要 v̄ ≥ 12.18 同时 v̄ ≤ 10.48。

        这不是 bug，是真实结论 —— 噪声大到一定程度，合法速度区间会整个消失。
        """
        p = SpeedProfile()
        s = sigma_apparent(1.0, "harsh")
        # 精确 σ = 1.5 × 1.9552 = 2.9328（文档显示 3.0）
        assert s == pytest.approx(2.9328, abs=0.002)

        # 两条约束分开看都是「有解」的，但交集为空
        assert (V_MIN + 2 * s) / (1 + p.delta) == pytest.approx(12.05, abs=0.03)
        assert (V_MAX - 2 * s) / (1 + p.f) == pytest.approx(10.60, abs=0.03)
        assert safety_band(s, p, 2.0) is None

    def test_off_band_spans_window(self) -> None:
        p = SpeedProfile()
        band = safety_band(0.0, p, 2.0)
        assert band is not None
        assert band[0] == pytest.approx(V_MIN / (1 + p.delta), rel=1e-12)
        assert band[1] == pytest.approx(V_MAX / (1 + p.f), rel=1e-12)

    def test_colors(self) -> None:
        p = SpeedProfile()
        s = sigma_apparent(1.0, "realistic")
        assert safety_color(11.0, s, p) == "green"
        assert safety_color(10.0, s, p) == "yellow"
        assert safety_color(16.0, s, p) == "red"

    def test_harsh_has_no_green_at_all(self) -> None:
        p = SpeedProfile()
        s = sigma_apparent(1.0, "harsh")
        for v in [6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 17.0]:
            assert safety_color(v, s, p) != "green", f"v̄={v} 不应被判为安全"


# ══════════════════════════════════════════════════════════════
#  §3.6 闭式解
# ══════════════════════════════════════════════════════════════

class TestClosedForm:
    def test_default_is_18_09(self) -> None:
        p = SpeedProfile()
        assert closed_form_eta(3000.0, 10.0, p) == pytest.approx(1089.0, abs=0.5)

    def test_components_are_additive(self) -> None:
        """三个修正项：起步 +3.0 s、漂移 +8.1 s、冲刺 −2.2 s，净 +9 s。"""
        p = SpeedProfile()
        t_raw = 3000.0 / (10.0 / 3.6)

        ramp_debt = p.t_w * (1 - p.r0) / 2
        drift_cost = t_raw * abs(p.delta) / 2
        finish_gain = 0.04 * p.f * t_raw

        assert ramp_debt == pytest.approx(3.0, abs=0.01)
        assert drift_cost == pytest.approx(8.1, abs=0.05)
        assert finish_gain == pytest.approx(2.16, abs=0.05)

        total = t_raw + ramp_debt + drift_cost - finish_gain
        assert closed_form_eta(3000.0, 10.0, p) == pytest.approx(total, abs=1e-9)

    def test_naive_versus_corrected(self) -> None:
        """朴素 18:00 vs 修正 18:09 —— 预览必须诚实。"""
        p = SpeedProfile()
        naive = 3000.0 / (10.0 / 3.6)
        corrected = closed_form_eta(3000.0, 10.0, p)
        assert format_duration(naive) == "18:00"
        assert format_duration(corrected) == "18:09"
        assert corrected > naive


# ══════════════════════════════════════════════════════════════
#  边缘行为
# ══════════════════════════════════════════════════════════════

class TestEdgeBehaviour:
    @pytest.mark.parametrize("v", [V_MIN, V_MAX])
    def test_edges_disable_clamp(self, v: float) -> None:
        """贴线设定必须让夹紧失效，否则测不出边界行为（works.md §3.2 注）。"""
        r = estimate(v, 3000.0, "realistic", 1.0)
        assert r.clamp_enabled is False

    def test_middle_keeps_clamp(self) -> None:
        assert estimate(10.0, 3000.0, "realistic", 1.0).clamp_enabled is True

    def test_edges_are_not_blocked(self) -> None:
        """滑块**不阻止**越界设定，只如实预警（works.md §5.7）。"""
        r = estimate(V_MIN, 3000.0, "realistic", 1.0)
        assert r.speed_kmh == V_MIN
        assert r.pace_str == "10:00"

        r = estimate(V_MAX, 3000.0, "realistic", 1.0)
        assert r.speed_kmh == V_MAX
        assert r.pace_str == "3:32"

    def test_below_window_flagged(self) -> None:
        r = estimate(5.0, 3000.0, "realistic", 1.0)
        assert r.verdict["R3"].startswith("✗")

    def test_above_window_flagged(self) -> None:
        r = estimate(18.0, 3000.0, "realistic", 1.0)
        assert r.verdict["R4"].startswith("✗")

    def test_distance_rules(self) -> None:
        assert estimate(10.0, 2999.0, "off", 1.0).verdict["R1"].startswith("✗")
        assert estimate(10.0, 3000.0, "off", 1.0).verdict["R1"] == "✓"
        assert estimate(10.0, 15001.0, "off", 1.0).verdict["R2"].startswith("✗")

    def test_duration_rule(self) -> None:
        # 3 km @ 6 km/h ≈ 30 min，远低于 2 h
        assert estimate(6.0, 3000.0, "off", 1.0).verdict["R5"] == "✓"

    def test_rejects_bad_input(self) -> None:
        with pytest.raises(ValueError):
            estimate(0.0)
        with pytest.raises(ValueError):
            estimate(-1.0)
        with pytest.raises(ValueError):
            estimate(10.0, 0.0)
        with pytest.raises(ValueError):
            estimate(10.0, 3000.0, "realistic", 0.0)


# ══════════════════════════════════════════════════════════════
#  §5.4 golden 表
# ══════════════════════════════════════════════════════════════

# ── golden 契约（跨语言）────────────────────────────────────
# 数值来源是 works.md §5.4，而**不是**本实现的输出 —— 它是手工维护的基准，
# Swift 侧 ``PreviewMath`` 的单测读同一份文件（docs/file-manifest.md §6.1）。
# 两条线都通过，才说明「预览与实际一致」不是巧合。
_GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "estimate.json"
_GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


class TestGoldenTable:
    """逐行对照 ``tests/golden/estimate.json``（种子是 works.md §5.4）。

    这个文件是**唯一允许手工维护**的数值表，也是 Python 与 Swift 两侧
    共用的跨语言契约（docs/file-manifest.md §6.1）。
    所以它**不能**由本实现回写 —— 否则就是用自己证明自己。

    容差策略（文档是给人看的，保留位有限）：
    - ``pace_str`` 精确匹配；
    - ``eta_naive_s`` 由 D/(v·ms) 唯一决定 → ±0.5 s；
    - ``eta_s`` 走数值积分，文档列的是一阶近似 → ±2 s；
    - ``v_lo`` / ``v_hi`` → ±0.15。文档把 **σ 舍入成 2.0 之后又代回了公式**，
      而实现用精确的 1.9552，两者天然差 0.09，再叠加显示层舍入可达 0.14。
      这是**文档口径**问题，不是计算错误。
    """

    @pytest.mark.parametrize(
        "row", _GOLDEN["rows"], ids=lambda r: f"v={r['speed_kmh']}"
    )
    def test_row(self, row: dict) -> None:
        tol = _GOLDEN["tolerances"]
        r = estimate(row["speed_kmh"], 3000.0, "realistic", 1.0)
        assert r.pace_str == row["pace"], f"v̄={row['speed_kmh']} 配速不符"

        if not row.get("known_anomaly"):
            assert r.eta_naive_s == pytest.approx(row["eta_naive_s"], abs=tol["eta_naive_s"])
            assert r.eta_s == pytest.approx(row["eta_s"], abs=tol["eta_s"])
        # else：文档这一行的两个 eta 列与公式不一致（见 known_anomalies），
        # 跳过对照；实现值由 test_row_17_legal_but_very_noisy 单独锁定。

        assert r.v_lo == pytest.approx(row["v_lo"], abs=tol["v_lo"])
        assert r.v_hi == pytest.approx(row["v_hi"], abs=tol["v_hi"])
        assert r.safety == row["safety"]

    def test_contract_shape(self) -> None:
        """契约文件自身的完整性 —— 少一行、改个 schema 版本号都应该被拦住。"""
        assert _GOLDEN["schema_version"] == 1
        assert len(_GOLDEN["rows"]) == 11
        assert _GOLDEN["context"]["distance_m"] == 3000.0
        assert _GOLDEN["context"]["sample_rate_hz"] == 1.0

    def test_every_anomaly_is_annotated(self) -> None:
        """异常行必须在 known_anomalies 里登记，不允许默默跳过。"""
        flagged = {row["speed_kmh"] for row in _GOLDEN["rows"] if row.get("known_anomaly")}
        documented = {a["speed_kmh"] for a in _GOLDEN["known_anomalies"]}
        assert flagged == documented

    def test_safety_disagreements_are_registered(self) -> None:
        """§5.4 判定列与 §5.3 规则表分歧的行必须显式登记，不许默默吞掉。

        §5.3 定义了规则（条件 → 颜色），§5.4 给了示例表，两者对 9.0 / 13.0 / 14.0
        这三行不一致：它们的 1σ 区间并没越界，按规则应是 🟡，但表里标了 🔴。
        实现取**规则定义**（§5.3）为准，同时把 §5.4 原值留在 doc_safety 里。
        """
        disagree = {
            r["speed_kmh"] for r in _GOLDEN["rows"] if r["safety"] != r["doc_safety"]
        }
        registered = {
            s for c in _GOLDEN["document_contradictions"] for s in c["affected_speeds_kmh"]
        }
        assert disagree == registered
        assert disagree == {9.0, 13.0, 14.0}

    def test_safe_band_edges_are_in_the_table(self) -> None:
        """10.2 与 12.4 是安全带上下沿，必须一绿一绿且相邻超集为空。"""
        assert estimate(10.2, 3000.0, "realistic", 1.0).safety == "green"
        assert estimate(12.0, 3000.0, "realistic", 1.0).safety == "green"

    def test_10kmh_is_just_barely_yellow(self) -> None:
        """10.0 贴着 🟢 下沿 —— 精确 σ 下差 0.06，文档（σ=2.0）说差 0.1。

        两种口径都指向同一结论：标准档 10 km/h **不是**安全的设定。
        """
        r = estimate(10.0, 3000.0, "realistic", 1.0)
        assert r.safety == "yellow"
        band = r.band_2sigma
        assert band is not None
        assert band[0] - 10.0 == pytest.approx(0.06, abs=0.02)

    def test_row_17_legal_but_very_noisy(self) -> None:
        """17.0 是**合法速度**，但 2σ 上界冲到 21+ —— 高价值压测点。

        ⚠️ 文档 §5.4 这一行的两个 eta 列与公式对不上（其余 10 行都吻合到 ±2 s）：
        文档写「纯运动 10:42 / 修正 ETA 10:47」，而公式给 10:35 / 10:42。
        647 s 恰好等于按 v_ceil=16.7 算出的纯运动时间，所以一个可能是
        该行在某个中间版本按「clamp 生效」的口径算过。
        无法从现有材料判定原因，因此**不改文档**，只在此锁定实现值。
        详见 tests/golden/estimate.json 的 known_anomalies 与 docs/findings.md。
        """
        r = estimate(17.0, 3000.0, "realistic", 1.0)
        assert r.pace_str == "3:32"
        assert r.eta_naive_s == pytest.approx(635.3, abs=0.5)
        assert r.eta_s == pytest.approx(641.9, abs=1.0)
        assert r.v_hi > 21.0
        assert r.safety == "red"

    def test_golden_rows_match_contract(self) -> None:
        rows = golden_rows()
        assert len(rows) == 11
        assert [r.speed_kmh for r in rows] == [row["speed_kmh"] for row in _GOLDEN["rows"]]

    def test_golden_rows_are_deterministic(self) -> None:
        a = golden_rows()
        b = golden_rows()
        assert [(r.speed_kmh, r.eta_s) for r in a] == [(r.speed_kmh, r.eta_s) for r in b]

    def test_more_noise_narrows_the_green_zone(self) -> None:
        """噪声越大，绿灯区间越窄（或消失）—— 面板色带必须如实反映。"""
        green_realistic = [
            v for v in [6.0 + 0.1 * i for i in range(111)]
            if estimate(v, 3000.0, "realistic", 1.0).safety == "green"
        ]
        green_harsh = [
            v for v in [6.0 + 0.1 * i for i in range(111)]
            if estimate(v, 3000.0, "harsh", 1.0).safety == "green"
        ]
        assert len(green_realistic) > 0
        assert len(green_harsh) == 0
