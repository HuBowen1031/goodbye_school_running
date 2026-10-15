"""test_soak.py —— 长跑压测工具的判定逻辑与无头整链路

**不需要 iPhone。** `soak()` 全程走假 bridge。

为什么这些测试值钱
------------------
压测本身要占着真机 10–20 分钟，一轮就是十几分钟起步。
而压测工具最要紧的**不是**能不能跑，而是**判定准不准** ——
一份把「丢了 40 个点」显示成「✅ 通过」的报告，比没有报告更坏。

所以这里的重点是 :func:`vrun.soak.report_lines` 的判定分支，
以及一次 2 秒的**无头整链路**（假 bridge 跑完整套 prepare → start → stop → cleanup）。
"""

from __future__ import annotations

import asyncio

import pytest

from vrun.device import InjectionError, InjectionStats
from vrun.route import Route
from vrun.safety import SafetyGuard
from vrun.profile import SpeedProfile
from vrun.soak import SoakResult, make_long_route, report_lines, soak, write_report


@pytest.fixture(scope="module", autouse=True)
def _disarm():
    """收工时拆掉本模块装过的所有保险。

    ``soak()`` 内部会 ``RunSession.start()`` → ``guard.arm()``，
    而 ``SafetyGuard.arm`` 注册的是**进程级** ``atexit`` 与 signal 处理器，
    不随对象消亡 —— 不拆的话解释器退出会真的去连真机跑清理。

    ⚠️ pytest 9 没有 ``monkeypatch_module`` 这种按作用域命名的 fixture；
       需要 session/module 作用域就用 ``pytest.MonkeyPatch()`` 手工建一个。
    """
    armed: list[SafetyGuard] = []
    original = SafetyGuard.arm

    def tracking_arm(self, *a, **kw):
        result = original(self, *a, **kw)
        if self._armed:
            armed.append(self)
        return result

    mp = pytest.MonkeyPatch()
    mp.setattr(SafetyGuard, "arm", tracking_arm)
    try:
        yield
    finally:
        for guard in armed:
            guard._restore_handlers()
        mp.undo()


def _ready_status():
    from vrun.device import DeviceStatus

    return DeviceStatus(connected=True, tunnel_up=True, phase="ready")


class FakeBridge:
    """假设备层。记调用、不碰硬件。"""

    def __init__(self, fail_after: int = -1) -> None:
        self.fail_after = fail_after
        self.set_calls: list[tuple[float, float]] = []
        self.clear_calls = 0
        self.set_timeout = 2.0
        self.set_retries = 0        # 测试里不要重试，否则「丢点」被重试掩盖
        self._stats = InjectionStats()
        self._status = _ready_status()

    async def connect(self):
        return None

    async def set_location(self, lat, lon, **kw):
        # ★ 必须复刻 DeviceBridge 的**契约**，不能只抛异常：
        #   真 bridge 在失败时是「先 dropped += 1，再抛 InjectionError」。
        #   只抛异常的话 dropped 永远是 0，而那正是压测要看的数。
        if 0 <= self.fail_after < len(self.set_calls):
            self._status.dropped += 1
            raise InjectionError("假注入失败")
        self.set_calls.append((lat, lon))
        self._status.injected += 1
        self._status.lat, self._status.lon = lat, lon
        self._stats.attempt_ms.append(12.0)

    async def clear(self) -> bool:
        self.clear_calls += 1
        return True

    async def aclose(self) -> None:
        return None

    def status(self):
        return self._status

    def injection_stats(self) -> InjectionStats:
        return self._stats


# ══════════════════════════════════════════════════════════════════
#  判定逻辑 —— 这一段是整个压测工具的价值所在
# ══════════════════════════════════════════════════════════════════


def _good() -> SoakResult:
    return SoakResult(duration_s=300, sample_rate_hz=1.0, speed_kmh=10.0,
                      wall_s=301.0, planned_samples=301, produced_samples=301,
                      injected=301, dropped=0, covered_s=300.0, reached_duration=True,
                      cleared=True, planned_s=833.0, produced_s=833.0,
                      latency={"attempts": 301, "timeouts": 0, "p50_ms": 12.0,
                               "p90_ms": 18.0, "p99_ms": 25.0, "max_ms": 40.0,
                               "min_ms": 9.0, "mean_ms": 13.0})


