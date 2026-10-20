"""test_injection_resilience.py —— F22：注入超时不该毁掉整场验收

背景（2026-10 真机那轮踩到的）
    注入延迟分布是 p50 7 ms / p90 8 ms / p99 60 ms / **max 2003 ms**，
    而当时的超时阈值是 **2 s** —— 长尾正好卡在阈值上。
    一次瞬时抖动连着 3 次超时，``InjectionError`` 一路抛到引擎线程，
    整个 **19 分钟**的会话崩掉，前 320 秒的成绩全废。

这里钉的是三件事
    ① **超时 ≠ 失败**：超时是**可恢复**的（设备多半还连着，只是这一次慢了），
       必须能降级继续；非超时错误（通道断了）才该立刻失败。
    ② **降级必须可见**：跳过的点要被计数、要发 ``error`` 事件、
       要出现在压测报告里 —— 「明确报错」不等于「让整场跑崩」。
    ③ **容忍有上限**：设备真掉线时不能无限刷失败。

★ 为什么这些测试**不碰真机**
    真机上「超时」是偶发的（323 次尝试里 3 次），靠不住。
    要复现它只能**注入假故障** —— 把「超时」和「硬失败」这两种
    截然不同的失败分开喂进去，看会话分别怎么反应。
"""

from __future__ import annotations

import asyncio

import pytest

from vrun.device import DeviceBridge, InjectionError
from vrun.noise import GPSNoise
from vrun.profile import SpeedProfile
from vrun.route import Route
from vrun.safety import SafetyGuard
from vrun.session import Phase, RunSession
from vrun.soak import SoakResult, report_lines

# 江安校区附近一个小方环（与其它测试文件一致）
LAT0, LON0 = 30.5535, 103.9910


