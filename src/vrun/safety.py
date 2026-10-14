"""safety.py — 三保险清理 ★

职责
    works.md §2.3 的**三保险**，全部指向同一个幂等的清理动作。

为什么这个文件最不能出错
    注入的位置会**一直保持**，直到显式清除或设备重连。
    程序异常退出而没清理，博文的 iPhone 定位就「卡」在校园那个虚拟点上 ——
    导航 / 天气 / 「查找」全部跟着偏，而且要直到手机重连才恢复。
    所以这里的每一个设计决定都倒向「宁可多清一次」。

三保险（works.md §2.3）

    1. ``try/finally``    —— 正常与异常路径   → :meth:`SafetyGuard.acleanup`
    2. ``atexit``         —— 解释器正常退出   → :meth:`_on_atexit`
    3. ``signal``         —— Ctrl-C 与 kill  → :meth:`_on_signal`

全部经由 :meth:`cleanup`，而 :meth:`cleanup` 被 ``_cleaned`` 标记位保护 ——
**这是本文件的核心**。三保险在某些路径上必然重复触发
（例如 Ctrl-C 同时走 ``finally`` 与 signal handler），
没有幂等保护就会对已清除的设备再发一次 ``clear``。

★ 本文件有一个绕不开的技术难点，值得先说清楚
    清理动作 :meth:`DeviceBridge.clear` 是 **async** 的，而
    ``atexit`` 处理器和 signal 处理器都是**同步**的。
    而且 ——
        DTX 的 ``stopLocationSimulation`` 虽然 ``expects_reply=False``
        （不需要等设备回应），但它**仍然要通过主 event loop 把字节写出去**。
    所以到了 atexit / signal 这两条路上，主循环很可能已经死了
    （这恰恰是我们需要保险的场景）。此时**原通道无法使用**。

    本模块的解法是二选一，且**都记进日志、不静默**：

    ① 主循环还活着 → 把协程丢回主循环，等它跑完（限时）。
    ② 主循环已死 → 起一个**短命子进程**执行 ``python -m vrun panic-clear``。

    ② 就是 works.md §2.3 要求 ``scripts/panic-clear.sh`` 的原因 ——
    它不是「可选的救援脚本」，而是 ``SafetyGuard`` 在最坏情况下**自动**走的那条路。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import signal
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

__all__ = ["SafetyGuard"]


class SafetyGuard:
    """三保险 + 幂等清理。

    用法::

        guard = SafetyGuard(bridge)
        guard.arm()
        try:
            await run_session()
        finally:
            await guard.acleanup()      # 第①条保险

    ⚠️ 必须在**装好保险之后**才做任何可能注入位置的事。
    """

    #: 同步清理路径等待主循环的秒数。超时就转子进程。
    SYNC_TIMEOUT = 5.0
    #: 子进程清理的超时。
    SUBPROCESS_TIMEOUT = 30.0

    def __init__(self, bridge) -> None:
        """
        :param bridge: 任何带 ``async clear() -> bool`` 的对象。
            刻意用鸭子类型而非 ``DeviceBridge`` ——
            这样测试能塞假 bridge，而测试里塞假对象**不依赖 iPhone**。
        """
        self._bridge = bridge
        self._cleaned = False
        self._armed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._prev_handlers: dict[int, object] = {}

    # ── 只读状态 ────────────────────────────────────────────

    @property
    def cleaned(self) -> bool:
        """清理是否**已经**执行过。"""
        return self._cleaned

    @property
    def armed(self) -> bool:
        return self._armed

    # ── 装保险 ──────────────────────────────────────────────

    def arm(self) -> None:
        """注册 atexit + SIGINT / SIGTERM 处理器。**可重复调用，只生效一次。**

        必须在 event loop **运行中**调用（在协程里），才能抓到 loop 句柄；
        在事件循环外调用也能工作，只是第②条保险会退化成子进程。
        """
        if self._armed:
            return

        atexit.register(self._on_atexit)

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
            log.debug("arm() 在事件循环外调用，信号保险将走子进程兜底")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._prev_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._make_signal_handler(sig))
            except (ValueError, OSError, RuntimeError):
                # 非主线程，或平台不支持该信号 —— 不影响另外两条保险
                log.debug("无法安装 %s 处理器（可能不在主线程）", sig)

        self._armed = True
        log.info("三保险已就位")

    # ── 清理 ────────────────────────────────────────────────

    async def acleanup(self) -> bool:
        """**异步**清理。正常路径（``finally``）用这个。

        :return: 本次是否真的执行了清理。重复调用返回 ``False``。
        """
        if self._cleaned:
            return False
        self._cleaned = True      # ★ 先置位：宁可漏清一次，也不重复清
        try:
            await self._bridge.clear()
        except Exception as exc:
            # 到这一步已经没什么能救的了 —— 但仍要记下来，
            # 因为这是「手机可能卡住了」的唯一线索。
            log.error("清理过程抛出异常：%r", exc)
            return False
        return True

    def cleanup(self) -> bool:
        """**同步**清理入口。atexit / signal 走这里。

        行为见模块 docstring 的「★ 技术难点」：能借到主循环就借，借不到
        就起短命子进程。

        :return: 本次是否真的执行了清理。
        """
        if self._cleaned:
            return False
        self._cleaned = True
        return self._cleanup_without_loop()

    def _cleanup_without_loop(self) -> bool:
        """在**没有可用 event loop** 的上下文里把清理做掉。"""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._bridge.clear(), loop,
                )
                fut.result(timeout=self.SYNC_TIMEOUT)
                log.info("清理完成（借主循环执行）")
                return True
            except Exception as exc:
                log.warning("借主循环清理失败，转子进程：%r", exc)

        return self._cleanup_via_subprocess()

    def _cleanup_via_subprocess(self) -> bool:
        """起一个短命子进程执行 ``python -m vrun panic-clear``。

        这是**最后一道保险**。它的价值恰恰在于：主循环已经死了的场合，
        原通道必然不可用，只有新进程能建新隧道去清。
        """
        log.warning("主循环不可用，改由子进程执行 panic-clear")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "vrun", "panic-clear"],
                timeout=self.SUBPROCESS_TIMEOUT,
                capture_output=True,
                text=True,
                env=self._subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            log.error("panic-clear 子进程超时（%ss）—— "
                      "请手动执行 scripts/panic-clear.sh", self.SUBPROCESS_TIMEOUT)
            return False
        except Exception as exc:
            log.error("panic-clear 子进程起不来：%r", exc)
            return False

        if proc.returncode != 0:
            log.error("panic-clear 失败（退出码 %d）：%s",
                      proc.returncode, (proc.stderr or "").strip()[:500])
            return False
        log.info("panic-clear 子进程执行成功")
        return True

    def _subprocess_env(self) -> dict[str, str]:
        """构造子进程环境，**必须把当前 ``sys.path`` 里的源码目录带出去**。

        实测踩到（2026-10-14）：在源码树下直接 ``python -m vrun panic-clear``
        会报 ``No module named vrun``，因为 ``src/`` 只在
        ``conftest.py`` 里被塞进 ``sys.path``，**子进程看不到**。
        而这恰恰是「程序崩了之后才跑」的场景 —— 平时根本不会暴露。

        保险要是自己也不保险，就毫无意义。所以这里显式拼 PYTHONPATH。
        """
        import os

        env = dict(os.environ)
        # 只带非标准库路径（含 vrun 所在的那一层）
        src_paths = [p for p in sys.path
                     if p and p not in sys.stdlib_module_names and os.path.isdir(p)]
        if src_paths:
            existing = env.get("PYTHONPATH")
            joined = os.pathsep.join(src_paths)
            env["PYTHONPATH"] = f"{joined}{os.pathsep}{existing}" if existing else joined
        return env

    # ── 保险 ②：atexit ──────────────────────────────────────

    def _on_atexit(self) -> None:
        if self._cleaned:
            return
        log.info("解释器退出，执行清理")
        self.cleanup()

    # ── 保险 ③：signal ──────────────────────────────────────

    def _make_signal_handler(self, signum: int):
        def handler(signum_: int, frame) -> None:   # noqa: ARG001
            self._handle_signal(signum_)

        return handler

    def _handle_signal(self, signum: int) -> None:
        """信号处理器的实际逻辑。

        ⚠️ 信号上下文里**只做最少的事**：置位 + 尽力清理 + 交还控制权。
        不做日志 flush、不做复杂异常处理 —— 那些在信号上下文里都不安全。

        :param rethrow: 置 ``False`` 可只跑清理不重抛信号（测试用）。
        """
        log.info("收到信号 %s，执行清理", signum)
        try:
            self.cleanup()
        except Exception as exc:
            log.error("信号处理中清理失败：%r", exc)

        # 交还默认处理权并重新抛出，让进程以正确的退出码结束。
        # 不这样做的话 Ctrl-C 会被「吃掉」，用户以为程序还活着。
        try:
            signal.signal(signum, self._prev_handlers.get(signum, signal.SIG_DFL))
            signal.raise_signal(signum)
        except Exception as exc:
            log.error("重新抛出信号失败：%r", exc)

    # ── 测试辅助 ────────────────────────────────────────────

    def _handle_signal_no_rethrow(self, signum: int = signal.SIGINT) -> None:
        """只跑清理、不重抛信号。测试专用。"""
        if self._cleaned:
            return
        self.cleanup()

    def _restore_handlers(self) -> None:
        """把保险**整体拆除**：signal 处理器还原 + atexit 注销。

        正常流程用不到它（进程要退出了，保险本来就要留着）。
        但测试里必须能拆：否则上一个测试 arm 的 guard 会在**解释器退出时**
        真的触发第②条保险，拖出一个真子进程 —— 测试输出被污染，
        而且它是在测别的用例时炸的，排查起来极其难。
        """
        for sig, prev in self._prev_handlers.items():
            try:
                signal.signal(sig, prev)  # type: ignore[arg-type]
            except (ValueError, OSError, RuntimeError):
                pass
        self._prev_handlers.clear()
        try:
            atexit.unregister(self._on_atexit)
        except Exception:
            pass
        self._armed = False