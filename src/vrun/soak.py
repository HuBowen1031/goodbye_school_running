"""soak.py —— 真机长跑压测（M4 / works.md §6 M2）

为什么要有这个文件
------------------

M2 的短测只有 6 个点、每点停 3 s。它证明的是「**能注入**」，
不是「18 分钟不漂、不漏、不卡」—— 而 M4 的 3 km 全程恰恰是 18 分钟。

works.md §6 M2 那条「实测 1 Hz 连续注入 10 分钟，统计丢失点数与**延迟分布**」
当时只落了 ``dropped`` 计数器，**延迟分布这一半没有埋点**。
本文件把两半都收齐，并产出一份可归档的 JSON 报告。

⚠️ 它**不走 GUI**
    压测要连续占着手机 10–20 分钟，而 GUI 那套「预置 → 静置 → 开始」
    需要人点按钮。自动化压测不该依赖点击，所以这里直接用
    :class:`vrun.session.RunSession` 驱动，跑完自己清理。

它测什么、不测什么
------------------

===========  ==========================================================
测           注入**稳定性**：成功率、丢失点数、每次注入的延迟分布、
             轨迹与真实时钟的贴合度
**不测**     轨迹**数学**是否正确 —— 那是 M1 的事（假时钟已验过）
===========  ==========================================================

⚠️ 两件事必须分开验
    「算得对不对」和「注得进去吗」是两种故障，混在一起验不出是谁的锅。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .device import DeviceBridge
from .estimate import closed_form_eta
from .noise import GPSNoise
from .profile import SpeedProfile
from .route import Route
from .session import RunSession, SessionError


@dataclass
class SoakResult:
    """一次压测的完整报告。字段刻意全部是标量或短列表，可直接 JSON 化。"""

    # ── 配置 ──
    duration_s: float = 0.0
    sample_rate_hz: float = 1.0
    speed_kmh: float = 10.0
    route_m: float = 0.0

    # ── 结果 ──
    wall_s: float = 0.0
    planned_samples: int = 0
    produced_samples: int = 0
    injected: int = 0
    dropped: int = 0

    # ── 延迟分布（毫秒）──
    latency: dict = field(default_factory=dict)

    # ── 轨迹与时钟的贴合度 ──
    planned_s: float = 0.0
    produced_s: float = 0.0
    #: 是否跑满了计划时长。
    #:
    #: ⚠️ **不是**「引擎是否跑到路线终点」—— 压测到点就主动停，
    #:    那是**预期**行为，拿它当判据会把每次都判成失败。
    #:    真正要问的是「计划的那 10 分钟有没有真的走完」。
    #: ★ 「跑到终点」模式（``--to-end``）。
    #:
    #: M4 的验收要的是「跑完整条 3 km 路线」，不是「跑够 N 分钟」。
    #: 两者不能互相代偿：定 18 分钟跑 3 km @ 10 km/h 会因为
    #: 路线（18:01）先跑完而被判成「没跑满」—— **假阴性**；
    #: 反过来定 20 分钟又会让引擎跑完还空转 2 分钟。
    to_end: bool = False
    #: 预置完成、可以开始操作 App 之后，是否已经通知过。
    #: ``on_prepared`` 只触发一次 —— 后续的 progress 事件都带着实时指标。
    prepared_notified: bool = False
    #: 引擎是否**跑到了路线终点**（``RunStats.completed``）
    route_completed: bool = False

    #: 闭式解预览的 ETA（秒）。``--to-end`` 模式下与实际用时对比，
    #: 判据是 works.md §6 M4 的「差 < 2 s」。
    planned_eta_s: float = 0.0
    #: 实际用时 − 预览 ETA（秒）。``None`` = 还没可比。
    eta_error_s: Optional[float] = None
    #: ETA 允许的误差上限（秒）
    eta_tolerance_s: float = 2.0

    #: 最后一个采样点的时刻（秒）。判断「跑满时长」的依据。
    covered_s: float = 0.0
    reached_duration: bool = False

    #: 清理是否真的执行了。``False`` = **手机可能还卡在注入位置上**。
    cleared: bool = False
    cleanup_error: Optional[str] = None

    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "duration_s": self.duration_s,
            "sample_rate_hz": self.sample_rate_hz,
            "speed_kmh": self.speed_kmh,
            "route_m": self.route_m,
            "wall_s": self.wall_s,
            "planned_samples": self.planned_samples,
            "produced_samples": self.produced_samples,
            "injected": self.injected,
            "dropped": self.dropped,
            "latency_ms": self.latency,
            "planned_distance_m": self.planned_s,
            "produced_distance_m": self.produced_s,
            "covered_s": self.covered_s,
            "to_end": self.to_end,
            "route_completed": self.route_completed,
            "planned_eta_s": self.planned_eta_s,
            "eta_error_s": self.eta_error_s,
            "eta_tolerance_s": self.eta_tolerance_s,
            "reached_duration": self.reached_duration,
            "cleared": self.cleared,
            "cleanup_error": self.cleanup_error,
            "error": self.error,
        }


def _dur(seconds: float) -> str:
    """秒 → ``m:ss`` / ``h:mm:ss``。与面板上的写法一致。"""
    total = int(round(seconds))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def report_lines(r: SoakResult) -> list[str]:
    """把报告渲染成人能扫的行。

    ⚠️ 两种模式的**判据不同**，绝不能混
        · 定时模式：回答「这段时间稳不稳」⇒ 红线是丢失点数 + 覆盖时长
        · 到点模式：回答「整条轨迹跑完对不对」⇒ 红线是**跑到了终点 + ETA 准**
    一份把「路线只跑了一半」显示成通过的报告，比没有报告更坏。
    """
    out: list[str] = []

    if r.to_end:
        out += [
            f"运行方式    跑到路线终点（路线 {r.route_m:.1f} m，{r.speed_kmh:g} km/h）",
            f"预览 ETA    {_dur(r.planned_eta_s)}（§3.6 闭式解，与面板同一个函数）",
        ]
    else:
        out += [
            f"计划时长    {r.duration_s:.0f} s  @ {r.sample_rate_hz:g} Hz"
            f"（{r.planned_samples} 个点）",
        ]
    out += [
        f"实际墙钟    {r.wall_s:.1f} s",
        f"产出采样    {r.produced_samples} 点",
        f"注入成功    {r.injected} 点    丢失 {r.dropped} 点",
        f"轨迹长度    计划 {r.planned_s:.1f} m / 实际 {r.produced_s:.1f} m",
        f"覆盖时长    {r.covered_s:.1f} s"
        + ("" if r.to_end else f" / 计划 {r.duration_s:.1f} s"),
    ]

    if r.to_end:
        out.append(f"跑到终点    {'是' if r.route_completed else '**否**'}")
        if r.eta_error_s is None:
            out.append("ETA 对比    —— 引擎没跑完，无从对比")
        else:
            bad = abs(r.eta_error_s) >= r.eta_tolerance_s
            out.append(
                f"实际用时    {_dur(r.covered_s)}　与预览差 {r.eta_error_s:+.2f} s"
                f"（要求 |差| < {r.eta_tolerance_s:g} s）"
                + ("  ← **超差**" if bad else "  ✓")
            )

    lat = r.latency or {}
    if lat.get("attempts"):
        def fmt(k: str) -> str:
            v = lat.get(k)
            return "—" if v is None else f"{v:.0f}"

        out.append(
            f"注入延迟    p50 {fmt('p50_ms')} / p90 {fmt('p90_ms')} / "
            f"p99 {fmt('p99_ms')} / max {fmt('max_ms')} ms"
            f"（{lat['attempts']} 次尝试"
            + (f"，{lat['timeouts']} 次超时" if lat.get("timeouts") else "")
            + "）"
        )
    else:
        out.append("注入延迟    无数据")

    out.append(f"清理        "
               + ("已确认" if r.cleared
                  else "**未确认 —— 请立刻跑 scripts/panic-clear.sh**"))
    if r.cleanup_error:
        out.append(f"清理错误    {r.cleanup_error}")
    if r.error:
        out.append(f"错误        {r.error}")

    # ── 判定 ──
    # ⚠️ 两条规则都必须逐条落到 `reasons` 上。
    #   写规则文案时漏了对应检查，报告就会「写着这条红线、实际不查」——
    #   而 `test到点模式清理未确认算红` 就是为这个存在的
    #   （第一版到点分支真的漏了 `cleared` 与 `error` 两条）。
    reasons: list[str] = []
    if r.dropped:
        reasons.append(f"丢了 {r.dropped} 点")
    if r.error:
        reasons.append("有报错")
    if not r.cleared:
        reasons.append("**清理未确认** —— 手机可能还卡在注入位置上")

    if r.to_end:
        if not r.route_completed:
            reasons.append("没跑到路线终点")
        if r.eta_error_s is None:
            reasons.append("无从比对 ETA")
        elif abs(r.eta_error_s) >= r.eta_tolerance_s:
            reasons.append(f"ETA 差 {r.eta_error_s:+.2f} s 超出 {r.eta_tolerance_s:g} s")
        ok = not reasons
        rule = "跑到终点、ETA 误差在容差内、丢失点数 = 0、无报错、清理成功"
    else:
        if not r.reached_duration:
            reasons.append("没跑满计划时长")
        ok = not reasons
        rule = "丢失点数 = 0、跑满计划时长、无报错、清理成功"

    out.append("")
    out.append(("✅ 通过" if ok else "❌ 未通过") + f" —— 红线：{rule}"
               + ("" if ok else f"（踩中：{'、'.join(reasons)}）"))
    return out



async def soak(
    bridge: DeviceBridge,
    route: Route,
    *,
    duration_s: float,
    sample_rate_hz: float = 1.0,
    speed_kmh: float = 10.0,
    noise_level: str = "realistic",
    seed: int = 0,
    to_end: bool = False,
    settle_s: float = 0.0,
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_prepared: Optional[Callable[[], None]] = None,
) -> SoakResult:
    """连着设备跑，按 ``sample_rate_hz`` 注入。

    两种终止方式（``to_end`` 切换）：

    ``False``（默认）
        跑满 ``duration_s`` 秒就停。回答的是「这段时间里稳不稳」。

    ⚠️ ``settle_s`` 默认 0，只适用于**压测**
        works.md §4.3.2 规定：预置起点后要静置 10–20 s，
        **再由用户在 App 里开始跑** ——
        这样「瞬移到起点」那一次跳变不会落在 App 的会话窗口内。
        压测时没有 App 在记录，静置没有意义，所以默认 0。
        但只要这次运行是为了让用户操作 App（``--to-end`` 的 M4 验收），
        **必须给足静置时间**，否则那一跳会被 App 记成一次荒唐的位移。
        传 ``on_prepared`` 可以在预置完成那一刻通知用户开始操作。
    ``True``
        **跑到路线终点**才停。回答的是「整条轨迹跑完对不对」——
        M4 的 3 km 验收要的是这个，而且顺带比对预览 ETA 与实际用时。

    跑完**一定**走 :meth:`RunSession.cleanup` —— 幂等标记位在
    :class:`vrun.safety.SafetyGuard` 里，自己另起一套会让
    「到底清没清掉」变得没法判断，所以清理结果原样带回报告。
    """
    rep = SoakResult(
        duration_s=duration_s,
        sample_rate_hz=sample_rate_hz,
        speed_kmh=speed_kmh,
        route_m=route.length(),
        to_end=to_end,
    )
    rep.planned_samples = int(duration_s * sample_rate_hz) + 1
    rep.planned_s = speed_kmh / 3.6 * duration_s

    ticks = 0

    async def emit(name: str, payload: dict) -> None:
        """吞掉 GUI 关心的事，但**记下点数与错误** —— 报告全靠它。

        ★ 错误必须从**事件**里接，不能靠 try/except，也不能事后查 phase：
          · 引擎的异常被 ``_run_engine_blocking`` 吞掉并转成 ``error`` 事件
            （这是有意的：不让工作线程静默死掉，否则 GUI 会一直停在「运行中」）；
          · 它把 ``phase`` 置成 ``FAILED``，可**紧接着的 ``stop()`` 又会改回
            ``STOPPED`` —— 事后再查 phase 永远查不到失败。
        """
        nonlocal ticks
        if name == "tick":
            ticks += 1
            if on_progress and ticks % 30 == 0:
                on_progress(ticks, rep.planned_samples)
        elif name == "device" and on_prepared and not rep.prepared_notified:
            # ⚠️ 判别信号是 **device** 事件，不是 progress。
            #    `prepare()` 发的是 `_emit("device", phase=..., prepared_at=[...])`；
            #    它紧接着发的那个 progress 走的是 `_progress()`，
            #    载荷里**根本没有 phase** 这个键 —— 按 progress 判会永远等不到。
            if payload.get("phase") == "prepared":
                rep.prepared_notified = True
                on_prepared()
        elif name == "error" and not rep.error:
            rep.error = (f"{payload.get('code', 'error')}: "
                         f"{payload.get('message', '')}").strip()

    # ⚠️ 刻意**不**自己造 SafetyGuard：:meth:`RunSession.start` 自己会装。
    #   两个保险同时在，其中一个的「已清理」标记会挡住另一个的真实清理 ——
    #   而「其实没清掉」恰恰是压测最不能瞒的事。
    profile = SpeedProfile(v_bar=speed_kmh)
    noise = GPSNoise(level=noise_level, seed=seed)
    # 预览 ETA 用 §3.6 的闭式解 —— 与面板上显示的是**同一个函数**，
    # 所以这里量到的差值就是「用户看到的预估准不准」。
    rep.planned_eta_s = closed_form_eta(route.length(), speed_kmh, profile)
    sess = RunSession(bridge=bridge, emit=emit, settle_s=settle_s,
                      profile=profile, noise=noise, sample_rate=sample_rate_hz)

    t0 = time.perf_counter()
    try:
        sess.set_route([(p[0], p[1]) for p in route.points])
        # 走官方路径：prepare 会把位置预置到起点并装上保险。
        # 压测要压的就是**这条真实链路**，不是我们自己拼的简化版。
        await sess.prepare()
        await sess.start()

        # 到点就停。⚠️ 靠计时器停、而不是等引擎跑到路线终点 ——
        #   否则「10 分钟」实际取决于路线长度，报告里的秒数就失去意义。
        #
        # ★ 顺序：**先等到点、再等引擎退出**，反过来会出事。
        #
        #   `RunSession._wait_task()` 内部是 `wait_for(..., timeout=5.0)`，
        #   也就是**最多等 5 秒就返回**，哪怕引擎还在正常跑。
        #   若先 `_wait_task()`，5 秒后它就返回；接着就去清理 ——
        #   而引擎线程仍在后台注入，于是「清完位置又被注回去」。
        #
        #   真机上那表现为：手机最终停在**压测途中**的某个点，
        #   而不是回到真实 GPS —— 而且报告还写着「清理已确认」。
        #
        #   `stop()` 内部已经等过一次引擎线程，所以以它为主、
        #   `_wait_task()` 只作兜底。
        if to_end:
            # 跑到终点。用 wait_until_finished 而不是 _wait_task ——
            # 后者 5 秒就返回，而引擎还在正常注入（findings F15）。
            finished = await sess.wait_until_finished(
                timeout=duration_s * 3.0 + 120.0)
            if not finished:
                rep.error = rep.error or "引擎在超时前没跑到终点"
        else:
            stopper = asyncio.create_task(_stop_after(sess, duration_s))
            try:
                await stopper          # 到点 → stop() → 内部已等过引擎线程
                await sess._wait_task()  # 再兜一次，确认线程真的退了
            finally:
                stopper.cancel()


    except Exception as exc:                      # noqa: BLE001 —— 报告要的是原文
        rep.error = f"{type(exc).__name__}: {exc}"
    finally:
        rep.wall_s = time.perf_counter() - t0
        st = bridge.status()
        rep.injected, rep.dropped = st.injected, st.dropped
        rep.latency = bridge.injection_stats().summary()
        rep.produced_samples = ticks
        stats = sess.stats
        if stats is not None:
            rep.route_completed = bool(stats.completed)
            if to_end and stats.duration_s > 0:
                rep.eta_error_s = stats.duration_s - rep.planned_eta_s
        samples = sess.samples
        if samples:
            rep.produced_s = samples[-1].s
            rep.covered_s = samples[-1].t
            # ★ 「跑满时长」按**最后一个采样点的时刻**判断，不按 await 是否返回。
            #   `_run_engine_blocking` 把引擎异常吞掉并改 phase，
            #   于是 `_wait_task()` 在会话崩掉时**照样正常返回** ——
            #   「await 回来了」不等于「跑完了」。
            #
            #   阈值取「计划时长减去一个采样周期」：定时器在 duration_s 处停，
            #   而最后一个采样点最多比它早一个周期。
            rep.reached_duration = (
                not rep.error
                and rep.covered_s >= duration_s - 1.0 / sample_rate_hz
            )
        # ⚠️ 走会话自己的 `cleanup()`，不要自己拼 `acleanup + aclose` ——
        #   幂等标记位在 SafetyGuard 里，自己另起一套会让「到底清没清掉」
        #   变得没法判断。报告里也如实记下 `cleared`。
        try:
            res = await sess.cleanup()
            rep.cleared = bool(res.get("cleared"))
        except Exception as exc:                  # noqa: BLE001
            rep.cleanup_error = f"{type(exc).__name__}: {exc}"
    return rep


async def _stop_after(sess: RunSession, seconds: float) -> None:
    await asyncio.sleep(seconds)
    try:
        await sess.stop()
    except SessionError:
        # 引擎自己先跑到终点并停下了（路线太短的情况）。
        # 这**不是**错误 —— `stop()` 只在「还在跑」时才有意义。
        # 但它必须被容忍，否则一份「提前跑完」的压测会被报成失败，
        # 而那件事本身要由 `covered_s` 来判断，不是由异常来表达。
        pass


def make_long_route(distance_m: float, origin: tuple[float, float]) -> Route:
    """一条**足够长**的折线，让引擎跑满 ``duration_s`` 之前不会到终点。

    ⚠️ 压测不该被「路线走完」打断。
       引擎跑到终点就停了，于是「10 分钟」会变成「路线时长」，
       而报告里 planned_samples 与 produced_samples 的差异会被误读成丢点。
    """
    # 周长取 2 倍余量，保证任何速度下都够跑
    side = (distance_m * 2.0) ** 0.5
    dlat = side / 111_320.0
    dlon = side / (111_320.0 * 0.87)      # 成都一带 cos(30.55°) ≈ 0.861
    lat0, lon0 = origin
    pts = [
        (lat0, lon0),
        (lat0 + dlat, lon0),
        (lat0 + dlat, lon0 + dlon),
        (lat0, lon0 + dlon),
        (lat0, lon0),
    ]
    return Route(pts).scaled_to(distance_m)


def write_report(rep: SoakResult, path: str) -> None:
    payload = rep.to_dict()
    payload["_doc"] = [
        "M4 真机长跑压测报告（vrun soak 生成）。",
        "latency_ms 记录的是**每一次尝试**，含失败与超时 ——",
        "只看成功的那批会把长尾抹掉，而长尾正是出问题的地方。",
        "",
        "判定红线（四条，缺一不可）：",
        "  dropped == 0  且  reached_duration  且  error 为空  且  cleared",
        "  cleared=False 意味着手机**可能还卡在注入位置上**，",
        "  请立刻跑 scripts/panic-clear.sh。",
        "",
        "⚠️ injected 正常会比 produced_samples 多 1：那多出来的一次是",
        "   prepare() 把位置预置到起点的那一注。它不属于推进轨迹，",
        "   但它确实真的打进了设备，所以会算进 injected。",
        "   ⚠️ covered_s 用**最后一个采样点的时刻**判定，不看墙钟 ——",
        "   墙钟只说明 sleep 睡够了，不说明采样真的发出去了。",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
