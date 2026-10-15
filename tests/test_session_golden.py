"""test_session_golden.py —— `tick` / `progress` 事件的**跨语言契约**

为什么需要这个文件（2026-10-15 实测挖出来的）
------------------------------------------------

会话进行中，:mod:`vrun.session` 一直按 1 Hz 发 ``tick`` + ``progress``。
但 Swift 侧的 :class:`SidecarEvent` **压根没解码这些字段**，
:func:`SessionController.apply` **也只认 `device` 事件** ——
事件收下了、内容全丢。后果是 GUI 对会话 progress 是**瞎的**。

这个 bug 的性质很讨厌：**两侧都不报错**。
Python 照发，Swift 照收（``Decodable`` 成功，只是字段不存在），
于是「协议没对上」这件事可以潜伏很久 —— 直到有人真去读实时数据才发现。

所以这里把**真实发出的载荷**落成基准，两侧都对着它写：

- Python：``tests/test_session_golden.py``（本文件）—— 比对当前载荷与基准
- Swift：``app/VRun/Tests/VRunAppTests/LiveMetricsTests.swift`` —— 真读同一份 JSON

⚠️ 改协议字段名/增删字段时，两侧会**同时**红。照失败信息重新生成：

    ``$(scripts/resolve_python.sh) -m pytest tests/test_session_golden.py --regen``

为什么比对**键集合**而不是逐值相等
    浮点数会随 numpy 版本合法地漂一点，而**键名对不上才是真正的契约破裂**。
    所以键集合必须严格相等，数值只做宽松的量级检查。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vrun.noise import GPSNoise
from vrun.profile import SpeedProfile
from vrun.safety import SafetyGuard
from vrun.session import RunSession

GOLDEN = Path(__file__).resolve().parent / "golden" / "session_events.json"

LAT0, LON0 = 30.5535, 103.9910


@pytest.fixture(scope="session", autouse=True)
def _disarm_all_guards():
    """收工时拆掉本模块装过的所有保险。

    与 ``test_session.py`` 同理、且同样必需：``SafetyGuard.arm`` 注册的是
    **进程级** ``atexit`` 与 signal 处理器，不随对象消亡 ——
    解释器退出时会真的起子进程去连真机（实测会打印
    「主循环不可用，改由子进程执行 panic-clear」，然后失败退出）。

    ⚠️ 必须是 **session** 作用域，不能抄 test_session.py 用函数作用域：
        本模块有个 module 级的 ``captured`` fixture 会跑完整会话。
        函数作用域的 autouse 建在 module 之后 —— 那次 arm 发生在
        monkeypatch 装上**之前**，记不到，保险就漏到解释器退出时才炸。
        fixture 的作用域决定建立顺序：session > module > function。
    """
    # pytest 9 没有 `monkeypatch_session` 这个 fixture，
    # 但 `MonkeyPatch` 可以直接实例化并当上下文管理器用 —— 语义一样。
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


class FakeBridge:
    """假设备层。只记调用，不碰硬件。"""

    def __init__(self) -> None:
        self.set_calls: list[tuple[float, float]] = []
        self.clear_calls = 0
        # ⚠️ 这两个字段 session.py 的 `_inject_timeout()` 会读。
        #    漏了不会立刻炸，而是让**工作线程**里的每次注入都 AttributeError，
        #    表现为「收不到 tick」—— 排查起来绕得很。
        self.set_timeout = 2.0
        self.set_retries = 2

    async def connect(self):
        return None

    async def set_location(self, lat, lon, **kw):
        self.set_calls.append((lat, lon))

    async def clear(self) -> bool:
        self.clear_calls += 1
        return True

    async def aclose(self) -> None:
        return None


def _tiny_profile() -> SpeedProfile:
    """秒级跑完：高速 + 零起步 ramp（否则前 30 s 全在爬坡）。"""
    return SpeedProfile(v_bar=17.0, t_w=0.0, sigma_v=0.0, clamp_enabled=False)


async def capture() -> dict:
    """真跑一小段会话，抓下**第一个** ``tick`` 与**第一个实时** ``progress``。"""
    events: list[tuple[str, dict]] = []

    async def emit(name: str, payload: dict) -> None:
        events.append((name, payload))

    sess = RunSession(bridge=FakeBridge(), emit=emit, settle_s=0.01,
                      profile=_tiny_profile(),
                      noise=GPSNoise(level="realistic", seed=20261015))
    # 一个 200 m 的小方环，秒级就能跑完
    sess.set_route([(LAT0, LON0), (LAT0, LON0 + 0.0015),
                    (LAT0 + 0.0013, LON0 + 0.0015),
                    (LAT0 + 0.0013, LON0)])

    await sess.prepare()
    await sess.start()
    # 跑够 3 个 tick 就停，不必跑完
    for _ in range(400):
        if sum(1 for n, _ in events if n == "tick") >= 3:
            break
        await asyncio.sleep(0.02)
    await sess.stop()

    def first(name: str) -> dict | None:
        return next((p for n, p in events if n == name), None)

    # ⚠️ `progress` 是**被重载**的：会话摘要（prepare/start/stop 的返回）
    #    和 1 Hz 实时指标用的是同一个事件名。实时那份带 `sigma_v`，
    #    摘要那份不带 —— 这里按这个特征挑，别按事件名挑。
    live_progress = next(
        (p for n, p in events if n == "progress" and "sigma_v" in p), None)

    return {
        "tick": first("tick"),
        "progress": live_progress,
    }


@pytest.fixture(scope="module")
def captured() -> dict:
    """跑一次会话，供本模块所有用例共用。

    刻意只跑一次：会话要走真实时钟，跑四遍纯粹是慢。
    而这四条用例验的是**载荷形状**，形状与跑几遍无关。
    """
    return asyncio.run(capture())


def _load_golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _regen() -> None:
    payload = asyncio.run(capture())
    doc = {
        "schema_version": 1,
        "_doc": [
            "跨语言契约：vrun.session 发出的 tick / progress 实时载荷。",
            "Python 侧 tests/test_session_golden.py 比对键集合；",
            "Swift 侧 LiveMetricsTests.swift 直接读本文件并解码。",
            "⚠️ 改协议字段时两侧会同时红 —— 照失败信息跑 --regen 重新生成。",
            "⚠️ progress 事件名被重载：会话摘要与 1 Hz 实时指标同名，",
            "   靠 `sigma_v` 是否存在区分（见 _doc 最后一条）。",
        ],
        **payload,
    }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"已重新生成 {GOLDEN}")


# ══════════════════════════════════════════════════════════════════
#  契约断言
# ══════════════════════════════════════════════════════════════════


def test_tick载荷的键集合与基准一致(captured):
    golden = _load_golden()
    got = captured["tick"]
    assert got is not None, "没抓到 tick —— 会话没推进？"
    assert set(got) == set(golden["tick"]), (
        f"tick 载荷的键变了：\n"
        f"  基准 {sorted(golden['tick'])}\n"
        f"  实际 {sorted(got)}\n"
        f"⇒ 改协议就跑：$(scripts/resolve_python.sh) -m pytest "
        f"tests/test_session_golden.py --regen"
    )


def test_progress载荷的键集合与基准一致(captured):
    golden = _load_golden()
    got = captured["progress"]
    assert got is not None, "没抓到实时 progress —— sigma_v 特征没命中？"
    assert set(got) == set(golden["progress"]), (
        f"progress 载荷的键变了：\n"
        f"  基准 {sorted(golden['progress'])}\n"
        f"  实际 {sorted(got)}"
    )


def test_progress确实被重载_摘要与实时靠sigma_v区分(captured):
    """守住这个特征 —— Swift 侧就是靠它决定「要不要更新实时读数」。

    ⚠️ 这是一条**约定**，不是设计。若哪天有人把两种载荷合并了，
       Swift 会静默地把摘要当成实时指标（显示一个不动的 0 km/h）。
    """
    assert captured["progress"] is not None
    assert "sigma_v" in captured["progress"], "实时 progress 必须带 sigma_v"


def test抓到的数值量级合理(captured):
    """数值只做宽松检查 —— 真正要锁的是键名，不是浮点。"""
    tick = captured["tick"]
    assert tick is not None
    for key in ("t", "lat", "lon", "s", "v_target", "v_actual"):
        assert isinstance(tick[key], (int, float)), f"{key} 不是数值：{tick[key]!r}"
    assert -90 <= tick["lat"] <= 90
    assert -180 <= tick["lon"] <= 180
    assert tick["s"] >= 0, "弧长不能是负的"
    assert tick["v_target"] > 0, "目标速度必须为正"


# ── --regen：重生成基准的入口 ────────────────────────────────
#   $(scripts/resolve_python.sh) -m pytest tests/test_session_golden.py --regen

@pytest.fixture(scope="module", autouse=True)
def _maybe_regen(pytestconfig):
    """``--regen`` 时先重生成，再跑断言。

    顺序很关键：先生成再断言 —— 于是「刚改完协议」的情况下跑一次就绿，
    不需要「先 --regen、再跑一遍」两步。

    刻意用 fixture 而不是 ``pytest_collection_modifyitems``：
    后者拿不到 fixture，只能通过全局变量绕，而全局变量在
    ``xdist`` 并行下不可靠。
    """
    if pytestconfig.getoption("--regen", default=False):
        _regen()
