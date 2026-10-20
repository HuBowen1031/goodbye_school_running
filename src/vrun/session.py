"""session.py — 会话编排 ★

职责
    把 :class:`~vrun.engine.TrackEngine` 的输出接到 :class:`~vrun.device.DeviceBridge`，
    并管理状态机：

        空闲 → 已预置 → 运行中 ⇄ 已暂停 → 已停止 → 已清理

为什么单独一个文件（而不是塞进 ``ipc.py``）
    ``ipc.py`` 的纪律是「纯编排，不含业务公式」——它只负责编解码与分发。
    会话时序、暂停语义、线程与事件循环的边界处理都是**实打实的业务编排**，
    挤进协议层会让两边都变脆。

★ 本文件最关键的设计：跨线程 / 跨事件循环的边界

    :meth:`TrackEngine.run` 是**同步阻塞**的，而一次会话是 **18 分钟**
    （works.md §2.4 的成本真相）。直接 `await engine.run()` 会把整个事件循环
    堵死 —— GUI 收不到 tick、点不了暂停、手机拔了也没反应。

    所以引擎跑在**工作线程**里，三个边界问题分别这样解决：

    ┌────────────────┬──────────────────────────────────────────────────┐
    │ 问题           │ 解法                                             │
    ├────────────────┼──────────────────────────────────────────────────┤
    │ 引擎同步、驱动 │ ``asyncio.to_thread`` 跑引擎，主循环始终可响应  │
    │ 会话很长       │                                                  │
    ├────────────────┼──────────────────────────────────────────────────┤
    │ ``on_sample``  │ ``run_coroutine_threadsafe`` 把注入提交给事件循环，│
    │ 在线程里、要   │ 再 ``.result(timeout)`` **阻塞工作线程等它回来**。│
    │ 调 async 注入  │ 这个「等」就是**背压**：注入慢了引擎就慢下来，   │
    │                │ 不会跑到前面去 —— 轨迹因此不会与真实时间脱节。  │
    ├────────────────┼──────────────────────────────────────────────────┤
    │ 暂停 / 停止    │ 引擎的 ``sleeper`` 是注入点：换成可中断的版本，│
    │ 要能打断睡眠  │ 按小片轮询标志位，暂停时挂起、停止时抛异常。   │
    └────────────────┴──────────────────────────────────────────────────┘

    ⚠️ 绝不要图省事把 ``sleeper`` 写成 ``time.sleep(gap)`` ——
       那样暂停和停止都要等到本轮睡完才生效，1 Hz 下最多迟 1 s，
       而「停止」迟 1 s 意味着多注入一个点。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .device import DeviceBridge, InjectionError
from .engine import RunStats, Sample, TrackEngine
from .noise import GPSNoise
from .profile import SpeedProfile
from .route import Route
from .safety import SafetyGuard

log = logging.getLogger(__name__)

__all__ = ["Phase", "RunSession", "SessionError"]


class SessionError(Exception):
    """会话编排层的错误。"""


class _StopRun(Exception):
    """内部信号：把引擎从 ``run()`` 里叫醒。**不是错误**，是正常停止路径。"""


class Phase:
    IDLE = "idle"
    PREPARED = "prepared"     # 已预置到起点，正在静置
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    CLEANED = "cleaned"
    FAILED = "failed"

    #: 这些状态下 GUI 必须锁住滑块（works.md §5.7）
    ACTIVE = frozenset({PREPARED, RUNNING, PAUSED})
    #: 这些状态下可以改速度 / 距离 / 噪声
    EDITABLE = frozenset({IDLE, STOPPED, CLEANED, FAILED})


@dataclass
class RunSession:
    """一次「从预置到停止」的完整会话。

    :param emit: ``emit(event_name, payload)``。**在事件循环线程上被调用**，
        所以实现方可以安全地 ``await``。事件名沿用 works.md §4.2 的协议。
    """

    bridge: DeviceBridge
    emit: Callable[[str, dict], Awaitable[None]]
    settle_s: float = 15.0

    route: Optional[Route] = None
    profile: Optional[SpeedProfile] = None
    noise: Optional[GPSNoise] = None
    sample_rate: float = 1.0

    #: 连续多少次**注入超时**之后就放弃整个会话（findings F22）。
    #:
    #: ★ 为什么要有这个阈值，而不是「一超时就崩」
    #:     2026-10 真机那轮：注入延迟 p50 7 ms / p90 8 ms / p99 60 ms /
    #:     max **2003 ms**，而超时阈值当时是 2 s —— **长尾正好卡在阈值上**。
    #:     于是一次瞬时抖动（设备当时在忙）连着 3 次超时，
    #:     整个 19 分钟的会话就崩了，前 320 秒的成绩全废。
    #:
    #:     超时是**可恢复**的：消息大概率已经发到手机了（DTX 的发送先于回执），
    #:     设备还连着。所以正确做法是**降级继续** —— 跳过一个点、如实记一笔、
    #:     继续跑完 —— 而不是让一次抖动废掉整场验收。
    #:
    #:     但也不能无限容忍：设备真的掉线时，每点都会超时，
    #:     继续跑只是在刷一串没有意义的失败。所以连续失败够多就真的失败。
    max_injection_failures: int = 5

    phase: str = Phase.IDLE
    stats: Optional[RunStats] = None
    #: 因注入超时被跳过的点数。**非零就说明轨迹上有洞**，必须能看见。
    injection_degraded: int = 0

    _guard: SafetyGuard = field(default=None, repr=False)
    _task: Optional[asyncio.Task] = None
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _samples: list[Sample] = field(default_factory=list, repr=False)
    _t0: float = 0.0
    #: 连续注入失败次数。成功一次就清零 —— 「连续」才算数，
    #: 因为隔一个成功再失败一次说明设备是活的。
    _consecutive_injection_failures: int = 0
    #: 停止**请求**。与 ``phase`` 分开是有意的 ——
    #: ``phase`` 是对外报告的状态，``stop()`` 里必须**先置这个再等线程**，
    #: 而那一刻 phase 还该保持 RUNNING 才对。合成一个字段会让「停止」失效。
    _stop_requested: bool = False

    def __post_init__(self) -> None:
        self._guard = SafetyGuard(self.bridge)

    # ── 只读状态 ────────────────────────────────────────────

    @property
    def slider_locked(self) -> bool:
        """GUI 该不该锁滑块（works.md §5.7）。"""
        return self.phase in Phase.ACTIVE

    @property
    def editable(self) -> bool:
        return self.phase in Phase.EDITABLE

    @property
    def samples(self) -> list[Sample]:
        return list(self._samples)

    # ── 配置 ────────────────────────────────────────────────

    def set_route(self, points: list) -> None:
        """``route.set``。**必须至少 2 个点**，否则无法推进。"""
        if len(points) < 2:
            raise SessionError(f"路线至少需要 2 个点，收到 {len(points)} 个")
        if not self.editable:
            raise SessionError(
                f"当前状态「{self.phase}」下不能改路线"
                "（会话进行中改路线会让弧长积分断裂，works.md §5.7）"
            )
        self.route = Route([(p[0], p[1]) for p in points])
        self._samples.clear()
        self.stats = None
        self.phase = Phase.IDLE

    def configure(
        self,
        *,
        profile: Optional[SpeedProfile] = None,
        noise: Optional[GPSNoise] = None,
        sample_rate: Optional[float] = None,
    ) -> None:
        """设置会话参数。运行中改会破坏轨迹连续性，因此一律拒绝。"""
        if not self.editable:
            raise SessionError(
                f"当前状态「{self.phase}」下不能改参数"
                "（跑到一半改速度会让弧长积分断裂，轨迹出现不连续跳变）"
            )
        if profile is not None:
            self.profile = profile
        if noise is not None:
            self.noise = noise
        if sample_rate is not None:
            if sample_rate <= 0:
                raise SessionError(f"采样率必须为正，收到 {sample_rate}")
            self.sample_rate = float(sample_rate)

    # ── 状态机 ──────────────────────────────────────────────

    async def prepare(self) -> dict:
        """``run.prepare`` —— 预置起点并静置。

        ★ 为什么要静置（works.md §4.3.2）：
          「瞬移到起点」这一次**跳变**不能落在 App 的会话窗口内。
          先把定位设到起点，等 10–20 s 让系统把位置稳定下来，
          **再**由用户在校园 App 里点开始 —— 这样那次跳变就发生在
          计时之外，不会被记成一段荒唐的位移。
        """
        if not self.editable:
            raise SessionError(f"当前状态「{self.phase}」下不能预置")

        engine = self._build_engine()
        lat, lon, _ = self.route.point_at(0.0)

        await self.bridge.set_location(lat, lon)
        self.phase = Phase.PREPARED
        await self._emit("device", phase=self.phase, prepared_at=[lat, lon])
        await self._emit("progress", **self._progress(0.0, lat, lon, engine))

        log.info("已预置到起点 %.6f,%.6f，静置 %.0f s", lat, lon, self.settle_s)
        # 静置期间用可中断的等待：用户在这个窗口里点 stop 应该立即生效
        await self._interruptible_sleep(self.settle_s)

        return {
            "phase": self.phase,
            "settled_s": self.settle_s,
            "start": [lat, lon],
            "target_m": self.route.length(),
        }

    async def start(self) -> dict:
        """``run.start`` —— 开始推进。

        引擎跑在工作线程上；本协程立刻返回，事件循环继续服务 GUI。
        """
        if self.phase != Phase.PREPARED:
            raise SessionError(
                f"必须先 run.prepare 再 run.start（当前「{self.phase}」）"
            )

        engine = self._build_engine()
        self._loop = asyncio.get_running_loop()
        self._samples.clear()
        self.stats = None
        self._t0 = time.monotonic()

        # ★ 保险必须在**开跑之前**装上。
        #   反过来的话，「引擎刚起来就崩」这个窗口里没有任何保险兜底。
        self._guard.arm()

        self.phase = Phase.RUNNING
        await self._emit("device", phase=self.phase)

        self._task = asyncio.create_task(
            asyncio.to_thread(self._run_engine_blocking, engine)
        )
        return {"phase": self.phase, "target_m": self.route.length()}

    async def pause(self) -> dict:
        if self.phase != Phase.RUNNING:
            raise SessionError(f"当前状态「{self.phase}」下不能暂停")
        self.phase = Phase.PAUSED
        await self._emit("device", phase=self.phase)
        return {"phase": self.phase}

    async def resume(self) -> dict:
        if self.phase != Phase.PAUSED:
            raise SessionError(f"当前状态「{self.phase}」下不能恢复")
        self.phase = Phase.RUNNING
        await self._emit("device", phase=self.phase)
        return {"phase": self.phase}

    async def stop(self) -> dict:
        """``run.stop`` —— 停止推进，**保持在终点**（不清理）。

        ⚠️ 停止 ≠ 清理。停止之后手机应该还停在终点，
           让用户能核对 App 的记录；彻底清除位置是 ``device.cleanup`` 的事。
        """
        if self.phase in (Phase.IDLE, Phase.STOPPED, Phase.CLEANED):
            raise SessionError(f"当前状态「{self.phase}」下无需停止")

        # ★ 必须**先发停止信号，再等线程**。
        #   反过来的话（先 _wait_task 后改 phase），引擎在 :meth:`_sleeper`
        #   里看到的仍是 RUNNING，会一直跑到路线终点 ——
        #   「停止」实际上要等整条路线跑完才生效（3 km 要等 18 分钟）。
        #   2026-10-14 的单测就是被这个卡死的。
        self._stop_requested = True
        await self._wait_task()
        self.phase = Phase.STOPPED
        await self._emit("device", phase=self.phase)
        return {"phase": self.phase, "stats": self._stats_dict()}

    async def cleanup(self) -> dict:
        """``device.cleanup`` —— 清除虚拟定位并关闭通道。

        走 :class:`SafetyGuard` 而不是直接 ``bridge.clear()`` ——
        幂等标记位是唯一真相源。
        """
        await self._wait_task()
        cleared = await self._guard.acleanup()
        await self.bridge.aclose()
        self.phase = Phase.CLEANED
        await self._emit("device", phase=self.phase, cleared=cleared)
        return {"phase": self.phase, "cleared": cleared, "stats": self._stats_dict()}

    # ── 工作线程里的部分 ────────────────────────────────────

    def _run_engine_blocking(self, engine: TrackEngine) -> None:
        """在工作线程上跑引擎。

        本方法**不碰事件循环**；跨边界一律经 ``_submit_coro``。
        任何异常都在这里被捕获并转成事件 —— **绝不让工作线程静默死掉**，
        那会让 GUI 一直停在「运行中」而没人知道出了什么事。
        """
        try:
            self.stats = engine.run(
                on_sample=self._on_sample_blocking,
                sleeper=self._sleeper,
            )
            self.phase = Phase.STOPPED
            self._submit_coro(self._emit("device", phase=self.phase))
        except _StopRun:
            self.phase = Phase.STOPPED
            self._submit_coro(self._emit("device", phase=self.phase, stopped=True))
        except Exception as exc:
            self.phase = Phase.FAILED
            log.exception("会话异常终止")
            self._submit_coro(self._emit_error("session_failed", str(exc)))
        finally:
            # 无论怎么结束都要把统计发出去 —— 否则 GUI 永远停在「运行中」
            self._submit_coro(self._emit("progress", **self._progress(
                self.stats.duration_s if self.stats else 0.0,
                *self._last_position())))

    def _on_sample_blocking(self, sample: Sample) -> None:
        """``TrackEngine`` 的回调。**跑在工作线程上。**

        ★ 这里是整个文件最要紧的一段：

        ``run_coroutine_threadsafe(...).result(timeout)`` 会**阻塞本线程**
        直到注入真正完成。注入慢了，引擎就慢下来 —— 这就是**背压**。
        没有它，引擎会在注入前面越跑越远，轨迹就和真实时间彻底脱节
        （而且 ``v_actual`` 会被算错：它靠相邻两点的真实间隔做差分）。

        ⚠️ 漏掉 :meth:`DeviceBridge.set_location` 的后果极其隐蔽：
           tick 照发、progress 照报、状态机照跑、
           统计照算 —— 但手机上**一个点都不会动**，一切看起来都正常。
           ``test_on_sample_waits_for_injection`` 钉的就是这一条。
        """
        self._samples.append(sample)

        # ① 注入。
        #    ⚠️ 这里曾经是「失败必须让整个会话失败」（works.md §4.3.4：
        #    异常恢复要明确报错，不静默失败）。但那条纪律针对的是
        #    **静默吞掉** —— 轨迹出现无声的空洞，而统计数字一切正常。
        #    现在改成**降级继续**：跳过的点会被计数、会发 ``error`` 事件、
        #    会出现在压测报告里 —— 一样「明确报错」，
        #    但不会让一次瞬时抖动废掉整场 19 分钟的验收（F22）。
        #
        #    「超时」和「其他错误」分开处理：
        #    · 超时 —— 设备多半还活着，只是这一次慢了 ⇒ 降级继续
        #    · 其他 —— 通道断了，重试没意义 ⇒ 立刻失败
        try:
            self._submit_coro(self.bridge.set_location(sample.lat, sample.lon),
                              strict=True)
        except InjectionError as exc:
            if not exc.timeout_exhausted:
                raise          # 通道断了 —— 没有降级的余地
            self._consecutive_injection_failures += 1
            self.injection_degraded += 1
            if self._consecutive_injection_failures >= self.max_injection_failures:
                log.error("连续 %d 次注入超时，放弃会话",
                          self._consecutive_injection_failures)
                raise
            log.warning("注入超时（第 %d 次连续，跳过该点）：%r",
                        self._consecutive_injection_failures, exc)
            self._submit_coro(self._emit_error(
                "injection_degraded",
                f"注入超时，已跳过该点（连续 {self._consecutive_injection_failures} 次）：{exc}",
                hint="设备多半还连着，已跳过该点继续跑；"
                     "若连续失败太多会自行终止"))
            return
        else:
            # 成功一次就清零 —— 「连续失败」才说明设备真的出问题了
            self._consecutive_injection_failures = 0
        # ② 再把状态发回 GUI —— 宽松：GUI 挂了不该拖垮会话。
        self._submit_coro(self._tick_and_progress(sample))

    # ── 跨边界：线程 ⇄ 事件循环 ────────────────────────────

    def _submit_coro(self, coro, *, strict: bool = False) -> None:
        """把协程丢回事件循环线程执行，**并等待它完成**。

        :param strict: 注入用 ``True`` —— 失败直接抛出去终止会话。
            发事件用 ``False`` —— GUI 那边的异常不该拖垮一次 18 分钟的跑动。
        """
        if self._loop is None or self._loop.is_closed():
            coro.close()
            if strict:
                raise RuntimeError("事件循环已关闭，无法注入")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=self._inject_timeout())
        except Exception as exc:
            if strict:
                raise
            log.warning("从工作线程回传事件失败：%r", exc)

    def _inject_timeout(self) -> float:
        """注入 + 发事件的超时余量。给得比单点 set 宽，因为还要回传事件。

        ⚠️ 必须**严格大于**设备层最坏情况的注入耗时
            设备层最坏 = ``set_timeout × (set_retries + 1)``（每次尝试都超时）。
            早先这里取 ``max(那个值, 10.0)``，和设备层**恰好相等** ——
            线程侧的 ``fut.result(timeout=...)`` 会在注入刚要失败时先超时，
            于是抛出来的不是 :class:`InjectionError` 而是 ``TimeoutError``，
            「超时」这个信息就丢了，降级逻辑也就无从判断。
            现在留 5 s 余量。
        """
        return max(self.bridge.set_timeout * (self.bridge.set_retries + 1) + 5.0, 15.0)

    def _sleeper(self, gap: float) -> None:
        """可中断的等待 —— :meth:`TrackEngine.run` 的 ``sleeper`` 注入点。

        暂停：挂起，标志清掉后自动继续。
        停止：抛 :class:`_StopRun`，把引擎叫醒。

        ⚠️ 必须**分小片轮询**。整段 ``time.sleep(gap)`` 会让暂停迟 1 s、
        停止迟 1 s —— 而「停止迟 1 s」意味着**多注入一个点**。
        """
        deadline = time.monotonic() + max(gap, 0.0)
        while True:
            if self._stop_requested or self.phase in (Phase.STOPPED, Phase.CLEANED):
                raise _StopRun()
            if self.phase == Phase.PAUSED:
                time.sleep(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    #: :meth:`TrackEngine.run` 的 ``sleeper`` 注入点就是上面这个方法。
    #: （刻意不取别名 —— 在 dataclass 里写 ``a = b`` 会变成一个类属性字段，
    #:   不是方法绑定。）

    async def _interruptible_sleep(self, seconds: float) -> None:
        """静置等待。用户在静置窗口里点 stop 也要能立刻生效。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.phase in (Phase.STOPPED, Phase.CLEANED):
                raise _StopRun()
            if self.phase == Phase.PAUSED:
                await asyncio.sleep(0.1)
                continue
            await asyncio.sleep(min(0.1, deadline - time.monotonic()))

    # ── 辅助 ────────────────────────────────────────────────

    def _build_engine(self) -> TrackEngine:
        if self.route is None:
            raise SessionError("还没设置路线（先发 route.set）")
        if self.profile is None:
            raise SessionError("还没设置速度剖面（先发 run.start 的 profile）")
        if self.noise is None:
            self.noise = GPSNoise(level="realistic", sample_rate=self.sample_rate, seed=0)
        return TrackEngine(self.route, self.profile, self.noise, self.sample_rate)

    def _last_position(self) -> tuple[float, float]:
        if self._samples:
            s = self._samples[-1]
            return s.lat, s.lon
        return self.route.point_at(0.0)[:2] if self.route else (0.0, 0.0)

    def _stats_dict(self) -> Optional[dict]:
        if self.stats is None:
            return None
        return {
            "duration_s": self.stats.duration_s,
            "distance_m": self.stats.distance_m,
            "mean_speed_kmh": self.stats.mean_speed_kmh,
            "samples": self.stats.samples,
            "completed": self.stats.completed,
            "injection_degraded": self.injection_degraded,
        }

    def _progress(self, elapsed: float, lat: float, lon: float,
                  engine: Optional[TrackEngine] = None) -> dict:
        return {
            "t": elapsed,
            "lat": lat,
            "lon": lon,
            "s": self._samples[-1].s if self._samples else 0.0,
            "target_m": self.route.length() if self.route else 0.0,
            "v_actual": self._samples[-1].v_actual if self._samples else 0.0,
            "v_target": self._samples[-1].v_target if self._samples else 0.0,
            "sigma_v": self._sigma(),
            "sample_rate": self.sample_rate,
        }

    def _sigma(self) -> float:
        """表观速度 1σ。与 ``estimate.sigma_apparent`` **同口径**。

        这里刻意不复制公式 —— 直接调用 ① 纯计算层。
        复制一份就多一个漂移源（works.md §4.3.5 的结构性保证）。
        """
        from .estimate import sigma_apparent

        level = self.noise.level if self.noise is not None else "realistic"
        return sigma_apparent(1.0 / max(self.sample_rate, 0.01), level)

    async def _tick_and_progress(self, sample: Sample) -> None:
        await self._emit("tick",
                         t=sample.t, lat=sample.lat, lon=sample.lon, s=sample.s,
                         v_target=sample.v_target, v_actual=sample.v_actual)
        await self._emit("progress", **self._progress(sample.t, sample.lat, sample.lon))

    async def _emit(self, name: str, **payload) -> None:
        """发一个事件。**吞掉 GUI 侧的异常** —— 它不该拖垮会话。

        但不能静默：写进日志。
        """
        try:
            await self.emit(name, payload)
        except Exception as exc:
            log.warning("事件 %s 发出失败：%r", name, exc)

    async def _emit_error(self, code: str, message: str,
                          hint: Optional[str] = None) -> None:
        """发一个 ``error`` 事件。

        :param hint: 提示语。``None`` = 用默认的「跑 panic-clear 清残留」。
            降级事件**不该**用那句 —— 位置是好的，不需要清理，
            说「请清理残留」反而会让人以为出了大事。
        """
        if hint is None:
            hint = "可执行 scripts/panic-clear.sh 清除残留的虚拟定位"
        await self._emit("error", code=code, message=message, hint=hint)

    async def _wait_task(self) -> None:
        """等引擎线程结束（带超时，绝不无限等）。"""
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("引擎线程未在 5 s 内结束，仍继续清理")
        except Exception:
            pass          # 引擎自己已经报过错了
        self._task = None

    async def wait_until_finished(self, timeout: float) -> bool:
        """等引擎线程**真的跑完**。返回是否等到了。

        ★ 与 :meth:`_wait_task` 的区别不是「多等一会儿」，而是**用途不同**

        ``_wait_task`` 是会话内部的**快路径**：调用它之前（``stop`` /
        ``cleanup``）已经先置了停止标志，引擎会在一两个采样周期内退出，
        所以 5 秒足够。它**故意不等** —— 引擎还在正常跑的时候，
        5 秒后它就返回，调用方若接着去清理，就会出现「引擎仍在注入，
        位置却被清掉」的竞态（findings **F15** 实测踩过）。

        而外部要等的是「一次 18 分钟的运行自然跑到终点」，
        这种情况下 5 秒显然不够，必须显式给一个够长的 ``timeout``。

        :param timeout: 最多等多少秒。
        :return: ``True`` = 等到了；``False`` = 超时或引擎抛异常。
        """
        if self._task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.warning("引擎线程在 %.0f s 内未结束", timeout)
            return False
        except Exception:
            return False      # 引擎自己已经报过错了