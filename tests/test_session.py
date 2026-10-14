"""test_session.py — 会话状态机与跨线程边界

**不需要 iPhone。** 全部用假 bridge。

为什么这些测试值钱
    会话是**18 分钟**的真实占用（works.md §2.4）。真机上试错一轮就是
    18 分钟起步，而且错了还很难定位。所以状态机、暂停语义、
    线程与事件循环的边界，必须在**秒级**的离线测试里钉死。

★ 重点覆盖的三件事

    1. **时序**（works.md §4.3.2）：预置起点 → 静置 → 推进 → 停。
    2. **锁**（§5.7）：会话进行中不许改路线 / 改参数 ——
       改了弧长积分就断裂，轨迹会出现不连续跳变。
    3. **背压**：`on_sample` 必须**等注入完成**才返回。
       这是 :mod:`vrun.session` 最要紧的一行代码，错了轨迹就和真实时间脱节。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from vrun.noise import GPSNoise
from vrun.profile import SpeedProfile
from vrun.route import Route
from vrun.safety import SafetyGuard
from vrun.session import Phase, RunSession, SessionError


@pytest.fixture(autouse=True)
def _unarm_after_each_test(monkeypatch):
    """★ 每条用例结束后，把**所有**装过保险的 guard 拆掉。

    为什么必须有（2026-10-14 实测踩到）：
        :meth:`SafetyGuard.arm` 注册的是**进程级**的 ``atexit`` 与 signal 处理器。
        它们不随对象消亡 —— 于是任何 arm 过却没清理的用例，都会在
        **解释器退出时**真的触发第②条保险：起一个真子进程去连真机、跑清理。
        十几条用例叠起来就是几十秒的挂起，而且**报错点在测试完全跑完之后**，
        看起来就像「pytest 本身卡了」。

        正常流程里保险本来就该留到最后，所以这个「泄漏」不是 bug；
        但测试里必须显式收摊。
    """
    armed: list[SafetyGuard] = []
    original = SafetyGuard.arm

    def tracking_arm(self, *a, **kw):
        result = original(self, *a, **kw)
        if self._armed:
            armed.append(self)
        return result

    monkeypatch.setattr(SafetyGuard, "arm", tracking_arm)
    yield
    for guard in armed:
        guard._restore_handlers()

# 江安校区附近一个小方环（与 conftest 的参考点一致）
LAT0, LON0 = 30.5535, 103.9910


class FakeBridge:
    """设备层的假替身。记录每一次注入，并可人为制造延迟 / 失败。"""

    def __init__(self, inject_delay: float = 0.0, fail_after: int = -1) -> None:
        self.inject_delay = inject_delay      # 模拟 DVT 往返耗时
        self.fail_after = fail_after          # 注入第 N 次之后开始失败（-1 = 不失败）
        self.set_calls: list[tuple[float, float]] = []
        self.clear_calls = 0
        self.closed = False
        self.set_timeout = 2.0
        self.set_retries = 2

    async def connect(self):
        return None

    async def set_location(self, lat, lon, **kw):
        if self.inject_delay:
            await asyncio.sleep(self.inject_delay)
        if 0 <= self.fail_after < len(self.set_calls):
            raise RuntimeError("假注入失败")
        self.set_calls.append((lat, lon))

    async def clear(self) -> bool:
        self.clear_calls += 1
        return True

    async def aclose(self) -> None:
        self.closed = True


def make_session(bridge, **kw) -> tuple[RunSession, list]:
    events: list = []

    async def emit(name: str, payload: dict) -> None:
        events.append((name, payload))

    return RunSession(bridge=bridge, emit=emit, **kw), events


def fast_profile() -> SpeedProfile:
    """秒级跑完的剖面：高速 + **零起步 ramp**。

    默认 ``t_w=30`` 意味着前 30 秒都在爬坡 —— 对测状态机毫无意义，
    却会让每条用例都变成 30 秒起步。这里把它压到 0。
    """
    return SpeedProfile(v_bar=17.0, t_w=0.0, sigma_v=0.0, clamp_enabled=False)


#: 秒级跑完的测试路线：**放弃物理尺度**，只测状态机。
#:
#: ⚠️ 踩过的坑：最初用 0.0004°（≈45 m 见方、周长 180 m）的环，
#:    按 10 km/h 算要跑 **65 秒**，十几条用例叠加直接把套件卡死。
#:    周长 ≈ 8.8 m / 17 km/h ≈ 1.9 s，正好够采样又不拖时间。
def small_route() -> Route:
    d = 0.00002                     # ≈ 2.2 m 见方，周长约 8.8 m
    return Route([
        (LAT0, LON0), (LAT0 + d, LON0),
        (LAT0 + d, LON0 + d), (LAT0, LON0 + d), (LAT0, LON0),
    ])


# ══════════════════════════════════════════════════════════════
#  ① 时序
# ══════════════════════════════════════════════════════════════

async def test_full_session_flow():
    """预置 → 开始 → 跑完 → 停止，状态迁移必须正确。"""
    bridge = FakeBridge()
    sess, events = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    assert sess.phase == Phase.PREPARED
    assert len(bridge.set_calls) == 1, "预置应恰好注入 1 个点"

    await sess.start()
    assert sess.phase in (Phase.RUNNING, Phase.STOPPED)
    await sess._wait_task()

    assert sess.phase == Phase.STOPPED
    assert sess.stats is not None
    assert sess.stats.completed is True, "应跑完整条路线"
    assert sess.stats.samples > 1

    # 事件序列：必须有 tick（否则 GUI 看不到任何反馈）
    names = [n for n, _ in events]
    assert "tick" in names
    assert names[0] == "device"        # prepare 先发 device
    assert names[-1] == "progress"     # 收尾必须是 progress（GUI 靠它解锁）


async def test_prepare_injects_only_the_start_point():
    """★ 预置**只**注入起点，绝不能顺手多注几个点。

    静置的意义是让「瞬移到起点」这个跳变发生在 App 计时窗口**之外**；
    多注入就等于把跳变摊进会话里了（works.md §4.3.2）。
    """
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    assert len(bridge.set_calls) == 1
    assert abs(bridge.set_calls[0][0] - LAT0) < 1e-9


async def test_start_requires_prepare():
    """没预置就开跑必须报错 —— 用户连点两次按钮就会走到这里。"""
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)
    with pytest.raises(SessionError, match="prepare"):
        await sess.start()


async def test_cleanup_clears_location():
    """清理必须真的调 clear —— 这是安全红线。"""
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)
    res = await sess.cleanup()
    assert res["cleared"] is True
    assert bridge.clear_calls == 1
    assert bridge.closed is True
    assert sess.phase == Phase.CLEANED


# ══════════════════════════════════════════════════════════════
#  ② 锁（works.md §5.7）
# ══════════════════════════════════════════════════════════════

async def test_route_locked_during_session():
    """会话进行中改路线必须被拒 —— 弧长积分会断裂。"""
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    assert sess.slider_locked is True
    with pytest.raises(SessionError, match="路线"):
        sess.set_route([(LAT0, LON0), (LAT0 + 0.001, LON0)])
    await sess.cleanup()


async def test_params_locked_during_session():
    """会话进行中改速度/采样率必须被拒。"""
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    assert sess.editable is False
    with pytest.raises(SessionError, match="参数"):
        sess.configure(sample_rate=5.0)
    await sess.cleanup()


async def test_editable_after_cleanup():
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)
    await sess.cleanup()
    assert sess.editable is True
    sess.set_route([(LAT0, LON0), (LAT0 + 0.001, LON0)])     # 不该抛


# ══════════════════════════════════════════════════════════════
#  ③ 背压 ★ 最要紧的一行
# ══════════════════════════════════════════════════════════════

async def test_on_sample_waits_for_injection():
    """★ ``on_sample`` 必须**等注入完成**才返回。

    没有背压的话，引擎会在注入前面越跑越远，轨迹与真实时间彻底脱节
    （而且 ``v_actual`` 会被算错 —— 它靠相邻两点的真实间隔做差分）。
    """
    bridge = FakeBridge(inject_delay=0.05)
    sess, events = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    ticks = [p for n, p in events if n == "tick"]
    # 注入次数 = prepare 的那 1 次 + 运行期的每个采样点。
    # ⚠️ 少算了 prepare 那次就会差 1 —— 而这正是「漏调 set_location」的信号，
    #    所以这里用 +1 的精确式子钉死，而不是 >=。
    assert len(ticks) == sess.stats.samples
    assert len(bridge.set_calls) == sess.stats.samples + 1


async def test_backpressure_serializes_writes():
    """★ 注入是串行的：不允许两个注入重叠。

    DTX 通道是单条串行流（DeviceBridge 里有锁），这里验证会话层
    不会因为工作线程而绕开这个保证。
    """
    bridge = FakeBridge(inject_delay=0.01)
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    assert len(bridge.set_calls) == sess.stats.samples + 1

    # 背压的直接证据：每个 tick 发出时，该点的注入**已经完成**。
    # 所以注入点数必须恰好等于采样点数（+1 是 prepare），一个不多一个不少。
    assert sess.stats.samples == len(sess.samples)


# ══════════════════════════════════════════════════════════════
#  ④ 暂停 / 停止
# ══════════════════════════════════════════════════════════════

async def test_pause_stops_injecting_then_resume_continues():
    """★ 暂停期间**不再注入**，恢复后接着跑。

    这是「暂停」在安全上的意义：暂停时手机定位会停在原地，
    而校园 App 会看到速度掉到 0 —— 用户应该清楚这一点。
    """
    bridge = FakeBridge(inject_delay=0.01)
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=5.0),
                   sample_rate=5.0)

    await sess.prepare()
    await sess.start()
    await asyncio.sleep(0.15)
    await sess.pause()

    n_at_pause = len(bridge.set_calls)
    await asyncio.sleep(0.25)
    n_after_wait = len(bridge.set_calls)
    assert n_after_wait == n_at_pause, f"暂停期间仍在注入：{n_at_pause} → {n_after_wait}"

    await sess.resume()
    await sess._wait_task()
    assert len(bridge.set_calls) > n_at_pause, "恢复后没有继续注入"


async def test_stop_ends_the_run():
    """停止要真的结束引擎线程，而不是挂在那儿。"""
    bridge = FakeBridge(inject_delay=0.01)
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=5.0),
                   sample_rate=5.0)

    await sess.prepare()
    await sess.start()
    await asyncio.sleep(0.1)
    res = await sess.stop()

    assert res["phase"] == Phase.STOPPED
    n = len(bridge.set_calls)
    await asyncio.sleep(0.15)
    assert len(bridge.set_calls) == n, "停止后仍在注入"


async def test_stop_does_not_clear():
    """★ 停止 ≠ 清理。停止后手机应停在终点，供用户核对记录。"""
    bridge = FakeBridge(inject_delay=0.01)
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=5.0),
                   sample_rate=5.0)

    await sess.prepare()
    await sess.start()
    await asyncio.sleep(0.1)
    await sess.stop()
    assert bridge.clear_calls == 0, "stop 不该清位置"

    await sess.cleanup()
    assert bridge.clear_calls == 1


async def test_pause_requires_running():
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)
    with pytest.raises(SessionError, match="暂停"):
        await sess.pause()


async def test_resume_requires_paused():
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)
    with pytest.raises(SessionError, match="恢复"):
        await sess.resume()


# ══════════════════════════════════════════════════════════════
#  ⑤ 参数校验
# ══════════════════════════════════════════════════════════════

def test_route_needs_two_points():
    sess, _ = make_session(FakeBridge())
    with pytest.raises(SessionError, match="2 个点"):
        sess.set_route([(LAT0, LON0)])


def test_empty_route_rejected():
    sess, _ = make_session(FakeBridge())
    with pytest.raises(SessionError):
        sess.set_route([])


def test_non_positive_sample_rate_rejected():
    sess, _ = make_session(FakeBridge())
    sess.set_route([(LAT0, LON0), (LAT0 + 0.001, LON0)])
    with pytest.raises(SessionError, match="采样率"):
        sess.configure(sample_rate=0.0)


async def test_prepare_without_route_fails():
    sess, _ = make_session(FakeBridge(), settle_s=0.01)
    with pytest.raises(SessionError, match="路线"):
        await sess.prepare()


# ══════════════════════════════════════════════════════════════
#  ⑥ 失败传播
# ══════════════════════════════════════════════════════════════

async def test_injection_failure_emits_error_not_silent():
    """★ 注入中途失败必须发 ``error`` 事件，不能静默僵住。

    静默失败的表现是 GUI 永远停在「运行中」，而日志上一切正常。
    """
    bridge = FakeBridge(fail_after=2)
    sess, events = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    names = [n for n, _ in events]
    assert "error" in names, "注入失败却没有 error 事件"
    assert sess.phase == Phase.FAILED


async def test_error_event_carries_hint():
    """error 事件必须带 hint —— 要告诉用户怎么收拾残局。"""
    bridge = FakeBridge(fail_after=1)
    sess, events = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    errs = [p for n, p in events if n == "error"]
    assert errs and errs[0].get("hint"), "error 事件没有 hint"
    assert "panic-clear" in errs[0]["hint"]


async def test_emit_failure_does_not_kill_session():
    """GUI 那边的异常不该拖垮会话（但也不能静默）。"""
    bridge = FakeBridge()
    calls = []

    async def bad_emit(name: str, payload: dict) -> None:
        calls.append(name)
        raise RuntimeError("假 GUI 崩溃")

    sess = RunSession(bridge=bridge, emit=bad_emit, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()
    assert calls, "事件根本没发出去"


# ══════════════════════════════════════════════════════════════
#  ⑦ 保险必须在开跑之前装上
# ══════════════════════════════════════════════════════════════

async def test_guard_is_armed_before_the_thread_starts():
    """★ 保险必须在启动工作线程**之前**装。

    反过来的话，「引擎刚起来就崩」这个窗口里没有任何保险兜底 ——
    而那恰好是最容易崩的窗口（设备刚连上，隧道还不稳）。
    """
    bridge = FakeBridge()
    sess, _ = make_session(bridge, settle_s=0.01)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(), noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    assert sess._guard.armed is False
    await sess.prepare()
    assert sess._guard.armed is False        # 预置阶段还不跑，别急着装

    await sess.start()
    assert sess._guard.armed is True
    await sess._wait_task()
    sess._guard._restore_handlers()         # 别把 atexit 留给解释器退出


def test_two_sessions_do_not_share_state():
    """每次 make_session 都要拿到独立会话 —— 单例只存在于 ipc 层。"""
    a, _ = make_session(FakeBridge(), settle_s=0.01)
    b, _ = make_session(FakeBridge(), settle_s=0.01)
    assert a is not b
    a.set_route([(LAT0, LON0), (LAT0 + 0.001, LON0)])
    assert b.route is None