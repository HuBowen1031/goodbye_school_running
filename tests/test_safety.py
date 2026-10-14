"""test_safety.py — SafetyGuard 的幂等与三保险

**不依赖 iPhone，也不需要 pymobiledevice3。**
:class:`SafetyGuard` 用鸭子类型接收 bridge，塞个假对象即可 ——
这也是它不硬绑 ``DeviceBridge`` 类型的原因（见 safety.py 的 ``__init__`` 注释）。

为什么这些测试值钱
    清理漏了，博文的 iPhone 定位会一直卡在校园的虚拟点上。
    而三保险在某些路径上**必然重复触发**（Ctrl-C 同时走 finally 与 signal handler），
    幂等标记位就是防这个的 —— 它必须被测试钉死。

⚠️ 刻意**不用 pytest-asyncio** —— 本项目不为测试引入新依赖
   （file-manifest §4.5 依赖纪律）。惯例见 ``tests/test_ipc.py``：
   同步测试函数里用 ``asyncio.run()`` 包。
"""

from __future__ import annotations

import asyncio
import signal
import subprocess

from vrun.safety import SafetyGuard


class FakeBridge:
    """只提供 ``async clear() -> bool`` 的假 bridge。"""

    def __init__(self, fail: bool = False) -> None:
        self.clear_calls = 0
        self.fail = fail

    async def clear(self) -> bool:
        self.clear_calls += 1
        if self.fail:
            raise RuntimeError("假失败")
        return True


def _cleanup(guard: SafetyGuard, times: int = 1) -> list[bool]:
    """在**一个**事件循环里连续调 ``acleanup`` 若干次，返回每次的返回值。

    单测里不能给每个 await 单独开循环：``SafetyGuard`` 在 ``arm()`` 时会抓
    loop 句柄，跨循环调用会掩盖真实行为。
    """

    async def go() -> list[bool]:
        return [await guard.acleanup() for _ in range(times)]

    return asyncio.run(go())


# ══════════════════════════════════════════════════════════════
#  幂等 —— 本文件的核心
# ══════════════════════════════════════════════════════════════

def test_cleanup_is_idempotent():
    """★ 第二次及以后的 acleanup() 必须返回 False 且**不再调 clear**。"""
    bridge = FakeBridge()
    guard = SafetyGuard(bridge)

    assert _cleanup(guard, 3) == [True, False, False]
    assert bridge.clear_calls == 1, "幂等被破坏：clear 被调用了多次"


def test_sync_cleanup_is_idempotent():
    """同步入口（atexit / signal）同样幂等。"""
    bridge = FakeBridge()
    guard = SafetyGuard(bridge)
    # `_loop` 为 None → 走子进程路径；这里用 monkeypatch 隔离掉真实副作用
    guard._cleaned = True      # 先置位，只验证第二次调用不再动作
    assert guard.cleanup() is False
    assert bridge.clear_calls == 0


def test_mixed_paths_only_clean_once():
    """★ finally + atexit + signal 三条路都触发过，也只能清一次。

    这是真实故障模式：Ctrl-C 会先走 signal handler，随后 ``finally`` 又跑一遍。
    """
    bridge = FakeBridge()
    guard = SafetyGuard(bridge)

    _cleanup(guard, 1)               # ① finally
    guard._on_atexit()               # ② atexit
    guard._handle_signal_no_rethrow()  # ③ signal

    assert bridge.clear_calls == 1


def test_cleaned_flag_flips():
    bridge = FakeBridge()
    guard = SafetyGuard(bridge)
    assert guard.cleaned is False
    _cleanup(guard)
    assert guard.cleaned is True


# ══════════════════════════════════════════════════════════════
#  失败路径
# ══════════════════════════════════════════════════════════════

def test_clear_raising_does_not_propagate():
    """clear 抛异常**不能**把异常甩给调用方。

    安全红线上的清理动作一旦抛异常，就会把真正的原因淹没 ——
    而且调用方（``finally`` 块）会因此再抛一次，反过来盖掉原始异常。
    """
    bridge = FakeBridge(fail=True)
    guard = SafetyGuard(bridge)

    assert _cleanup(guard) == [False]          # 返回 False，不抛
    assert guard.cleaned is True                # 仍然置位，不重试
    assert bridge.clear_calls == 1


def test_flag_set_before_clear_so_failure_does_not_retry():
    """★ 先置位再清理。

    顺序反了的话：clear 失败 → 标记没置位 → 三保险每条路都会重试一次
    → 对着一台可能已经断开的设备狂发 clear。
    """
    bridge = FakeBridge(fail=True)
    guard = SafetyGuard(bridge)

    _cleanup(guard, 3)
    assert bridge.clear_calls == 1, "失败后仍在重试"


