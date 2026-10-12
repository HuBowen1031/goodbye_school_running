"""test_ipc.py —— JSON Lines 协议的测试。

★ 本文件存在的意义
    守住三条**容易在重构中被悄悄破坏**的性质：

    ① ``write_message`` 必须 **flush** —— 不 flush 时 GUI 会「按了开始两分钟后才动」，
       这种 bug 极难从代码上看出来，只能靠测试钉死。
    ② 中文必须 ``ensure_ascii=False`` —— 否则日志里全是 ``\\uXXXX``。
    ③ ``estimate`` 分支**不许 import 设备层** —— 这是「没插手机也能用面板」的
       技术前提（works.md §5.7）。一旦被破坏，① 纯计算层就会变成强依赖设备。
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vrun.ipc import (
    CMD_DEVICE_STATUS,
    CMD_ESTIMATE,
    CMD_RUN_START,
    COMMANDS,
    ERR_BAD_COMMAND,
    ERR_BAD_JSON,
    ERR_NOT_IMPLEMENTED,
    ProtocolError,
    dispatch,
    event,
    read_message,
    serve,
    write_message,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def _roundtrip(obj: dict) -> str:
    """写进内存流再读回来，返回那一行原文。"""
    buf = io.StringIO()
    write_message(buf, obj)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
#  编解码
# ══════════════════════════════════════════════════════════════

class TestCodec:
    def test_roundtrip(self) -> None:
        obj = {"command": CMD_ESTIMATE, "speed": 10.0}
        assert json.loads(_roundtrip(obj)) == obj

    def test_line_terminated(self) -> None:
        """一行一条 —— 没有它就没有「流式」。"""
        assert _roundtrip({"a": 1}).endswith("\n")

    def test_chinese_not_escaped(self) -> None:
        """★ 坑②：中文必须原样输出。"""
        line = _roundtrip({"event": "error", "message": "未获定位授权"})
        assert "未获定位授权" in line
        assert "\\u" not in line

    def test_flushed_immediately(self) -> None:
        """★ 坑①：不 flush 则调用方读不到。

        用一个「写完立刻检查缓冲」的自定义流来断言，
        而不是靠时序 —— 后者会偶发通过，等于没测。
        """
        class Probe(io.StringIO):
            flushed = False

            def flush(self) -> None:
                self.flsushed = True
                type(self).flushed = True

        probe = Probe()
        write_message(probe, {"a": 1})
        assert Probe.flushed is True

    def test_eof_returns_none(self) -> None:
        assert read_message(io.StringIO("")) is None

    def test_blank_lines_are_skipped(self) -> None:
        stream = io.StringIO('\n\n  \n{"command": "estimate"}\n')
        assert read_message(stream) == {"command": "estimate"}

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ProtocolError) as ei:
            read_message(io.StringIO("{not json}\n"))
        assert ei.value.code == ERR_BAD_JSON
        assert ei.value.hint          # 报错必须能指导下一步

    def test_non_object_raises(self) -> None:
        with pytest.raises(ProtocolError) as ei:
            read_message(io.StringIO("[1,2,3]\n"))
        assert ei.value.code == ERR_BAD_JSON


# ══════════════════════════════════════════════════════════════
#  estimate 分支 —— 面板的唯一数据源
# ══════════════════════════════════════════════════════════════

class TestEstimateHandler:
    @staticmethod
    def _ask(**payload) -> dict:
        msg = {"command": CMD_ESTIMATE, **payload}
        return asyncio.run(dispatch(msg))

    def test_returns_event_envelope(self) -> None:
        resp = self._ask(speed=10.0, distance=3000.0)
        assert resp["event"] == "estimate.result"

    def test_matches_estimate_module(self) -> None:
        """★ 协议层不许自己算 —— 必须与 ① 层的 estimate() 完全一致。"""
        from vrun.estimate import estimate

        resp = self._ask(speed=12.0, distance=3000.0)
        ref = estimate(12.0, 3000.0, "realistic", 1.0)
        assert resp["eta_s"] == pytest.approx(ref.eta_s)
        assert resp["sigma_v"] == pytest.approx(ref.sigma_v)
        assert resp["safety"] == ref.safety
        assert resp["verdict"] == ref.verdict

    def test_bands_serialisable(self) -> None:
        """tuple 不能直接过 json —— 必须转成 list。"""
        resp = self._ask(speed=10.0, noise="realistic")
        assert isinstance(resp["band_1sigma"], list)
        assert isinstance(resp["band_2sigma"], list)

    def test_harsh_band_is_null(self) -> None:
        """严苛档没有 2σ 安全带 —— 必须是 ``null`` 而不是空数组。

        GUI 靠这个区分「无安全带」与「有安全带」，语义不同。
        """
        resp = self._ask(speed=11.0, noise="harsh")
        assert resp["band_2sigma"] is None

    def test_does_not_import_device_layer(self) -> None:
        """★ 坑③：这条分支不许把设备层拖进来。

        works.md §5.7 要求「未连接 iPhone 时面板完全可用」。
        """
        import sys as _sys

        assert "vrun.device" not in _sys.modules
        assert "pymobiledevice3" not in _sys.modules
        self._ask(speed=10.0)

    def test_accepts_custom_profile(self) -> None:
        resp = self._ask(speed=10.0, profile={"t_w": 15.0, "r0": 0.9})
        assert resp["eta_s"] > 0

    def test_bad_args_become_error_event(self) -> None:
        with pytest.raises(ProtocolError):
            self._ask(speed=-1.0)


# ══════════════════════════════════════════════════════════════
#  分发与错误
# ══════════════════════════════════════════════════════════════

class TestDispatch:
    def test_missing_command_field(self) -> None:
        with pytest.raises(ProtocolError) as ei:
            asyncio.run(dispatch({"speed": 10.0}))
        assert ei.value.code == ERR_BAD_COMMAND

    def test_unknown_command(self) -> None:
        with pytest.raises(ProtocolError) as ei:
            asyncio.run(dispatch({"command": "run.teleport"}))
        assert ei.value.code == ERR_BAD_COMMAND
        assert "estimate" in ei.value.hint      # 提示里要列出可用命令

    def test_device_commands_report_not_implemented(self) -> None:
        """M2 未实现的命令必须**结构化报错**，不能静默成功。

        静默成功会让 GUI 停在「准备中」而没人知道出了什么事。
        """
        for cmd in (CMD_RUN_START, "route.set", "device.cleanup"):
            with pytest.raises(ProtocolError) as ei:
                asyncio.run(dispatch({"command": cmd}))
            assert ei.value.code == ERR_NOT_IMPLEMENTED

    def test_device_status_is_offline_safe(self) -> None:
        resp = asyncio.run(dispatch({"command": CMD_DEVICE_STATUS}))
        assert resp["event"] == "device"
        assert resp["connected"] is False

    def test_every_documented_command_has_a_handler(self) -> None:
        from vrun.ipc import HANDLERS

        assert set(COMMANDS) == set(HANDLERS)


# ══════════════════════════════════════════════════════════════
#  服务循环
# ══════════════════════════════════════════════════════════════

class TestServe:
    @staticmethod
    def _run(text: str) -> list[dict]:
        out = io.StringIO()
        asyncio.run(serve(io.StringIO(text), out))
        return [json.loads(line) for line in out.getvalue().splitlines() if line]

    def test_processes_several_commands(self) -> None:
        lines = self._run(
            '{"command": "estimate", "speed": 10.0}\n'
            '{"command": "estimate", "speed": 12.0}\n'
        )
        assert len(lines) == 2
        assert [ln["event"] for ln in lines] == ["estimate.result"] * 2

    def test_bad_line_does_not_kill_the_loop(self) -> None:
        """一条坏消息不能让服务死掉 —— 后面的命令还得能处理。"""
        lines = self._run(
            "{bad}\n"
            '{"command": "estimate", "speed": 10.0}\n'
        )
        assert lines[0]["event"] == "error"
        assert lines[0]["code"] == ERR_BAD_JSON
        assert lines[1]["event"] == "estimate.result"

    def test_unknown_command_yields_error_event(self) -> None:
        lines = self._run('{"command": "nope"}\n')
        assert lines[0]["code"] == ERR_BAD_COMMAND

    def test_eof_exits_cleanly(self) -> None:
        """GUI 退出 → 管道断开 → EOF → 正常返回 0。"""
        assert asyncio.run(serve(io.StringIO(""), io.StringIO())) == 0


# ══════════════════════════════════════════════════════════════
#  端到端：真的起一个子进程
# ══════════════════════════════════════════════════════════════

class TestServeSubprocess:
    """★ 模拟 GUI 的真实用法：起子进程、喂一行、读一行、关管道。"""

    def test_estimate_over_pipe(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "vrun", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env={"PYTHONPATH": str(SRC)},
        )
        try:
            proc.stdin.write('{"command": "estimate", "speed": 10.0, "distance": 3200}\n')
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp["event"] == "estimate.result"
            assert resp["eta_s"] == pytest.approx(1161.6, abs=0.5)
            assert resp["verdict"]["R1"] == "✓"
        finally:
            proc.stdin.close()
            # 管道关闭后侧车应当自行退出，不留孤儿进程
            assert proc.wait(timeout=15) == 0
