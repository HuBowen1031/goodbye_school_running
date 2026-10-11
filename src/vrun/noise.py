"""noise.py — 双层 AR(1) GPS 噪声。

职责
    works.md §3.4 的**双层一阶高斯马尔可夫过程**（慢漂移 + 快抖动），
    东 / 北各一路独立分量；可选跳点注入。

为什么不用白噪声
    白噪声产生的轨迹是锯齿状毛刺，一眼就假。真实 GPS 误差在时间上是**强相关**的
    —— 表现为缓慢游走的平滑偏移，这正是 AR(1) 的形状。

依赖
    ``numpy``（① 纯计算层里唯一使用 numpy 的文件）。

⚠️ 使用纪律
    只允许 ``np.random.default_rng(seed)``，**禁止** ``np.random.seed()`` 与全局
    ``np.random.*``。全局状态会被任何第三方库的随机调用污染，直接摧毁
    「同种子 ⇒ 逐字节可复现」这条验收标准。
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np


class AR1:
    """一阶自回归（高斯马尔可夫）过程。

    递推::

        n_{k+1} = ρ · n_k + √(1 − ρ²) · σ · ε ,    ρ = exp(−Δt / τ)

    ``√(1−ρ²)`` 这个系数是为了让**稳态标准差恰为 σ**（而非随 Δt 漂移），
    自相关时间常数为 τ。
    """

    __slots__ = ("sigma", "tau", "dt", "_rng", "_rho", "_scale", "_x")

    def __init__(
        self,
        sigma: float,
        tau: float,
        dt: float,
        rng: np.random.Generator,
    ) -> None:
        self.sigma = float(sigma)
        self.tau = float(tau)
        self.dt = float(dt)
        self._rng = rng
        self._x = 0.0
        self._refresh()

    def _refresh(self) -> None:
        if self.tau <= 0.0 or self.dt <= 0.0:
            self._rho = 0.0
            self._scale = self.sigma
            return
        self._rho = math.exp(-self.dt / self.tau)
        self._scale = math.sqrt(max(0.0, 1.0 - self._rho * self._rho)) * self.sigma

    def resample(self, dt: float) -> None:
        """改变采样间隔时调用。

        ρ 必须按**新的** Δt 重算，不能缓存成常量：5 Hz 与 0.5 Hz 的 ρ 相差一个
        量级，缓存会让「切换采样率」这个动作在数值上失真（works.md §3.4）。
        """
        self.dt = float(dt)
        self._refresh()

    def step(self) -> float:
        """推进一步，返回当前值。"""
        self._x = self._rho * self._x + self._scale * float(self._rng.standard_normal())
        return self._x

    def reset(self) -> None:
        """回到初始状态（x = 0），保留 rng 的当前位置。

        注意：要让整段轨迹完全复现，应重建 rng —— 见 :meth:`GPSNoise.reset`。
        """
        self._x = 0.0


class GPSNoise:
    """双层 AR(1) 噪声发生器。

    +----------+--------------------------------------+---------+--------+
    | 分量     | 物理来源                             | σ       | τ      |
    +==========+======================================+=========+========+
    | 慢漂移   | 卫星几何 / 电离层 / 多径长期项       | 3.0 m   | 300 s  |
    | 快抖动   | 接收机热噪声                         | 0.3 m   | 1.5 s  |
    | 跳点(可选)| 隧道 / 高架 / 城市峡谷              | 8–25 m  | 2 s    |
    +----------+--------------------------------------+---------+--------+

    ``LEVELS`` 的键是**英文**（``off`` / ``realistic`` / ``harsh``），对应
    works.md §3.4 的三个中文档位（关 / 真实 / 严苛）。映射表只在这里维护，
    GUI 只传英文键 —— 避免中文字符串同时散落在 Python 与 Swift 两侧。
    """

    LEVELS: Final[dict[str, float]] = {"off": 0.0, "realistic": 1.0, "harsh": 1.5}

    SIGMA_SLOW: Final[float] = 3.0
    TAU_SLOW: Final[float] = 300.0
    SIGMA_FAST: Final[float] = 0.3
    TAU_FAST: Final[float] = 1.5

    SPIKE_RATE_PER_S: Final[float] = 1.0 / 180.0
    SPIKE_MIN_M: Final[float] = 8.0
    SPIKE_MAX_M: Final[float] = 25.0
    SPIKE_TAU_S: Final[float] = 2.0

    def __init__(
        self,
        level: str = "realistic",
        sample_rate: float = 1.0,
        seed: int = 0,
        enable_spikes: bool = False,
    ) -> None:
        if level not in self.LEVELS:
            raise ValueError(
                f"未知噪声档位 {level!r}，可选：{sorted(self.LEVELS)}"
            )
        if sample_rate <= 0.0:
            raise ValueError(f"sample_rate 必须为正，收到 {sample_rate}")

        self.level = level
        self.sample_rate = float(sample_rate)
        self.seed = int(seed)
        self.enable_spikes = bool(enable_spikes)
        self.dt = 1.0 / self.sample_rate
        self._scale = self.LEVELS[level]

        # 先声明，再由 reset() 填充 —— 保证「构造」与「重置」走同一条路径，
        # 不会出现两条初始化逻辑悄悄分叉。
        self._rng: np.random.Generator = np.random.default_rng(seed)
        self._slow_e: AR1
        self._slow_n: AR1
        self._fast_e: AR1
        self._fast_n: AR1
        self._spike_e = 0.0
        self._spike_n = 0.0
        self.reset()

    # ── 生命周期 ──────────────────────────────────────────────

    def reset(self) -> None:
        """回到初始状态：**重建 rng** 并清零所有状态。

        重建 rng 是必需的 —— 只把 x 置零而不重置随机流，第二次跑出来的轨迹
        与第一次不同，「同种子可复现」就会失效。
        """
        self._rng = np.random.default_rng(self.seed)
        s = self._scale
        self._slow_e = AR1(self.SIGMA_SLOW * s, self.TAU_SLOW, self.dt, self._rng)
        self._slow_n = AR1(self.SIGMA_SLOW * s, self.TAU_SLOW, self.dt, self._rng)
        self._fast_e = AR1(self.SIGMA_FAST * s, self.TAU_FAST, self.dt, self._rng)
        self._fast_n = AR1(self.SIGMA_FAST * s, self.TAU_FAST, self.dt, self._rng)
        self._spike_e = 0.0
        self._spike_n = 0.0

    # ── 采样 ──────────────────────────────────────────────────

    def sample(self) -> tuple[float, float]:
        """返回本次采样的 ``(de, dn)``，单位米（东向、北向偏移）。"""
        if self._scale == 0.0 and not self.enable_spikes:
            # 「关」档短路：输出恒为零，也省掉无意义的随机数消耗
            return (0.0, 0.0)

        de = self._slow_e.step() + self._fast_e.step()
        dn = self._slow_n.step() + self._fast_n.step()

        if self.enable_spikes:
            de += self._spike_step("e")
            dn += self._spike_step("n")

        return (de, dn)

    def _spike_step(self, axis: str) -> float:
        """跳点：以 ``SPIKE_RATE_PER_S · Δt`` 的概率触发，之后按 τ 指数衰减。"""
        attr = "_spike_e" if axis == "e" else "_spike_n"
        val = getattr(self, attr)

        if self._rng.random() < self.SPIKE_RATE_PER_S * self.dt:
            mag = float(self._rng.uniform(self.SPIKE_MIN_M, self.SPIKE_MAX_M))
            val += mag if self._rng.random() < 0.5 else -mag

        val *= math.exp(-self.dt / self.SPIKE_TAU_S)
        setattr(self, attr, val)
        return val

    # ── 便捷构造 ──────────────────────────────────────────────

    @classmethod
    def from_dict(cls, cfg: dict) -> "GPSNoise":
        """从 IPC 字典构造（GUI 传来的参数）。缺字段走默认值。"""
        return cls(
            level=cfg.get("level", "realistic"),
            sample_rate=float(cfg.get("sample_rate", 1.0)),
            seed=int(cfg.get("seed", 0)),
            enable_spikes=bool(cfg.get("enable_spikes", False)),
        )


# 速度波动的随机流偏移量：与位置噪声从同一人工种子派生，但**错开**一条独立的流。
# 取质数且远离位置噪声的消耗量，避免两条流在很长序列后重叠。
_SPEED_JITTER_SEED_OFFSET: Final[int] = 1_000_003


class SpeedJitter:
    """速度波动 η(t) —— 呼吸 / 路况引起的速度起伏（works.md §3.2）。

    为什么单独一个类
        位置噪声（:class:`GPSNoise`）描述的是**测量误差**，
        η 描述的是**跑者自己速度的自然起伏** —— 物理来源不同。
        把两者混在一条随机流里，会让「换个噪声档位」意外改变速度剖面，
        也会让 §3.5 的表观抖动公式失去清晰的口径。

    可复现性
        与位置噪声共享同一个**人工种子**（经偏移派生），所以「同种子 ⇒ 同轨迹」
        依然成立；但两条流互不干扰。
    """

    def __init__(
        self,
        sigma_kmh: float = 0.35,
        tau_s: float = 10.0,
        dt: float = 1.0,
        seed: int = 0,
    ) -> None:
        self.sigma_kmh = float(sigma_kmh)
        self.tau_s = float(tau_s)
        self.dt = float(dt)
        self.seed = int(seed)
        self._ar1 = self._build()

    def _build(self) -> AR1:
        rng = np.random.default_rng(self.seed + _SPEED_JITTER_SEED_OFFSET)
        return AR1(self.sigma_kmh, self.tau_s, self.dt, rng)

    def step(self) -> float:
        """推进一步，返回本采样的 η（km/h）。"""
        return self._ar1.step()

    def reset(self) -> None:
        """重建内部随机流，使同一对象能重复生成同一条 η 序列。"""
        self._ar1 = self._build()