@pytest.fixture(autouse=True)
def _unarm_after_each_test(monkeypatch):
    """★ 每条用例结束后把装过保险的 guard 拆掉。

    :meth:`SafetyGuard.arm` 注册的是**进程级**的 ``atexit`` 与 signal 处理器，
    不随对象消亡 —— 任何 arm 过却没清理的用例，都会在**解释器退出时**
    真的触发第②条保险：起一个真子进程去连真机、跑清理。
    症状是「测试全跑完了才挂起几十秒」，看起来像 pytest 自己卡了。
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


# ══════════════════════════════════════════════════════════════
#  替身
# ══════════════════════════════════════════════════════════════

class FlakyBridge:
    """按**调用序号**制造失败的假设备。

    :param timeout_on: 这些序号（1 起）的调用报「超时」（可恢复）。
    :param fatal_on: 这些序号的调用报「硬失败」（通道断了，不可恢复）。
    """

    def __init__(self, timeout_on: set[int] | None = None,
                 fatal_on: set[int] | None = None) -> None:
        self.timeout_on = timeout_on or set()
        self.fatal_on = fatal_on or set()
        self.set_calls: list[tuple[float, float]] = []
        self.attempts = 0
        self.clear_calls = 0
        self.closed = False
        self.set_timeout = 5.0
        self.set_retries = 2

    async def connect(self):
        return None

    async def set_location(self, lat, lon, **kw):
        self.attempts += 1
        n = self.attempts
        if n in self.fatal_on:
            raise InjectionError("通道断了", timeout_exhausted=False)
        if n in self.timeout_on:
            raise InjectionError("注入超时", timeout_exhausted=True)
        self.set_calls.append((lat, lon))

    async def clear(self) -> bool:
        self.clear_calls += 1
        return True

    async def aclose(self) -> None:
        self.closed = True


class HangingLocation:
    """``set`` 永远不返回 —— 用来逼出「超时」这条路径。"""

    def __init__(self) -> None:
        self.calls = 0

    async def set(self, lat: float, lon: float) -> None:
        self.calls += 1
        await asyncio.sleep(3600)

    async def clear(self) -> None:
        pass


def make_session(bridge, **kw) -> tuple[RunSession, list]:
    events: list = []

    async def emit(name: str, payload: dict) -> None:
        events.append((name, payload))

    return RunSession(bridge=bridge, emit=emit, **kw), events


def fast_profile() -> SpeedProfile:
    """秒级跑完的剖面：高速 + 零起步 ramp。"""
    return SpeedProfile(v_bar=17.0, t_w=0.0, sigma_v=0.0, clamp_enabled=False)


def small_route() -> Route:
    """周长约 8.8 m 的小环 —— 跑完只要 2 秒。"""
    d = 0.00002
    return Route([
        (LAT0, LON0), (LAT0 + d, LON0),
        (LAT0 + d, LON0 + d), (LAT0, LON0 + d), (LAT0, LON0),
    ])


def error_codes(events: list) -> list[str]:
    return [p.get("code") for n, p in events if n == "error"]


# ══════════════════════════════════════════════════════════════
#  ① 设备层：超时必须与硬失败区分开
# ══════════════════════════════════════════════════════════════

async def test_默认注入超时放宽到五秒(monkeypatch):
    """★ 超时阈值必须是 5 s，不能是 2 s。

    真机实测 max 延迟 **2003 ms** —— 2 s 的阈值正好卡在长尾上，
    把「慢」当成了「死」。5 s 给了 2.5 倍余量。
    """
    bridge = DeviceBridge()
    assert bridge.set_timeout == 5.0, (
        f"单次注入超时阈值是 {bridge.set_timeout}s，"
        "真机长尾 2003 ms 会被误判成失败"
    )


async def test_超时重试用尽标记为可恢复(monkeypatch):
    """连续超时后抛的异常必须带 ``timeout_exhausted=True``。

    上层要靠这个标记决定「降级继续」还是「立刻失败」。
    """
    bridge = DeviceBridge(set_timeout=0.05, set_retries=1)
    hanging = HangingLocation()

    async def _fake_connect() -> None:
        bridge._loc = hanging

    monkeypatch.setattr(bridge, "connect", _fake_connect)

    with pytest.raises(InjectionError) as excinfo:
        await bridge.set_location(LAT0, LON0)

    assert excinfo.value.timeout_exhausted is True
    assert hanging.calls == 2, "set_timeout × (set_retries+1) 次尝试"


async def test_硬失败不标记为可恢复(monkeypatch):
    """非超时类错误（通道断了）必须带 ``timeout_exhausted=False``。

    反了的话，通道断了会话也会「降级继续」，
    于是一路刷失败直到耗尽阈值 —— 表面上是降级，实际上是拖延。
    """

    class Broken:
        async def set(self, lat, lon):
            raise RuntimeError("隧道掉了")

        async def clear(self):
            pass

    bridge = DeviceBridge(set_timeout=0.05, set_retries=1)

    async def _fake_connect() -> None:
        bridge._loc = Broken()

    monkeypatch.setattr(bridge, "connect", _fake_connect)

    with pytest.raises(InjectionError) as excinfo:
        await bridge.set_location(LAT0, LON0)

    assert excinfo.value.timeout_exhausted is False


# ══════════════════════════════════════════════════════════════
#  ② 会话层：超时降级继续，硬失败立刻终止
# ══════════════════════════════════════════════════════════════

async def test_注入超时降级继续会话不崩():
    """★ 一次超时**不能**让会话崩掉 —— 这正是 F22 的要害。

    19 分钟的验收，毁在一次瞬时抖动上不划算。
    """
    bridge = FlakyBridge(timeout_on={3})
    sess, events = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(),
                   noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    assert sess.phase != Phase.FAILED, "一次超时不该让会话失败"
    assert sess.injection_degraded == 1
    assert "injection_degraded" in error_codes(events)
    # 会话确实跑完了 —— 不是「崩在半路」
    assert sess.stats is not None and sess.stats.completed is True


async def test_连续超时超阈值才终止会话():
    """★ 容忍有上限：连续失败够多就真的失败。

    设备真掉线时，每点都超时 —— 这时候继续跑只是在刷失败。
    """
    bridge = FlakyBridge(timeout_on=set(range(3, 30)))
    sess, events = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(),
                   noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    assert sess.phase == Phase.FAILED
    assert "session_failed" in error_codes(events)
    assert sess.injection_degraded == sess.max_injection_failures, (
        "容忍次数必须正好是阈值 —— 少一次是放过，多一次是拖延"
    )


async def test_硬失败立刻终止会话不降级():
    """★ 通道断了 ⇒ 立刻失败，不走降级路径。

    降级只对「超时」开放。
    """
    bridge = FlakyBridge(fatal_on={3})
    sess, events = make_session(bridge, settle_s=0.05)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(),
                   noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    assert sess.phase == Phase.FAILED
    assert sess.injection_degraded == 0, "硬失败不该被记成降级"
    assert "injection_degraded" not in error_codes(events)


async def test_成功一次清零连续失败计数():
    """★ 计数是**连续**的 —— 中间成功一次就清零。

    隔一个成功再失败，说明设备是活的，不该累加到阈值。
    阈值调到 2：只要「不清零」，这两次失败就会攒成连续 2 次而崩掉会话。
    """
    # 第 3 次超时，第 5 次再超时 —— 两次之间有一次成功
    bridge = FlakyBridge(timeout_on={3, 5})
    sess, _ = make_session(bridge, settle_s=0.05, max_injection_failures=2)
    sess.set_route([(p[0], p[1]) for p in small_route().points])
    sess.configure(profile=fast_profile(),
                   noise=GPSNoise(level="off", sample_rate=20.0),
                   sample_rate=20.0)

    await sess.prepare()
    await sess.start()
    await sess._wait_task()

    assert sess.phase != Phase.FAILED
    assert sess.injection_degraded == 2, "两次隔开的超时都该被容忍"


def test_注入超时余量严格大于设备最坏耗时():
    """★ 线程侧的等待余量必须**严格大于**设备层的最坏注入耗时。

    相等就会在注入刚要失败时先超时，抛出来的是 ``TimeoutError``
    而不是 :class:`InjectionError` —— 「超时」这个信息就丢了，
    降级逻辑无从判断（这正是 F22 的第二层根因）。
    """
    bridge = DeviceBridge(set_timeout=5.0, set_retries=2)
    sess, _ = make_session(bridge)
    worst = bridge.set_timeout * (bridge.set_retries + 1)
    assert sess._inject_timeout() > worst, (
        f"余量 {sess._inject_timeout()} 必须严格大于最坏耗时 {worst}"
    )


# ══════════════════════════════════════════════════════════════
#  ③ 报告层：降级必须看得见
# ══════════════════════════════════════════════════════════════

def test_报告里出现降级行():
    """降级点必须出现在报告里 —— 看不见的洞比崩掉更危险。"""
    r = SoakResult(duration_s=60.0, route_m=100.0, injected=59, dropped=1)
    r.injection_degraded = 1
    text = "\n".join(report_lines(r))
    assert "降级跳过" in text
    assert "1 点" in text


def test_没有降级时不出现降级行():
    """没降级就别占一行 —— 报告要能一眼扫过去。"""
    r = SoakResult(duration_s=60.0, route_m=100.0, injected=60, dropped=0)
    assert "降级跳过" not in "\n".join(report_lines(r))