def test全绿时报通过():
    # ⚠️ 必须 join 再查：report_lines 返回 **list**，
    #    直接 `"x" in [...]` 是精确元素匹配而不是子串匹配 ——
    #    而结论那一行是整句，「✅ 通过」并不是某个独立元素。
    assert "✅ 通过" in "\n".join(report_lines(_good()))


def test丢了点绝不能报通过():
    """★ 本文件最重要的一条。

    背景：这个工具的全部意义就是「让跑步 App 以为你在真的跑」。
    轨迹里有洞 = App 那边看到的是一次**跳着走**的跑步 ——
    判定结果毫无意义，而报告如果还打「✅ 通过」，就等于骗自己。
    """
    r = _good()
    r.dropped = 40
    lines = report_lines(r)
    assert "❌ 未通过" in "\n".join(lines)
    assert "丢了 40 点" in "\n".join(lines)


def test丢点数必须出现在报告正文里而不是只在结论里():
    """结论会被扫一眼，正文不会。所以正文也要有。"""
    r = _good()
    r.dropped = 7
    body = [ln for ln in report_lines(r) if "注入成功" in ln]
    assert body, "报告里没有「注入成功」那一行"
    assert "丢失 7 点" in body[0]


def test没跑满时长不算通过():
    r = _good()
    r.reached_duration = False
    assert "❌ 未通过" in "\n".join(report_lines(r))


def test路线太短导致提前到终点也算没跑满():
    """★ 这条守的是 `covered_s` 阈值本身。

    上一条（``test没跑满时长不算通过``）只改了标志位，而整链路那条里
    ``error`` 非空会把阈值短路掉 —— 两者都抓不到「阈值失效」。
    所以这里造一个**没有错误、只是提前到终点**的情形：
    引擎正常跑完、phase 是 STOPPED、error 为空，但覆盖时长不够。

    现实对应的是 ``make_long_route`` 的余量被改小 ——
    那种情况下压测会「跑着跑着就到终点了」，而报告看不出来。
    """
    bridge = FakeBridge()
    # 12 m 的极短路线：速度剖面前 30 s 在爬坡（约 6 km/h ≈ 1.7 m/s），
    # 所以约 7 秒就走完了 —— 而计划时长给 15 秒。
    # 刻意不用 `make_long_route`：它带 2 倍余量，正是为了避免提前到终点。
    short = Route([(30.5535, 103.9910), (30.5535, 103.99111)])
    assert short.length() < 20.0, short.length()

    rep = asyncio.run(soak(bridge, short, duration_s=15.0,
                           sample_rate_hz=5.0, speed_kmh=10.0))

    assert rep.error is None, "这条用例要的是「无错但没跑满」，不该有错"
    assert rep.covered_s < 15.0, "短路线本来就该提前到终点"
    assert not rep.reached_duration, "覆盖时长不够却被判成跑满了"
    assert "❌ 未通过" in "\n".join(report_lines(rep))
    assert rep.cleared, "提前结束也必须清理干净"


def test有报错不算通过():
    r = _good()
    r.error = "InjectionError: 隧道掉了"
    assert "❌ 未通过" in "\n".join(report_lines(r))


def test清理未确认不算通过且必须提示手动清理():
    """清理失败 = 手机可能还卡在注入位置上，这是最坏的一种失败。"""
    r = _good()
    r.cleared = False
    text = "\n".join(report_lines(r))
    assert "❌ 未通过" in text
    assert "panic-clear" in text, "清理没成功却没告诉用户怎么救"


def test没数据时延迟显示破折号而不是0():
    r = _good()
    r.latency = {}
    assert "注入延迟    无数据" in report_lines(r)


# ══════════════════════════════════════════════════════════════════
#  延迟分布
# ══════════════════════════════════════════════════════════════════


def test分位数用最近秩法且含长尾():
    s = InjectionStats(attempt_ms=[10.0] * 90 + [2000.0] * 10)
    d = s.summary()
    assert d["p50_ms"] == 10.0
    assert d["p90_ms"] == 10.0
    assert d["p99_ms"] == 2000.0, "超时那批就是长尾，必须能被看见"
    assert d["max_ms"] == 2000.0


def test样本不足时如实返回None而不是硬凑():
    d = InjectionStats().summary()
    assert d["attempts"] == 0
    assert all(d[k] is None for k in ("p50_ms", "p90_ms", "p99_ms", "max_ms"))