# ══════════════════════════════════════════════════════════════
#  arm()
# ══════════════════════════════════════════════════════════════

def test_arm_is_idempotent():
    """重复 arm() 不能装两套处理器。"""
    guard = SafetyGuard(FakeBridge())

    async def go():
        guard.arm()
        before = _handler_count()
        guard.arm()
        guard.arm()
        return before, _handler_count()

    before, after = asyncio.run(go())

    assert guard.armed is True
    assert before == after, "重复 arm 改变了处理器数量"
    guard._restore_handlers()


def _handler_count() -> int:
    return sum(1 for s in (signal.SIGINT, signal.SIGTERM)
               if callable(signal.getsignal(s)))


def test_arm_captures_running_loop():
    """在事件循环里 arm() 必须抓到 loop，否则②③保险要退化成子进程。"""
    guard = SafetyGuard(FakeBridge())

    async def go():
        guard.arm()
        assert guard._loop is asyncio.get_running_loop()

    asyncio.run(go())
    guard._restore_handlers()


def test_arm_outside_loop_is_safe():
    """事件循环外 arm() 也不能炸 —— 只是退化。"""
    guard = SafetyGuard(FakeBridge())
    guard.arm()
    assert guard._loop is None
    assert guard.armed is True
    guard._restore_handlers()


# ══════════════════════════════════════════════════════════════
#  最后一道保险：子进程
#
#  atexit / signal 到达时主循环往往已经死了，原 DTX 通道随之失效 ——
#  此时只有新进程能建新隧道去清。这就是 works.md §2.3 要求
#  scripts/panic-clear.sh 的原因。
# ══════════════════════════════════════════════════════════════

class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def test_falls_back_to_subprocess_when_loop_dead(monkeypatch):
    """★ 主循环不可用时必须转子进程 panic-clear。"""
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: (calls.append((cmd, kw)), _FakeProc())[1])

    guard = SafetyGuard(FakeBridge())
    guard._loop = None                       # 模拟主循环不可用
    assert guard.cleanup() is True

    assert len(calls) == 1
    cmd = calls[0][0]
    assert cmd[1:] == ["-m", "vrun", "panic-clear"]
    assert "timeout" in calls[0][1]


def test_subprocess_gets_pythonpath(monkeypatch):
    """★ 子进程必须能 import 到 vrun。

    实测踩到：``python -m vrun panic-clear`` 在没有把 ``src/`` 放进
    ``PYTHONPATH`` 的环境里会直接 `No module named vrun` ——
    而这恰恰是「程序崩了之后才跑」的场景，平时根本不会暴露。
    保险要是自己也不保险，就毫无意义。
    """
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: (calls.append((cmd, kw)), _FakeProc())[1])

    guard = SafetyGuard(FakeBridge())
    guard._loop = None
    guard.cleanup()

    env = calls[0][1].get("env")
    assert env is not None, "子进程没传 env"
    assert env.get("PYTHONPATH"), "PYTHONPATH 没带出去，源码树下必然 import 失败"


def test_subprocess_failure_is_reported(monkeypatch):
    """子进程失败要如实返回 False，不能假装成功。"""
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: _FakeProc(returncode=2, stderr="boom"))
    guard = SafetyGuard(FakeBridge())
    guard._loop = None
    assert guard.cleanup() is False


def test_subprocess_timeout_is_reported(monkeypatch):
    def timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", timeout_run)
    guard = SafetyGuard(FakeBridge())
    guard._loop = None
    assert guard.cleanup() is False


def test_uses_live_loop_when_available(monkeypatch):
    """主循环还活着时不该起子进程 —— 借循环更可靠也更快。"""
    spawned = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (spawned.append(cmd), _FakeProc())[1],
    )

    bridge = FakeBridge()
    guard = SafetyGuard(bridge)

    async def go():
        guard.arm()                          # 在循环内 arm → 抓到 loop
        first = await guard.acleanup()
        second = await guard.acleanup()
        return first, second

    first, second = asyncio.run(go())

    assert first is True and second is False
    assert not spawned, "主循环可用时不该起子进程"
    guard._restore_handlers()


def test_atexit_does_nothing_after_cleanup():
    """已清理过之后 atexit 不该再动 —— 解释器退出路径同样幂等。"""
    bridge = FakeBridge()
    guard = SafetyGuard(bridge)
    _cleanup(guard)
    guard._on_atexit()
    assert bridge.clear_calls == 1