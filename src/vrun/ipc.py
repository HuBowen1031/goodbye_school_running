"""ipc.py —— JSON Lines 协议（GUI ⇄ 侧车）★

职责
    works.md §4.2 协议的一行一条编解码、命令分发表、以及侧车的 stdio 服务循环。
    **纯编排，不含业务公式** —— 任何数值都来自 :mod:`vrun.estimate` 等 ① 纯计算层。

为什么用 stdio 管道而不是 UNIX socket
    GUI 用 ``Process`` 启动侧车，生命周期天然绑定：
    **GUI 退出 → 管道断开 → 侧车读到 EOF → 自动清理**。
    少一个 socket 文件、少一层权限问题。

本轮（M1）做到哪一步
    ✅ 协议编解码、命令/事件名常量、``estimate`` 分支（**纯计算，离线可跑**）
    ⛔ ``run.*`` / ``device.*`` 需要设备层（M2），已注册但返回结构化「未实现」错误

    这不是半成品 —— ``estimate`` 恰好是 works.md §5 面板的唯一数据源，
    也就是说**滑块面板的全部数学现在就能端到端验证**，不依赖 iPhone。

三条容易踩的坑（都来自 file-manifest §4.3.1）
    ① **每行必须 flush。** 缓冲会让 ``tick`` 在管道里攒够 4 KB 才吐出来，
       GUI 上的表现是「按了开始以后两分钟才开始动」——极难排查。
    ② **中文必须 ``ensure_ascii=False``。** 否则日志里全是 ``\\uXXXX``，出问题时没法读。
    ③ **事件不带 id 配对。** 因为同一时刻只可能有一个会话
       （works.md §5.7 运行中锁定滑块），天然串行，不需要请求-响应配对。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Awaitable, Callable, Optional, TextIO

from .estimate import estimate as _estimate
from .profile import SpeedProfile

# ══════════════════════════════════════════════════════════════
#  协议常量
# ══════════════════════════════════════════════════════════════

# GUI → 侧车 的命令名（works.md §4.2）
CMD_ROUTE_SET = "route.set"
CMD_RUN_PREPARE = "run.prepare"
CMD_RUN_START = "run.start"
CMD_RUN_PAUSE = "run.pause"
CMD_RUN_RESUME = "run.resume"
CMD_RUN_STOP = "run.stop"
CMD_DEVICE_CLEANUP = "device.cleanup"
CMD_DEVICE_STATUS = "device.status"
CMD_ESTIMATE = "estimate"

COMMANDS: tuple[str, ...] = (
    CMD_ROUTE_SET, CMD_RUN_PREPARE, CMD_RUN_START,
    CMD_RUN_PAUSE, CMD_RUN_RESUME, CMD_RUN_STOP,
    CMD_DEVICE_CLEANUP, CMD_DEVICE_STATUS, CMD_ESTIMATE,
)

# 侧车 → GUI 的事件名
EV_TICK = "tick"
EV_PROGRESS = "progress"
EV_DEVICE = "device"
EV_ERROR = "error"
EV_ESTIMATE_RESULT = "estimate.result"

EVENTS: tuple[str, ...] = (
    EV_TICK, EV_PROGRESS, EV_DEVICE, EV_ERROR, EV_ESTIMATE_RESULT,
)

# 错误码
ERR_BAD_JSON = "bad_json"
ERR_BAD_COMMAND = "bad_command"
ERR_BAD_ARGS = "bad_args"
ERR_NOT_IMPLEMENTED = "not_implemented"


class ProtocolError(Exception):
    """协议层错误。**一定**要带上 ``hint`` —— 报错要能指导下一步动作。"""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_event(self) -> dict:
        return {"event": EV_ERROR, "code": self.code,
                "message": self.message, "hint": self.hint}


# ══════════════════════════════════════════════════════════════
#  编解码
# ══════════════════════════════════════════════════════════════

def read_message(stream: TextIO) -> Optional[dict]:
    """读一行 → dict。

    - **EOF** 返回 ``None``（调用方据此退出服务循环）
    - **空行**跳过 —— GUI 调试时容易多敲回车，不该因此崩掉
    - **JSON 非法**抛 :class:`ProtocolError`，由上层转成 ``error`` 事件

    ⚠️ 必须是**单行**读取。JSON Lines 的全部约定就压在这一行上，
    换成 ``readline()`` 之外的任何方式都会破坏流式语义。
    """
    while True:
        line = stream.readline()
        if line == "":                       # EOF
            return None
        line = line.strip()
        if not line:                          # 空行，跳过
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(
                ERR_BAD_JSON,
                f"收到非法 JSON：{exc}",
                "协议是一行一条 JSON。检查 GUI 是否把多个对象写进了同一行。",
            ) from exc
        if not isinstance(obj, dict):
            raise ProtocolError(
                ERR_BAD_JSON,
                f"期望 JSON 对象，收到 {type(obj).__name__}",
                "每行必须是一个对象，如 {\"command\": \"estimate\", ...}",
            )
        return obj


def write_message(stream: TextIO, obj: dict) -> None:
    """写一行 JSON + ``\\n`` 并 **flush**。

    flush 不是优化，是**正确性要求**（见模块 docstring 坑 ①）。
    """
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def event(name: str, **payload: Any) -> dict:
    """构造事件包：``{"event": 名字, ...}``。"""
    return {"event": name, **payload}


# ══════════════════════════════════════════════════════════════
#  分发
# ══════════════════════════════════════════════════════════════

Handler = Callable[[dict], Awaitable[Optional[dict]]]


async def _handle_estimate(payload: dict) -> Optional[dict]:
    """``estimate`` —— ★ 面板的唯一数据源，**纯计算，不碰设备**。

    载荷（works.md §4.2）：``{speed, distance, noise, sample_rate, profile, seed}``

    为什么要做成同步可测的一条分支：
    works.md §5.6 要求「拖动时用 GUI 的闭式解，松手 250 ms 后用这里的精确值」，
    而 §5.7 要求「没连 iPhone 时面板完全可用」。
    两者的技术前提就是这条分支**不 import 设备层**。
    """
    speed = float(payload.get("speed", 10.0))
    distance = float(payload.get("distance", 3000.0))
    noise = str(payload.get("noise", "realistic"))
    rate = float(payload.get("sample_rate", 1.0))
    seed = int(payload.get("seed", 0))

    # profile 允许 GUI 传自定义剖面参数；缺省就用 ① 层的默认档。
    raw_profile = payload.get("profile")
    try:
        profile = SpeedProfile(**raw_profile) if isinstance(raw_profile, dict) else None
        result = _estimate(speed_kmh=speed, distance_m=distance,
                           noise_level=noise, sample_rate=rate,
                           profile=profile, seed=seed)
    except (ValueError, TypeError) as exc:
        # ⚠️ 必须在**这里**转成协议错误。若放任 ValueError 逃逸，
        # serve() 的兜底会把它标成 "internal / 这是侧车的 bug，请反馈" ——
        # 但传错参数是**调用方**的问题，标错方向会让人白忙一场。
        raise ProtocolError(
            ERR_BAD_ARGS,
            f"参数不合法：{exc}",
            "speed > 0、distance > 0、sample_rate > 0；"
            "noise 只能是 off / realistic / harsh。",
        ) from exc

    return event(
        EV_ESTIMATE_RESULT,
        speed_kmh=result.speed_kmh,
        distance_m=result.distance_m,
        noise_level=result.noise_level,
        sample_rate=result.sample_rate,
        pace_s_per_km=result.pace_s_per_km,
        pace_str=result.pace_str,
        eta_naive_s=result.eta_naive_s,
        eta_s=result.eta_s,
        eta_closed_form_s=result.eta_closed_form_s,
        eta_str=result.eta_str,
        sigma_v=result.sigma_v,
        band_1sigma=list(result.band_1sigma) if result.band_1sigma else None,
        band_2sigma=list(result.band_2sigma) if result.band_2sigma else None,
        safety=result.safety,
        v_lo=result.v_lo,
        v_hi=result.v_hi,
        clamp_enabled=result.clamp_enabled,
        verdict=result.verdict,
    )


async def _handle_device_status(payload: dict) -> Optional[dict]:  # noqa: ARG001
    """``device.status`` —— M2 已实装：真实探测并回报。

    ⚠️ 分层纪律的关键动作在这里：设备层的 import 放在**函数体内**，
    不是模块顶层。这样 ``import vrun.ipc``（进而 ``vrun serve``）
    **不会**把 pymobiledevice3 拖进来 —— 没插手机时侧车照样能起，
    面板的数学仍然完全可用（works.md §5.7）。
    ``tests/test_device.py::test_pure_layer_never_imports_pymobiledevice3``
    是这条纪律的回归护栏。
    """
    try:
        from .device import DeviceBridge
    except ImportError as exc:
        return event(EV_DEVICE, connected=False, ddi_mounted=False,
                     tunnel_up=False, phase="disconnected",
                     error=f"设备层不可用：{exc}")

    bridge = _shared_bridge()
    try:
        await bridge.connect()
    except Exception as exc:
        # ★ 探测失败**不算协议错误**：GUI 需要看到「没连 + 为什么」，
        #   而不是收到一个 exception 事件。状态查询失败是常态，不是故障。
        return event(EV_DEVICE, connected=False, ddi_mounted=False,
                     tunnel_up=False, phase=bridge.status().phase,
                     device_name=bridge.status().device_name,
                     injected=bridge.status().injected,
                     dropped=bridge.status().dropped,
                     error=str(exc))
    return event(EV_DEVICE, **bridge.status().as_event())


async def _handle_device_cleanup(payload: dict) -> Optional[dict]:  # noqa: ARG001
    """``device.cleanup`` —— M2 已实装：立即清除虚拟定位。

    走 :class:`vrun.safety.SafetyGuard` 而不是直接 ``bridge.clear()``：
    幂等标记位是唯一的真相源，否则 GUI 每次启动都发一次 cleanup
    就会对着设备反复清。
    """
    try:
        from .device import DeviceBridge
        from .safety import SafetyGuard
    except ImportError as exc:
        raise ProtocolError(ERR_NOT_IMPLEMENTED,
                            f"设备层不可用：{exc}",
                            "确认已安装 pymobiledevice3。") from exc

    bridge = _shared_bridge()
    try:
        await bridge.connect()
    except Exception as exc:
        # 连不上 = 没有通道 = 没有位置可清，如实回报即可
        return event(EV_DEVICE, connected=False, tunnel_up=False,
                     phase=bridge.status().phase, cleared=False,
                     error=f"连不上设备，未执行清除：{exc}")

    guard = SafetyGuard(bridge)
    cleared = await guard.acleanup()
    return event(EV_DEVICE, cleared=cleared, **bridge.status().as_event())


#: 侧车进程内的**单一**设备桥与**单一**会话。
#:
#: 为什么必须是单例：
#:   ① 隧道是**进程级单例**（PyTCP 网络栈全局唯一），第二次建隧道会直接失败。
#:   ② works.md §5.7 已锁定 —— 同一时刻只可能有一个会话，天然串行。
_BRIDGE = None
_SESSION = None


def _shared_bridge():
    global _BRIDGE
    if _BRIDGE is None:
        from .device import DeviceBridge

        _BRIDGE = DeviceBridge()
    return _BRIDGE


def reset_shared_state() -> None:
    """清空进程内的单例桥与单例会话。**测试专用。**

    为什么需要它
        隧道是**进程级单例**（PyTCP 网络栈全局唯一）。测试里若让
        ``device.status`` 真的去连真机，会① 依赖「手机在不在」，
        ② 建完隧道又被下一条用例抢走，表现为**随机卡死**。

        所以设备层的单测一律先注入假 bridge（``_BRIDGE = FakeBridge()``），
        跑完调本函数还原 —— 测试之间彻底不共享设备状态。
    """
    global _BRIDGE, _SESSION
    _BRIDGE = None
    _SESSION = None


def _shared_session(emit):
    global _SESSION
    if _SESSION is None:
        from .session import RunSession

        _SESSION = RunSession(bridge=_shared_bridge(), emit=emit)
    return _SESSION


# ── run.* —— M2 实装 ──────────────────────────────────────
#
# emit 由 serve() 注入：把事件写成一行 JSON 推给 GUI。
# 「每行 flush」是硬要求（§4.3）—— 不 flush 会让 tick 在管道里攒满 4 KB
# 才吐出来，GUI 上的表现是「按了开始以后两分钟才开始动」。

async def _emit_event(name: str, payload: dict, out=None) -> None:
    stream = out if out is not None else _OUT_STREAM[0]
    if stream is None:
        return
    write_message(stream, event(name, **payload))


#: serve() 把输出流挂在这里。模块级单槽位，不是全局可变状态满天飞。
_OUT_STREAM: list = [None]


async def _handle_route_set(payload: dict) -> Optional[dict]:
    points = payload.get("points")
    if not isinstance(points, list):
        raise ProtocolError(ERR_BAD_ARGS, "route.set 需要 `points` 数组",
                            "格式：{\"command\":\"route.set\",\"points\":[[lat,lon],…]}")
    try:
        sess = _shared_session(_make_emitter())
        sess.set_route(points)
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc),
                            "路线至少 2 个点，且必须先 stop/cleanup。") from exc
    return event(EV_PROGRESS, route_points=len(points))


async def _handle_run_prepare(payload: dict) -> Optional[dict]:
    sess = _shared_session(_make_emitter())
    try:
        return event(EV_PROGRESS, **(await sess.prepare()))
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc),
                            "先发 route.set 再发 run.prepare。") from exc


async def _handle_run_start(payload: dict) -> Optional[dict]:
    sess = _shared_session(_make_emitter())
    try:
        _apply_config(sess, payload)
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc)) from exc
    try:
        return event(EV_PROGRESS, **(await sess.start()))
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc),
                            "必须先 run.prepare 再 run.start。") from exc


async def _handle_run_pause(payload: dict) -> Optional[dict]:  # noqa: ARG001
    sess = _shared_session(_make_emitter())
    try:
        return event(EV_PROGRESS, **(await sess.pause()))
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc)) from exc


async def _handle_run_resume(payload: dict) -> Optional[dict]:  # noqa: ARG001
    sess = _shared_session(_make_emitter())
    try:
        return event(EV_PROGRESS, **(await sess.resume()))
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc)) from exc


async def _handle_run_stop(payload: dict) -> Optional[dict]:  # noqa: ARG001
    sess = _shared_session(_make_emitter())
    try:
        return event(EV_PROGRESS, **(await sess.stop()))
    except Exception as exc:
        raise ProtocolError(ERR_BAD_ARGS, str(exc)) from exc


def _make_emitter():
    async def emit(name: str, payload: dict) -> None:
        await _emit_event(name, payload)
    return emit


def _apply_config(sess, payload: dict) -> None:
    """把 ``run.start`` 的载荷套到会话上。"""
    from .noise import GPSNoise
    from .profile import SpeedProfile

    kwargs: dict = {}
    if "profile" in payload and isinstance(payload["profile"], dict):
        raw = payload["profile"]
        allowed = {"t_w", "r0", "delta", "f", "sigma_v", "tau_v", "clamp_enabled"}
        filtered = {k: v for k, v in raw.items() if k in allowed}
        kwargs["profile"] = SpeedProfile(**filtered) if filtered else SpeedProfile()

    if "noise" in payload:
        level = payload["noise"]
        rate = float(payload.get("sample_rate", 1.0) or 1.0)
        try:
            kwargs["noise"] = GPSNoise(level=level, sample_rate=rate,
                                       seed=int(payload.get("seed", 0) or 0))
        except Exception as exc:
            raise ProtocolError(ERR_BAD_ARGS, f"noise 参数非法：{exc}") from exc
    if "sample_rate" in payload:
        kwargs["sample_rate"] = float(payload["sample_rate"])
    sess.configure(**kwargs)


def _make_unavailable(name: str, milestone: str = "M2") -> Handler:
    """为需要设备层的命令生成一个「结构化未实现」处理器。

    刻意**不静默成功**。GUI 若发了 ``run.start`` 却收到空响应，
    界面会停在「准备中」而没人知道出了什么事（works.md §4.2 的设计要点）。
    """

    async def handler(payload: dict) -> Optional[dict]:  # noqa: ARG001
        raise ProtocolError(
            ERR_NOT_IMPLEMENTED,
            f"命令 `{name}` 属于{ milestone}（设备层），尚未实现。",
            "当前可用的命令：estimate（面板预览）、device.status（状态查询）。",
        )

    handler.__name__ = f"handle_{name.replace('.', '_')}"
    return handler


HANDLERS: dict[str, Handler] = {
    CMD_ESTIMATE: _handle_estimate,
    CMD_DEVICE_STATUS: _handle_device_status,
    CMD_ROUTE_SET: _handle_route_set,
    CMD_RUN_PREPARE: _handle_run_prepare,
    CMD_RUN_START: _handle_run_start,
    CMD_RUN_PAUSE: _handle_run_pause,
    CMD_RUN_RESUME: _handle_run_resume,
    CMD_RUN_STOP: _handle_run_stop,
    CMD_DEVICE_CLEANUP: _handle_device_cleanup,
}


async def dispatch(message: dict) -> Optional[dict]:
    """把一条命令派发给对应处理器。

    返回 ``None`` 表示**这条命令没有响应**（合法：``run.pause`` 之类只需回 ack）。
    抛 :class:`ProtocolError` 表示**出错** —— 由 :func:`serve` 转成 ``error`` 事件。
    """
    name = message.get("command")
    if name is None:
        raise ProtocolError(
            ERR_BAD_COMMAND,
            "消息里没有 `command` 字段。",
            "命令格式：{\"command\": \"estimate\", \"speed\": 10.0, ...}",
        )
    handler = HANDLERS.get(name)
    if handler is None:
        raise ProtocolError(
            ERR_BAD_COMMAND,
            f"未知命令 {name!r}。",
            f"可选命令：{', '.join(COMMANDS)}",
        )
    return await handler(message)


# ══════════════════════════════════════════════════════════════
#  服务循环
# ══════════════════════════════════════════════════════════════

async def serve(
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
) -> int:
    """读 stdin 的命令、向 stdout 回事件，直到 EOF。

    返回值是**进程退出码**。

    ⚠️ 管道断开（GUI 被杀 / 崩溃）会让 ``readline()`` 立刻返回 EOF，
    服务循环随即退出。这是 works.md §4.2 选 stdio 的理由：
    **生命周期天然绑定，不留孤儿进程。**
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    # 会话事件（tick / progress）要写到这里。必须挂上，
    # 否则 run.start 之后 tick 全部丢进黑洞。
    _OUT_STREAM[0] = stdout

    while True:
        try:
            message = read_message(stdin)
        except ProtocolError as exc:
            write_message(stdout, exc.to_event())
            continue

        if message is None:          # EOF —— 对端关闭
            return 0

        try:
            response = await dispatch(message)
        except ProtocolError as exc:
            write_message(stdout, exc.to_event())
            continue
        except Exception as exc:     # noqa: BLE001 —— 兜底，绝不让服务死掉
            write_message(stdout, ProtocolError(
                "internal", f"处理 {message.get('command')!r} 时异常：{exc}",
                "这是侧车的 bug，请把上面的 message 一并反馈。",
            ).to_event())
            continue

        if response is not None:
            write_message(stdout, response)