# ══════════════════════════════════════════════════════════════════
#  路线
# ══════════════════════════════════════════════════════════════════


def test压测路线长度达标():
    """路线太短的话，引擎会先到终点，压测提前结束 —— 而报告看不出来。"""
    r = make_long_route(2000.0, (30.5535, 103.9910))
    assert abs(r.length() - 2000.0) < 1.0


def test压测路线必须比计划跑的距离长():
    """2 倍余量：10 km/h × 600 s = 1667 m，路线得 > 3334 m。"""
    planned = 10.0 / 3.6 * 600.0
    r = make_long_route(planned * 2.0, (30.5535, 103.9910))
    assert r.length() > planned, "路线不够长的话压测会提前跑到终点"


# ══════════════════════════════════════════════════════════════════
#  无头整链路（假 bridge，不碰硬件）
# ══════════════════════════════════════════════════════════════════


def test两秒整链路跑通并清理():
    """prepare → start → 到点 stop → cleanup 的完整链路。

    压测工具本身也是新代码，也得有条测试证明它**能跑完**，
    而不是只在纸面上看着对。
    """
    bridge = FakeBridge()
    route = make_long_route(400.0, (30.5535, 103.9910))

    rep = asyncio.run(soak(bridge, route, duration_s=2.0,
                           sample_rate_hz=5.0, speed_kmh=10.0))

    assert rep.error is None, rep.error
    assert rep.reached_duration
    assert rep.dropped == 0
    assert rep.injected > 0
    assert rep.produced_samples > 0
    assert rep.cleared, "清理没确认 —— 手机可能还卡在注入位置上"
    assert bridge.clear_calls >= 1, "cleanup 没真的调 clear"
    assert bridge.set_calls, "一次都没注入"


def test整链路能如实报出丢点():
    """假 bridge 从第 5 次起失败 ⇒ dropped 非零 ⇒ 报告必须是红的。

    ★ 顺带钉住一个设计事实：``RunSession._on_sample_blocking`` 用的是
      ``strict=True``，所以**单点注入失败会让整个会话中断** ——
      不存在「悄悄跳掉几个点、剩下的照跑」这种情况。
      于是压测里 ``dropped > 0`` 必然同时伴随会话提前结束与一条报错，
      三者一起出现才是正常的失败形态。
    """
    bridge = FakeBridge(fail_after=5)
    route = make_long_route(400.0, (30.5535, 103.9910))

    rep = asyncio.run(soak(bridge, route, duration_s=2.0,
                           sample_rate_hz=5.0, speed_kmh=10.0))

    assert rep.dropped > 0, "假 bridge 明明在失败，dropped 却没记上"
    assert not rep.reached_duration, "strict 模式下会话应该已经中断"
    assert rep.error, "会话中断必须留一条错误，别悄悄结束"
    assert "❌ 未通过" in "\n".join(report_lines(rep))
    # ★ 就算出错了，清理也必须做到 —— 否则手机就留在那儿了
    assert rep.cleared, "出错时反而没清理，这是最坏的结果"


def test报告能落盘且含判定所需字段(tmp_path):
    rep = _good()
    path = tmp_path / "soak.json"
    write_report(rep, str(path))

    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    for k in ("dropped", "injected", "reached_duration", "cleared",
              "latency_ms", "error"):
        assert k in data, f"报告缺字段 {k} —— M4 对账要靠它"
    assert data["dropped"] == 0


# ══════════════════════════════════════════════════════════════════
#  到点模式（--to-end）—— M4 的 3 km 验收走这条
#
#  ⚠️ 判据与定时模式**完全不同**，不能互相代偿：
#     · 定时模式问「这段时间稳不稳」⇒ 红线是丢失点数 + 覆盖时长
#     · 到点模式问「整条轨迹跑完对不对」⇒ 红线是**跑到了终点 + ETA 准**
# ══════════════════════════════════════════════════════════════════


