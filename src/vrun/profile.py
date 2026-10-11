"""profile.py — 速度剖面（单一真相源）★

职责
    works.md §3.2 的四段叠加剖面、§3.3 预设档位、硬夹紧、精确 ETA 数值积分。

为什么是 ★
    ``engine.py`` 与 ``estimate.py`` **都 import 这一个类**。
    这是防「预览说 18:09、实际跑 18:40」这类事故的**结构性**保证（风险 R-8）。

    ⚠️ 禁止在本文件之外出现第二份速度公式（works.md §11）。

模型
    ``v(t) = clamp( v̄ · ramp(t) · drift(t) · finish(t) + η(t), v_floor, v_ceil )``

    +--------+--------------------------------------------------------+
    | ramp   | 起步加速：前 t_w 秒从 r₀ 线性升到 1                    |
    | drift  | 疲劳漂移：全程线性，δ = −1.5%                          |
    | finish | 终点冲刺：最后 finish_frac 段距离线性抬升 f = +5%      |
    | η      | 呼吸/路况波动，AR(1) 相关噪声（由 engine 侧注入）      |
    +--------+--------------------------------------------------------+

依赖
    无第三方（纯标量运算）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# 滑块的吸附点 / 预设档位（works.md §3.3）。键是中文是因为它直接显示在 GUI 上，
# 且只在 Python 侧定义一次；Swift 侧通过 JSON 读取，不复制这份表。
PRESETS: Final[dict[str, float]] = {
    "慢跑/快走": 7.0,
    "常规慢跑": 8.5,
    "标准": 10.0,
    "进阶": 12.0,
    "快跑": 14.0,
    "贴近上限": 16.5,
}

KMH_TO_MS: Final[float] = 1.0 / 3.6


@dataclass
class SpeedProfile:
    """跑步速度剖面。默认值即 works.md §3.2 的「标准」设定。"""

    v_bar: float = 10.0          # 目标稳态速度 km/h（由滑块给出）
    t_w: float = 30.0            # 起步 ramp 时长，秒
    r0: float = 0.8              # 起步初速比（0.8 → 从 8 km/h 起）
    delta: float = -0.015        # 疲劳漂移（全程线性；正号表示越跑越快）
    finish_frac: float = 0.08    # 终点冲刺区段长度（占总距离比例）
    f: float = 0.05              # 冲刺幅度
    v_floor: float = 6.3         # 硬夹紧下界（留 0.3 余量防噪声顶出窗口）
    v_ceil: float = 16.7         # 硬夹紧上界
    clamp_enabled: bool = True   # 贴线设定（6.0 / 17.0）时必须置 False

    # η(t)：呼吸 / 路况引起的**速度**波动（works.md §3.2），与 GPS 的**位置**
    # 噪声物理来源不同，因此由 engine 用独立随机流生成后经 ``eta`` 参数注入。
    sigma_v: float = 0.35        # η 的标准差，km/h
    tau_v: float = 10.0          # η 的相关时间，秒

    def __post_init__(self) -> None:
        if self.v_bar <= 0.0:
            raise ValueError(f"v_bar 必须为正，收到 {self.v_bar}")
        if not (0.0 < self.r0 <= 1.0):
            raise ValueError(f"r0 必须在 (0, 1] 内，收到 {self.r0}")
        if self.t_w < 0.0:
            raise ValueError(f"t_w 不能为负，收到 {self.t_w}")
        if not (0.0 <= self.finish_frac < 1.0):
            raise ValueError(f"finish_frac 必须在 [0, 1) 内，收到 {self.finish_frac}")

    # ── 剖面 ──────────────────────────────────────────────────

    def ramp(self, t: float) -> float:
        """起步加速系数。``t ≥ t_w`` 后恒为 1。

        ``r0 = 0.8`` 是刻意选的：起跑瞬间不会掉到 6 km/h 以下，
        避免「为了真实反而触发过慢判定」（works.md §3.2 注）。
        """
        if self.t_w <= 0.0 or t >= self.t_w:
            return 1.0
        if t <= 0.0:
            return self.r0
        return self.r0 + (1.0 - self.r0) * (t / self.t_w)

    def reference_duration(self, distance_m: float) -> float:
        """纯运动参考时长（秒），用于把疲劳漂移归一化到「全程」。

        自洽性问题：drift 依赖总时长，总时长又依赖速度。这里用纯运动时长
        ``D / v̄`` 作为参考 —— 它带来的偏差是二阶小量（< 0.02%），
        远小于一阶近似本身的误差。
        """
        if self.v_bar <= 0.0:
            return 0.0
        return distance_m / (self.v_bar * KMH_TO_MS)

    def drift(self, t: float, distance_m: float) -> float:
        """疲劳漂移系数：从 1 线性变到 ``1 + delta``。"""
        t_ref = self.reference_duration(distance_m)
        if t_ref <= 0.0:
            return 1.0
        return 1.0 + self.delta * (t / t_ref)

    def finish(self, s: float, distance_m: float) -> float:
        """终点冲刺系数：最后 ``finish_frac`` 段距离内线性抬升到 ``1 + f``。

        注意它按**距离** ``s`` 触发而非时间 —— 冲刺是「看到终点才开始」的行为。
        """
        if self.finish_frac <= 0.0 or distance_m <= 0.0:
            return 1.0
        s_fin = (1.0 - self.finish_frac) * distance_m
        if s <= s_fin:
            return 1.0
        span = self.finish_frac * distance_m
        frac = (s - s_fin) / span if span > 0.0 else 1.0
        if frac > 1.0:
            frac = 1.0
        return 1.0 + self.f * frac

    def speed_at(self, t: float, s: float, total: float, eta: float = 0.0) -> float:
        """``t`` 已用时(s)、``s`` 已走弧长(m)、``total`` 总弧长(m) → 目标速度 km/h。

        ``eta`` 是呼吸/路况波动（km/h），由 engine 从 :mod:`vrun.noise` 取来注入。
        默认 0 ⇒ 本函数是**确定性**的，可直接用于数值积分。
        """
        v = self.v_bar * self.ramp(t) * self.drift(t, total) * self.finish(s, total) + eta
        if self.clamp_enabled:
            if v < self.v_floor:
                v = self.v_floor
            elif v > self.v_ceil:
                v = self.v_ceil
        return v

    # ── 精确 ETA ──────────────────────────────────────────────

    def expected_duration(self, distance_m: float, dt: float = 0.05) -> float:
        """对完整剖面数值积分，得到走完全程所需秒数（**精确 ETA**）。

        用**中点法**（二阶）而非朴素欧拉：步长 0.05 s 时欧拉的单步误差会在
        上千步上累积到秒级，中点法把这一步误差降到可以忽略。

        这是 §3.6 那个闭式解的**权威版本**；闭式解只供 GUI 拖动时做即时反馈
        （works.md §5.6）。
        """
        if distance_m <= 0.0:
            return 0.0

        t = 0.0
        s = 0.0
        max_steps = int(1e7)  # 兜底：防止参数异常导致死循环
        for _ in range(max_steps):
            if s >= distance_m:
                return t
            # 中点法：先用当前速度预估半步位置，再取两端速度的均值推进
            v_a = self.speed_at(t, s, distance_m)
            s_mid = s + (v_a * KMH_TO_MS) * (dt * 0.5)
            v_mid = self.speed_at(t + dt * 0.5, s_mid, distance_m)
            s_next = s + (v_mid * KMH_TO_MS) * dt

            if s_next >= distance_m:
                # 最后一步越界：在段内线性插值出精确撞击时刻，避免整步过冲
                step = s_next - s
                remain = distance_m - s
                frac = remain / step if step > 0.0 else 0.0
                return t + frac * dt

            t += dt
            s = s_next
        raise RuntimeError("expected_duration 未收敛：请检查 SpeedProfile 参数")  # pragma: no cover
