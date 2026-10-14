"""test_device.py — DeviceBridge 的离线行为

**绝大多数测试不需要 iPhone。**
它们只验证「不连接时也不该炸」这一类性质 ——
而这恰恰是最容易出事的部分：安全红线上的操作（:meth:`DeviceBridge.clear`）
必须在任何状态下都不抛异常。

需要真机的那部分（M2 验收清单里的端到端项）不在这里，
它们是**手动**验证的，见 ``docs/preflight.md``。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from vrun.device import (
    DeviceBridge,
    DeviceError,
    DeviceNotFound,
    DeviceStatus,
    InjectionError,
    TunnelError,
)


# ══════════════════════════════════════════════════════════════
#  ⓪ 离线替身 —— 让「校验闸门」这条测试**绝不**碰到真机
# ══════════════════════════════════════════════════════════════

class _StubLocationSimulation:
    """只提供 ``async set(lat, lon)`` 的假注入器。"""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    async def set(self, lat: float, lon: float) -> None:
        self.calls.append((lat, lon))


@pytest.fixture
def validation_only_bridge(monkeypatch) -> DeviceBridge:
    """一个**只走校验、不连设备**的 :class:`DeviceBridge`。

    ★ 为什么必须有它（2026-10-14 定位到的随机卡死根因）

        :meth:`DeviceBridge.set_location` 是「先校验、再连接、再注入」。
        本文件绝大多数测试只碰 :meth:`clear` / :meth:`aclose` / :meth:`status`
        —— 它们都不会触发连接，安全。

        唯独「±90 / ±180 不能误拒」这条：坐标是**合法的**，
        于是它一路穿过校验闸门，撞上 ``await self.connect()`` ——
        **真的去连 iPhone 了**。

        症状极具迷惑性，因为它完全取决于手机在不在线：

        ┌────────────────┬──────────────────────────────────────────┐
        │ 手机状态       │ 表现                                     │
        ├────────────────┼──────────────────────────────────────────┤
        │ 没插 / usbmuxd │ 快速抛 DeviceNotFound，测试「通过」      │
        │ 查不到设备     │                                           │
        ├────────────────┼──────────────────────────────────────────┤
        │ 插着           │ usbmuxd 找到设备 → 真的去建隧道 →        │
        │                │ **整套 pytest 挂死**                    │
        └────────────────┴──────────────────────────────────────────┘

        所以同一个测试套件，一次 21 s 跑完、一次挂 >60 s，
        看起来像「偶发抖动」，实际是「测试在偷偷连手机」。

    修法：把连接换成 ``connect`` 的空实现 + 一个假注入器。
    这样这条测试真正想验的东西 —— **校验闸门的边界** —— 一个不差地被覆盖，
    而硬件彻底隔离在外。
    """
    bridge = DeviceBridge()
    stub = _StubLocationSimulation()

    async def _fake_connect() -> None:
        bridge._loc = stub

    monkeypatch.setattr(bridge, "connect", _fake_connect)
    bridge.stub = stub            # type: ignore[attr-defined] 供断言取用
    return bridge


# ══════════════════════════════════════════════════════════════
#  ① 分层纪律回归护栏 —— 全项目最重要的一条
# ══════════════════════════════════════════════════════════════

def test_pure_layer_never_imports_pymobiledevice3():
    """★ ① 纯计算层**绝不能** import ``pymobiledevice3``。

    file-manifest §4.2 的核心纪律：设备层是**唯一**允许它的地方。
    一旦某个 ① 层文件偷偷引了它，`import vrun.estimate` 就会拖进
    USB / 隧道 / 配对这一整套设备依赖 —— 而滑块面板的数学
    本来是可以在**没有任何 iPhone** 的机器上跑的（works.md §5.7）。

    必须在**干净子进程**里查：在本进程里 ``vrun.device`` 可能早就被导入了，
    ``sys.modules`` 里挂着，查不出「谁引的」。
    """
    code = (
        "import sys, importlib\n"
        "mods = ['vrun.engine','vrun.estimate','vrun.noise','vrun.profile',"
        "'vrun.route','vrun.geo','vrun.recorder']\n"
        "for m in mods:\n"
        "    importlib.import_module(m)\n"
        "bad = [k for k in sys.modules if k.split('.')[0] == 'pymobiledevice3']\n"
        "print('LEAKED' if bad else 'CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        env={**__import__("os").environ,
             "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")},
    )
    assert "LEAKED" not in proc.stdout, (
        f"① 纯计算层引入了 pymobiledevice3：{proc.stdout.strip()}"
    )


# ══════════════════════════════════════════════════════════════
#  ② 未连接状态下的行为
# ══════════════════════════════════════════════════════════════

async def test_clear_before_connect_is_noop_not_error():
    """★ 没连就 clear()：返回 False，**不抛**。

    这是安全红线的底线 —— 清理动作在任何状态下都不许抛异常，
    否则「清理失败」会淹没真正要报告的原因。
    """
    bridge = DeviceBridge()
    assert await bridge.clear() is False


async def test_aclose_before_connect_is_noop():
    bridge = DeviceBridge()
    await bridge.aclose()          # 不该抛
    assert bridge.status().connected is False


async def test_aclose_is_idempotent():
    bridge = DeviceBridge()
    await bridge.aclose()
    await bridge.aclose()
    await bridge.aclose()
    assert bridge.status().phase == "disconnected"


async def test_status_is_readonly_and_does_not_connect():
    """status() 被 GUI 高频调用，绝不能触发连接。"""
    bridge = DeviceBridge()
    for _ in range(100):
        st = bridge.status()
        assert st.connected is False
        assert st.phase == "disconnected"


# ══════════════════════════════════════════════════════════════
#  ③ 参数校验 —— 必须在**触碰设备之前**就拒掉
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("lat", [-90.1, 90.1, 1000.0, float("nan"), float("inf")])
async def test_latitude_out_of_range_rejected(lat):
    """非法纬度必须在连设备之前就被拒 —— 省掉一次必然失败的隧道建立。"""
    bridge = DeviceBridge()
    with pytest.raises(ValueError, match="纬度"):
        await bridge.set_location(lat, 103.99)
    assert bridge.status().connected is False, "非法参数不应触发连接"


@pytest.mark.parametrize("lon", [-180.1, 180.1, 1000.0, float("nan")])
async def test_longitude_out_of_range_rejected(lon):
    bridge = DeviceBridge()
    with pytest.raises(ValueError, match="经度"):
        await bridge.set_location(30.55, lon)
    assert bridge.status().connected is False


async def test_boundary_coordinates_accepted(validation_only_bridge):
    """±90 / ±180 是合法的，不能误拒。

    用 :func:`validation_only_bridge` 而不是裸 ``DeviceBridge()`` ——
    坐标合法就会一路穿过校验闸门去连真机，在手机插着时会把整个套件挂死。
    """
    bridge = validation_only_bridge
    for lat, lon in [(90.0, 180.0), (-90.0, -180.0)]:
        await bridge.set_location(lat, lon)      # 抛 ValueError 即回归
        assert bridge.status().injected == len(bridge.stub.calls), "边界坐标没被注入"
    assert bridge.stub.calls == [(90.0, 180.0), (-90.0, -180.0)]


# ══════════════════════════════════════════════════════════════
#  ④ DeviceStatus 的事件序列化
# ══════════════════════════════════════════════════════════════

def test_status_as_event_shape():
    """必须与 ``ipc.EV_DEVICE`` 事件载荷对齐（GUI 按这些键渲染）。"""
    st = DeviceStatus(connected=True, ddi_mounted=True, tunnel_up=True,
                      device_name="iPhone", ios_version="27.0.1",
                      phase="ready", injected=120, dropped=2, error=None)
    ev = st.as_event()
    for key in ("connected", "ddi_mounted", "tunnel_up", "device_name",
                "ios_version", "phase", "injected", "dropped", "error"):
        assert key in ev, f"事件缺字段 {key}"
    assert ev["injected"] == 120
    assert ev["dropped"] == 2


def test_status_returns_a_copy_not_a_reference():
    """★ :meth:`DeviceBridge.status` 必须返回**副本**。

    2026-10-14 首跑真机时撞到的 bug：``DeviceStatus`` 是可变 dataclass，
    ``aclose()`` 原地改字段，于是「先取快照再 teardown」拿到的还是同一个对象 ——
    快照跟着一起变，``device-status`` 永远打印「没连」，哪怕隧道真建起来了。
    这种别名 bug 极难排查，因为它看起来像「连接失败」。
    """
    bridge = DeviceBridge()
    snap = bridge.status()

    # 改内部状态
    bridge._set_status(connected=True, tunnel_up=True, phase="ready")
    bridge.status().injected += 99

    assert snap.connected is False, "快照被内部状态污染了"
    assert snap.phase == "disconnected"
    assert bridge.status().connected is True, "内部状态被外部改动污染了"

    # 直接改拿到的副本也不该影响内部
    st = bridge.status()
    st.phase = "hacked"
    assert bridge.status().phase == "ready"


def test_dropped_counter_is_reported():
    """``dropped`` 是「轨迹有洞」的可见信号，M4 统计丢失点要靠它。"""
    bridge = DeviceBridge()
    bridge._status.injected += 10
    bridge._status.dropped += 1
    assert bridge.status().injected == 10
    assert bridge.status().dropped == 1


# ══════════════════════════════════════════════════════════════
#  ⑤ 异常层级
# ══════════════════════════════════════════════════════════════

def test_exception_hierarchy_is_catchable_as_one():
    """调用方想用**一个** except 兜住所有设备层错误。"""
    for exc in (DeviceNotFound, TunnelError, InjectionError):
        assert issubclass(exc, DeviceError)


def test_injection_error_message_has_coordinates():
    """注入失败必须报出**是哪一点** —— 否则无法定位轨迹上的洞。"""
    msg = str(InjectionError("注入 30.55000,103.99000 失败（已重试 2 次）：timeout"))
    assert "30.55000" in msg and "103.99000" in msg