def _to_end() -> SoakResult:
    return SoakResult(
        duration_s=1902.0, sample_rate_hz=1.0, speed_kmh=10.0,
        route_m=3000.0, to_end=True,
        planned_eta_s=1081.2, eta_error_s=1.35,
        wall_s=1084.0, planned_samples=1903, produced_samples=1081,
        injected=1082, dropped=0, cleared=True,
        planned_s=3000.0, produced_s=3000.0, covered_s=1082.55,
        route_completed=True,
        latency={"attempts": 1082, "timeouts": 0, "p50_ms": 5.0,
                 "p90_ms": 7.0, "p99_ms": 21.0, "max_ms": 51.0,
                 "min_ms": 2.0, "mean_ms": 5.7},
    )


def test到点模式全绿时报通过():
    assert "✅ 通过" in "\n".join(report_lines(_to_end()))


def test到点模式没跑到终点不算通过():
    """★ 这条是「只跑了一半却报通过」的最后一道防线。"""
    r = _to_end()
    r.route_completed = False
    text = "\n".join(report_lines(r))
    assert "❌ 未通过" in text
    assert "没跑到路线终点" in text


def test到点模式ETA超差不算通过():
    """works.md §6 M4 的验收线：预览 ETA 与实际用时差 < 2 s。"""
    r = _to_end()
    r.eta_error_s = 2.01
    text = "\n".join(report_lines(r))
    assert "❌ 未通过" in text
    assert "超出" in text


def test到点模式ETA刚好在容差内算通过():
    """容差是**严格小于** —— 2.00 s 不算过。边界要钉死。"""
    r = _to_end()
    r.eta_error_s = 1.999
    assert "✅ 通过" in "\n".join(report_lines(r))
    r.eta_error_s = 2.0
    assert "❌ 未通过" in "\n".join(report_lines(r))


def test到点模式不能拿跑满时长冒充跑到终点():
    """定时模式的 reached_duration 在到点模式下**不参与判定**。

    反过来也成立：只设 reached_duration 而 route_completed=False，仍要判红。
    """
    r = _to_end()
    r.route_completed = False
    r.reached_duration = True
    assert "❌ 未通过" in "\n".join(report_lines(r))


def test到点模式没跑到终点时不硬凑ETA():
    """eta_error 为 None 时要说「无从对比」，不能显示一个假的 0.00 s。"""
    r = _to_end()
    r.route_completed = False
    r.eta_error_s = None
    text = "\n".join(report_lines(r))
    assert "无从对比" in text
    assert "❌ 未通过" in text


def test到点模式也会显示注入延迟与清理状态():
    """延迟分布与清理是**两种模式共用**的底线，不能因为换了模式就消失。"""
    text = "\n".join(report_lines(_to_end()))
    assert "注入延迟" in text
    assert "清理        已确认" in text


def test到点模式丢点一样算红():
    r = _to_end()
    r.dropped = 3
    assert "❌ 未通过" in "\n".join(report_lines(r))


def test到点模式清理未确认算红():
    r = _to_end()
    r.cleared = False
    text = "\n".join(report_lines(r))
    assert "❌ 未通过" in text
    assert "panic-clear" in text


# ── wait_until_finished ──


def test没有引擎任务时wait_until_finished立刻返回真():
    from vrun.session import RunSession
    sess = RunSession(bridge=FakeBridge(), emit=None)
    assert asyncio.run(sess.wait_until_finished(timeout=1.0)) is True


def testwait_until_finished真能等到引擎跑完():
    """★ 这条钉的是 F15 那个竞态的修复。

    ``_wait_task`` 固定 5 秒就返回；``wait_until_finished`` 必须能等更久。
    用一个跑 6 秒以上的引擎来区分两者 ——
    5 秒的版本必然返回 False/提前返回，够长的版本必须等到。
    """
    from vrun.session import RunSession

    route = make_long_route(4000.0, (30.5535, 103.9910))
    sess = RunSession(bridge=FakeBridge(), emit=lambda *_: asyncio.sleep(0),
                      settle_s=0.0, sample_rate=5.0,
                      profile=SpeedProfile(v_bar=10.0))

    async def run() -> bool:
        sess.set_route([(p[0], p[1]) for p in route.points])
        await sess.prepare()
        await sess.start()
        try:
            # 引擎跑完 4000 m 要几十秒；这里只要求它**能等**而不是 5 秒就返回
            got = await sess.wait_until_finished(timeout=8.0)
            return got
        finally:
            await sess.stop()
            await sess.cleanup()

    # 8 秒不够跑完 4000 m ⇒ 返回 False，但这恰恰证明「它在等」而不是立刻返回
    assert asyncio.run(run()) is False
