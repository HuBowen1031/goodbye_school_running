"""device.py — DVT 位置注入设备桥 ★

职责
    封装 :mod:`pymobiledevice3` 的 ``LocationSimulation``：
    连接 → 挂 DDI → 建隧道 → ``set`` / ``clear``。

**全项目唯一允许 ``import pymobiledevice3`` 的地方。**
① 纯计算层（:mod:`vrun.engine` 等）永远不碰它，所以
``import vrun.estimate`` 不会把设备依赖拖进来（file-manifest §4.2 的分层纪律）。

为什么值得自己写一层，而不是直接调 CLI
    上游 CLI 形态是::

        pymobiledevice3 developer dvt simulate-location set -- LAT LON

    这个命令在 ``set`` 之后会落到 ``OSUTILS.wait_return()`` **永久阻塞**
    （findings.md F8 实测），并且每调用一次都新建一条 DTX 通道。
    于是「沿路线连续注入 1 Hz」在 CLI 层面**根本不存在循环形态** ——
    要跑完一条 3 km 轨迹，得自己写循环。

    本模块的价值就是：**建一次隧道、开一条长连接、循环调 :meth:`set`**
    ——这正是 CLI 形态做不到、而 device.py 存在的唯一理由。

上游 API（2026-10-14 对 11.27.2 实测核对，非凭记忆）
    ``LocationSimulation(dvt)``   —— async context manager
        ``async set(latitude, longitude) -> None``
        ``async clear() -> None``
    ⚠️ **只接受经纬度两个参数**。海拔与水平精度无法指定 ——
       实测注入点的 ``altitude`` 恒为 0.00、``horizontalAccuracy`` 恒为 5.0
       （works.md §6 M0 实测记录）。这直接决定了 §3 的噪声模型里
       「海拔漂移」和「精度半径」两项目前造不出来。

隧道
    iOS 17+ 的 DVT 只能走 RSD 隧道。上游把建隧道收敛在两处：
    ``native_tunnel.establish_native_rsd``（macOS 上免 root）
    与 ``userspace_tunnel.establish_userspace_rsd``（纯 Python 用户态）。
    ⚠️ **隧道是进程级单例**：PyTCP 网络栈全局唯一，同一进程第二次建隧道
    会直接失败。所以本类**惰性建立一次并长期复用**，不做「连了再断」。

清理职责的边界
    本模块只提供**底层** :meth:`clear` 原语，**不决定何时清理**。
    何时清理是 :class:`vrun.safety.SafetyGuard` 的独占职责 ——
    两处都管清理必然逻辑分叉，而清理逻辑一旦分叉就是安全红线
    （works.md §2.3）。
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from dataclasses import dataclass, field, replace
from typing import List, Optional

# ⚠️ 唯一允许的设备层 import。① 纯计算层绝不能有它。
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

log = logging.getLogger(__name__)

__all__ = [
    "DeviceStatus",
    "DeviceError",
    "DeviceNotFound",
    "TunnelError",
    "InjectionError",
    "DeviceBridge",
]


# ══════════════════════════════════════════════════════════════
#  异常
#
#  全部继承 DeviceError，方便调用方一个 except 兜住。
#  **绝不用裸 Exception 往上抛** —— 调用方需要区分
#  「设备没插」和「注入失败」：前者该提示用户插线，后者该重试。
# ══════════════════════════════════════════════════════════════

class DeviceError(Exception):
    """设备层所有异常的基类。"""


class DeviceNotFound(DeviceError):
    """没找到 USB 设备 / 未配对 / 开发者模式未开。"""


class TunnelError(DeviceError):
    """RSD 隧道建立失败。这是 iOS 17+ 最常见的失败点。"""


class InjectionError(DeviceError):
    """单点注入失败（超时、重试用尽）。

    :param timeout_exhausted: ``True`` 表示失败原因是**连续超时**；
        ``False`` 表示是别的错误（隧道掉了 / 通道断了 / 参数非法）。

    ★ 为什么要区分
        「超时」是**可恢复**的瞬时抖动 —— 设备还连着，只是这一下慢了。
        「其他错误」是**不可恢复**的 —— 通道已经断了，重试没有意义
        （见 :meth:`DeviceBridge.set_location` 里的分支）。
        上层要按这个区别决定「降级继续」还是「立刻失败」，
        所以这个标记必须由**设备层**如实给出，不能让上层去猜异常文本。
    """

    def __init__(self, message: str, *, timeout_exhausted: bool = False) -> None:
        super().__init__(message)
        self.timeout_exhausted = timeout_exhausted


# ══════════════════════════════════════════════════════════════
#  状态
# ══════════════════════════════════════════════════════════════

class Phase:
    """连接阶段。用字符串常量而不是 enum —— 它们要直接进 IPC 的 JSON。"""

    DISCONNECTED = "disconnected"   # 从没连过
    LOCKDOWN = "lockdown"           # 拿到 lockdown（已配对）
    TUNNEL = "tunnel"               # RSD 隧道已建立
    READY = "ready"                 # DTX 通道已开，随时可 set


@dataclass
class DeviceStatus:
    """给 GUI 看的状态快照（file-manifest §4.2.1 的接口约定）。"""

    connected: bool = False
    ddi_mounted: bool = False
    tunnel_up: bool = False
    device_name: Optional[str] = None
    ios_version: Optional[str] = None
    phase: str = Phase.DISCONNECTED
    #: 累计成功注入点数。M4 的「统计丢失点数」要靠它对账。
    injected: int = 0
    #: 累计因失败而**跳过**的点数。非零说明轨迹有洞，必须能看见。
    dropped: int = 0
    error: Optional[str] = None
    #: ★ 上一次**成功注入**的位置。GUI 地图上那个绿圈就画在这里。
    #:
    #: 为什么不给「iPhone 的真实 GPS」——
    #:     pymobiledevice3 11.27.2 **没有读取设备定位的 API**
    #:     （除注入方向外，全库无任何模块提及 latitude；
    #:     `remote/core_device/location_service.py` 只列模拟场景，不读位置）。
    #:
    #: 而注入期间设备上报的位置**就是**我们给的那个坐标 ——
    #:     所以「上次注入位置」在语义上恰好等于「iPhone 现在报的位置」。
    #:     两者在「注入中」这一状态上是等价的，这正是绿圈需要的含义。
    #:
    #: ``clear()`` 之后置回 ``None``：此时设备回到了真实 GPS，
    #: 而真实位置我们读不到，如实说「不知道」比编一个强。
    lat: Optional[float] = None
    lon: Optional[float] = None

    def as_event(self) -> dict:
        """转成 ``device`` 事件的载荷（与 ``ipc.EV_DEVICE`` 对齐）。"""
        return {
            "connected": self.connected,
            "ddi_mounted": self.ddi_mounted,
            "tunnel_up": self.tunnel_up,
            "device_name": self.device_name,
            "ios_version": self.ios_version,
            "phase": self.phase,
            "injected": self.injected,
            "dropped": self.dropped,
            "error": self.error,
            "lat": self.lat,
            "lon": self.lon,
        }


@dataclass
class InjectionStats:
    """注入延迟分布。

    ★ 为什么**不**放进 :class:`DeviceStatus`
        ``DeviceStatus`` 是 1 Hz 推给 GUI 的状态快照，
        把一个随时间增长的可变列表塞进去，等于每秒把整个历史再传一遍。
        延迟分布只对 M4 的压测有意义，所以留在这儿，按需取。

    ★ 记录的是**每一次尝试**，不是每一次成功
        超时的那些也要留 —— 它们正是延迟分布的长尾。
        只记成功的话，看到的分布会很漂亮，而真出问题的那次被抹掉了。
    """

    #: 每次 ``set`` 尝试的耗时（毫秒）。含失败与超时。
    attempt_ms: List[float] = field(default_factory=list)
    #: 因超时而失败的尝试次数
    timeouts: int = 0

    def summary(self) -> dict:
        """分位数摘要。样本太少时如实说「样本不足」，不硬凑数字。"""
        xs = sorted(self.attempt_ms)

        def pct(p: float) -> Optional[float]:
            if not xs:
                return None
            # 最近秩法：p=0.5 取第 ceil(0.5*n) 个
            k = max(1, min(len(xs), int(-(-p * len(xs) // 1))))
            return xs[k - 1]

        return {
            "attempts": len(xs),
            "timeouts": self.timeouts,
            "min_ms": xs[0] if xs else None,
            "p50_ms": pct(0.50),
            "p90_ms": pct(0.90),
            "p99_ms": pct(0.99),
            "max_ms": xs[-1] if xs else None,
            "mean_ms": (sum(xs) / len(xs)) if xs else None,
        }


# ══════════════════════════════════════════════════════════════
#  DeviceBridge
# ══════════════════════════════════════════════════════════════

class DeviceBridge:
    """一条可长期复用的 DVT 注入通道。

    用法::

        bridge = DeviceBridge()
        guard = SafetyGuard(bridge)      # 由调用方负责装三保险
        guard.arm()
        try:
            await bridge.set_location(30.5554, 103.9907)
            ...
        finally:
            await bridge.aclose()

    线程/协程安全
        DTX 通道是**单条**串行流。所有 :meth:`set_location` 都过一把
        :attr:`_lock`，避免两个循环交叉写坏轨迹。
    """

    def __init__(
        self,
        serial: Optional[str] = None,
        *,
        require_usb: bool = True,
        set_timeout: float = 5.0,
        set_retries: int = 2,
    ) -> None:
        """
        :param serial: 目标设备 udid；``None`` = 第一台 USB 设备。
        :param require_usb: 只认 USB。无线设备延迟大且隧道不稳，本项目不用。
        :param set_timeout: 单次 ``set`` 的超时（秒）。
        :param set_retries: 超时后的重试次数。**有限重试**，不做无限重试。

        ⚠️ ``set_timeout`` 默认 **5 s**，不是 2 s
            真机实测（2026-10 望江那轮）的注入延迟分布：
            p50 7 ms / p90 8 ms / p99 60 ms / **max 2003 ms**。
            2 s 的阈值**正好卡在长尾上** —— 那 3 次「超时」里，
            至少有一次其实只跑了 2003 ms，**慢，但没失败**，
            却被当成失败重试了一遍（findings F22）。
            5 s 给了 2.5 倍余量，把「慢」和「死」区分开。
        """
        self.serial = serial
        self.require_usb = require_usb
        self.set_timeout = set_timeout
        self.set_retries = set_retries

        self._status = DeviceStatus()
        self._lock = asyncio.Lock()

        # ★ 注入延迟分布。刻意**不放进 DeviceStatus** ——
        #   那是每秒推给 GUI 的状态快照，塞进一个随时间增长的可变列表，
        #   等于每秒把整段历史再传一遍。
        self._stats = InjectionStats()

        # 三层资源，从外到内：RSD 隧道 → DVT provider → LocationSimulation
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._dvt: Optional[DvtProvider] = None
        self._loc: Optional[LocationSimulation] = None
        self._lockdown = None
        #: 已尝试过建隧道吗。用于**避免重复建立**（隧道是进程级单例）。
        self._tunnel_attempted = False

    # ── 状态 ────────────────────────────────────────────────

    def injection_stats(self) -> InjectionStats:
        """注入延迟分布（M4 压测用）。"""
        return self._stats

    def status(self) -> DeviceStatus:
        """当前状态快照。**只读，不触发连接** —— GUI 会高频调用它。

        ⚠️ 返回的是**副本**，不是内部对象的引用。2026-10-14 首跑真机时撞到：
        ``DeviceStatus`` 是可变 dataclass，``aclose()`` 原地改字段，
        于是「先取快照再 teardown」的写法拿到的还是同一个对象 ——
        快照跟着一起变了，``device-status`` 永远只会打印「没连」，
        哪怕隧道真的建起来了。返回副本从根上堵掉这类别名 bug。
        """
        return replace(self._status)

    def _set_status(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self._status, k, v)

    # ── 连接 ────────────────────────────────────────────────

    async def connect(self) -> DeviceStatus:
        """建好整条链路：lockdown → 隧道 → DTX 通道。

        **幂等**：已 ``READY`` 时直接返回现状，不重复建隧道
        （隧道是进程级单例，重复建立会失败）。

        :raises DeviceNotFound: 没设备 / 未配对
        :raises TunnelError: 隧道建不起来
        """
        if self._status.phase == Phase.READY and self._loc is not None:
            return self._status

        async with self._lock:
            # 双检：等锁期间可能已被别的协程连上了
            if self._status.phase == Phase.READY and self._loc is not None:
                return self._status

            await self._connect_lockdown()
            await self._ensure_ddi()
            await self._ensure_tunnel()
            await self._open_channel()
            return self._status

    async def _connect_lockdown(self) -> None:
        from pymobiledevice3.lockdown import create_using_usbmux

        kwargs = {"autopair": True}
        if self.serial:
            kwargs["serial"] = self.serial
        if self.require_usb:
            kwargs["connection_type"] = "USB"

        try:
            self._lockdown = await create_using_usbmux(**kwargs)
        except Exception as exc:
            self._set_status(connected=False, error=f"连接失败：{exc}")
            raise DeviceNotFound(
                f"连接设备失败：{exc}\n"
                "请检查：USB 已插 / 已信任此电脑 / 开发者模式已开启。"
            ) from exc

        name = self._lockdown.udid
        self._set_status(
            connected=True,
            device_name=name,
            phase=Phase.LOCKDOWN,
            error=None,
        )
        log.info("已连接设备 %s", name)

    async def _ensure_ddi(self) -> bool:
        """探测 DDI 是否就绪。

        **刻意不主动挂载。** 实测（works.md §6 M0）本机 DDI
        ``com.apple.MobileAsset.DDI 27.1.266.1`` 已以 cryptex 形式在位，
        iOS 17+ 由系统自行托管，不存在「手动 mount 一份 DDI」的老流程。

        探测失败**不影响连接**：DVT 服务在 iOS 17+ 不依赖 usbmux DDI 挂载。
        把它降级成「尽力而为的探测」而不是硬性前置条件 ——
        硬卡这一步只会把一个无关的失败变成整条链路的阻塞点。
        """
        if self._lockdown is None:
            return False
        try:
            from pymobiledevice3.services.mobile_image_mounter import (
                MobileImageMounterService,
            )

            mounter = MobileImageMounterService(self._lockdown)
            mounted = await mounter.is_image_mounted("DeveloperDiskImage")
        except Exception as exc:
            # DDI 在 iOS 17+ 由 cryptex 托管，这个老接口可能直接抛 —— 无所谓
            log.debug("DDI 探测不可用（预期内）：%r", exc)
            mounted = False
        self._set_status(ddi_mounted=bool(mounted))
        return bool(mounted)

    async def _ensure_tunnel(self) -> RemoteServiceDiscoveryService:
        """建立 RSD 隧道，**只建一次**。

        顺序照抄上游 CLI 的降级链（``cli_common.make_rsd_dependency``）：
        macOS 先试 native（免 root），失败回落 userspace（纯 Python），
        两条都不行才由调用方决定要不要上 tunneld。
        """
        if self._rsd is not None:
            return self._rsd
        if self._tunnel_attempted:
            raise TunnelError("隧道此前已尝试建立但失败，不重复尝试"
                              "（隧道是进程级单例，重复建立必然失败）")
        self._tunnel_attempted = True

        errors: list[str] = []

        # ① macOS 上的 native 隧道（免 root）
        if platform.system() == "Darwin":
            try:
                from pymobiledevice3.remote import native_tunnel

                self._rsd = await native_tunnel.establish_native_rsd(serial=self.serial)
            except Exception as exc:
                errors.append(f"native: {exc}")
                log.debug("native 隧道不可用，回落 userspace：%r", exc)

        # ② 纯 Python userspace 隧道
        if self._rsd is None:
            try:
                from pymobiledevice3.remote import userspace_tunnel

                self._rsd = await userspace_tunnel.establish_userspace_rsd(
                    serial=self.serial, remotepairing_fallback=False,
                )
            except Exception as exc:
                errors.append(f"userspace: {exc}")

        if self._rsd is None:
            detail = "；".join(errors)
            self._set_status(tunnel_up=False, error=f"隧道建立失败：{detail}")
            raise TunnelError(
                f"RSD 隧道建立失败：{detail}\n"
                "iOS 17+ 的 DVT 必须走隧道。可试：`sudo pymobiledevice3 "
                "remote start-tunnel`（或让设备与本机在同一网络）。"
            )

        self._set_status(tunnel_up=True, phase=Phase.TUNNEL, error=None)
        log.info("RSD 隧道已建立")
        return self._rsd

    async def _open_channel(self) -> None:
        """打开 DTX 通道并挂上 :class:`LocationSimulation`。"""
        rsd = await self._ensure_tunnel()
        self._dvt = DvtProvider(rsd)
        await self._dvt.__aenter__()
        self._loc = LocationSimulation(self._dvt)
        await self._loc.__aenter__()
        self._set_status(phase=Phase.READY, error=None)
        log.info("DVT 通道已就绪")

    # ── 注入 ────────────────────────────────────────────────

    async def set_location(
        self,
        lat: float,
        lon: float,
        *,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> None:
        """注入一个坐标。

        带超时与**有限重试**。失败**一定抛异常，绝不静默吞掉** ——
        静默失败会让轨迹出现无声的空洞，而日志上一切正常（works.md §4.2.1）。

        但「整条轨迹不能因为一个点失败就全废」：
        因此 :class:`vrun.engine` 那层的调用方可以选择捕获 :class:`InjectionError`
        并计入 ``dropped``。**决定权在调用方，这里只负责如实报告。**

        :raises InjectionError: 超时且重试用尽
        """
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"纬度必须在 [-90, 90]，收到 {lat}")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"经度必须在 [-180, 180]，收到 {lon}")

        await self.connect()
        assert self._loc is not None  # connect() 后必然就绪

        tmo = self.set_timeout if timeout is None else timeout
        tries = self.set_retries if retries is None else retries

        last: Optional[BaseException] = None
        timed_out = False
        async with self._lock:
            for attempt in range(tries + 1):
                # ★ 计时**只包住这一次尝试**，不是整个重试循环。
                #   包住整个循环的话，重试过的那次会记成「一次很慢的注入」，
                #   而真相是「两次正常的注入」—— 分布会被彻底带偏。
                t0 = time.perf_counter()
                try:
                    await asyncio.wait_for(self._loc.set(lat, lon), timeout=tmo)
                except asyncio.TimeoutError as exc:
                    last = exc
                    timed_out = True
                    self._stats.attempt_ms.append((time.perf_counter() - t0) * 1000.0)
                    self._stats.timeouts += 1
                    log.warning("注入超时 (%.1fs, 第 %d/%d 次)：%.5f,%.5f",
                                tmo, attempt + 1, tries + 1, lat, lon)
                except Exception as exc:
                    # 非超时类错误（隧道掉了 / 通道断了）重试没有意义
                    last = exc
                    timed_out = False
                    self._stats.attempt_ms.append((time.perf_counter() - t0) * 1000.0)
                    break
                else:
                    self._stats.attempt_ms.append((time.perf_counter() - t0) * 1000.0)
                    self._status.injected += 1
                    # ★ 记住最后成功注入的位置 —— GUI 的绿圈就靠它。
                    #   只在**成功**时更新：失败的那次坐标设备根本没收到，
                    #   记下来会让绿圈指到一个手机上不存在的位置。
                    self._status.lat = lat
                    self._status.lon = lon
                    return

        self._status.dropped += 1
        self._set_status(error=f"注入失败：{last}")
        raise InjectionError(
            f"注入 {lat:.5f},{lon:.5f} 失败（已重试 {tries} 次）：{last}",
            timeout_exhausted=timed_out,
        ) from last

    # ── 清理 ────────────────────────────────────────────────

    async def clear(self) -> bool:
        """停止模拟，让设备回到真实 GPS。

        **幂等**：没连上、或通道已关，都直接返回 ``False`` 而不是抛异常。
        这是安全红线上的操作（works.md §2.3）——
        **清理动作在任何情况下都不许把异常抛给调用方**，
        否则「清理失败」会淹没真正的原因。

        :return: 本次是否真的发出了 ``clear``。
        """
        if self._loc is None:
            log.info("clear：通道未建立，无需清理")
            return False
        try:
            async with self._lock:
                await asyncio.wait_for(self._loc.clear(), timeout=self.set_timeout)
            # 清理后设备回到真实 GPS，而我们**读不到**真实位置。
            # 如实置空 —— 让 GUI 把绿圈退回「由用户起笔」，好过指着一个假位置。
            self._status.lat = None
            self._status.lon = None
            log.info("已清除虚拟定位")
            return True
        except Exception as exc:
            # ⚠️ 吞掉异常是**故意的**：见上方 docstring 的安全红线说明。
            #    异常记进日志，让调用方自己决定要不要重试。
            log.error("清除虚拟定位失败（已吞掉异常以免中断清理流程）：%r", exc)
            self._set_status(error=f"清除失败：{exc}")
            return False

    async def aclose(self) -> None:
        """关闭通道与隧道。**幂等**，异常一律吞掉。

        先清位置再关连接 —— 顺序反了的话，通道已经关了就没法再发 ``clear``。
        """
        await self.clear()
        for obj, label in (
            (self._loc, "LocationSimulation"),
            (self._dvt, "DvtProvider"),
            (self._rsd, "RSD 隧道"),
        ):
            if obj is None:
                continue
            try:
                await obj.__aexit__(None, None, None)
            except Exception as exc:
                log.debug("关闭 %s 时报错（忽略）：%r", label, exc)
            log.debug("已关闭 %s", label)
        self._loc = self._dvt = self._rsd = None
        self._tunnel_attempted = False
        self._set_status(connected=False, tunnel_up=False,
                         phase=Phase.DISCONNECTED)