"""cli.py — 命令行入口。

用法（conda 不在非交互 shell 的 PATH 上，必须用全路径或 ``conda run``）::

    cd <项目根>
    PYTHONPATH=src /Users/yuanjun/miniconda3/bin/conda run -n vrun \\
        python -m vrun estimate --speed 10 --distance 3000

三个子命令都不需要 iPhone：

    estimate   单点预览（配速 / ETA / 安全带 / R1–R5 判定）
    golden     works.md §5.4 的完整基准表（11 行，可导出 JSON）
    simulate   离线跑完整轨迹（假时钟，秒级完成）

设备相关子命令（``device-status`` / ``panic-clear``）**延迟导入** :mod:`vrun.device`，
未实现或环境缺失时给出明确提示，而不是让整个 CLI 崩掉。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from .estimate import (
    D_MIN_M,
    EstimateResult,
    estimate,
    golden_rows,
)
from .noise import GPSNoise
from .profile import PRESETS, SpeedProfile
from .route import Route, circle_route

# 江安校区附近的合成起点。**只用于离线演示**：真实使用时应由 GUI 从地图取点。
DEFAULT_ORIGIN: tuple[float, float] = (30.5535, 103.9910)

#: 噪声档位的合法取值。
#:
#: ⚠️ 必须从 :class:`~vrun.noise.GPSNoise` 推导，**不能手写字符串**。
#:    soak 的 ``--noise`` 曾手写成 ``["off", "realistic", "strict"]``，
#:    而 ``LEVELS`` 里的键是 ``harsh`` —— argparse 放行，引擎在几百毫秒后
#:    才抛「未知噪声档位」。错误信息指向完全无关的地方。
#:    从数据源推导让这类不同步**不可能发生**。
NOISE_LEVELS: list[str] = sorted(GPSNoise.LEVELS)


# ══════════════════════════════════════════════════════════════
#  输出辅助
# ══════════════════════════════════════════════════════════════

_COLOR_TAG = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _band_str(band: Optional[tuple[float, float]]) -> str:
    if band is None:
        return "无"
    return f"[{band[0]:.2f}, {band[1]:.2f}]"


def _print_result(r: EstimateResult) -> None:
    print(f"速度        {r.speed_kmh:.2f} km/h   配速 {r.pace_str} /km")
    print(f"目标距离    {r.distance_m / 1000:.2f} km")
    print(f"纯运动      {r.eta_naive_s:8.1f} s")
    print(f"修正 ETA    {r.eta_s:8.1f} s   (数值积分，权威)   {r.eta_str}")
    print(f"修正 ETA    {r.eta_closed_form_s:8.1f} s   (一阶近似，GUI 拖动用)")
    delta = r.eta_s - r.eta_closed_form_s
    print(f"两者差值    {delta:+8.2f} s   {'⚠️ 超过 1 s，GUI 应高亮' if abs(delta) > 1.0 else '一致'}")
    print()
    print(f"噪声档位    {r.noise_level}   采样率 {r.sample_rate} Hz")
    print(f"表观抖动 σ  {r.sigma_v:.3f} km/h")
    print(f"2σ 区间     [{r.v_lo:.1f}, {r.v_hi:.1f}] km/h   （规则窗口 6–17）")
    print(f"安全带 1σ   {_band_str(r.band_1sigma)}")
    print(f"安全带 2σ   {_band_str(r.band_2sigma)}")
    print(f"判定        {_COLOR_TAG.get(r.safety, '?')} {r.safety}")
    print(f"夹紧        {'开' if r.clamp_enabled else '关（贴线设定）'}")
    print()
    print("R1–R5 判定预期：")
    for key in ("R1", "R2", "R3", "R4", "R5"):
        print(f"  {key}  {r.verdict.get(key, '?')}")


# ══════════════════════════════════════════════════════════════
#  子命令
# ══════════════════════════════════════════════════════════════

def _cmd_estimate(args: argparse.Namespace) -> int:
    profile = SpeedProfile(v_bar=args.speed)
    result = estimate(
        speed_kmh=args.speed,
        distance_m=args.distance,
        noise_level=args.noise,
        sample_rate=args.rate,
        profile=profile,
    )
    _print_result(result)
    if args.json:
        print()
        print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2))
    return 0


def _cmd_golden(args: argparse.Namespace) -> int:
    rows = golden_rows(
        distance_m=args.distance,
        noise_level=args.noise,
        sample_rate=args.rate,
    )

    header = (
        f"{'v̄':>6} {'配速':>6} {'纯运动':>9} {'修正ETA':>9} "
        f"{'2σ 表观区间':>16} {'判定':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.speed_kmh:>6.1f} {r.pace_str:>6} {r.eta_naive_s:>9.1f} "
            f"{r.eta_s:>9.1f} {'[' + f'{r.v_lo:.1f}, {r.v_hi:.1f}' + ']':>16} "
            f"{_COLOR_TAG.get(r.safety, '?'):>6}"
        )

    if args.json:
        payload = {
            "meta": {
                "distance_m": args.distance,
                "noise_level": args.noise,
                "sample_rate": args.rate,
                "profile": "default (delta=-1.5%, f=+5%, t_w=30s, r0=0.8)",
            },
            "rows": [_result_to_dict(r) for r in rows],
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n已写出 {args.json}")
    return 0


def _resolve_route(args: argparse.Namespace):
    """按参数解析出路线，返回 ``(route, 提示信息或 None)``。

    ⚠️ ``--distance`` 的默认值是 None 而不是 3000。这个区别很重要：
    缺省语义必须是「**保持路线原长**」。若默认值取 3000，
    ``simulate --route X.json`` 会把用户画好的路线**无声地**缩放掉 ——
    用户没要求，却已经发生了，且界面上看不出任何提示。
    """
    from .route import Route

    if args.route:
        route = Route.from_json(args.route)
        original_m = route.length()
        if args.distance is not None and abs(original_m - args.distance) > 1.0:
            route = route.scaled_to(args.distance)
            return route, f"路线缩放    {original_m:.1f} m → {route.length():.1f} m（由 --distance 指定）"
        return route, None
    return _synthetic_route(D_MIN_M if args.distance is None else args.distance), None


def _build_engine(args, route):
    """按参数装配好引擎（纯计算，不碰设备）。"""
    from .engine import TrackEngine

    profile = SpeedProfile(v_bar=args.speed)
    noise = GPSNoise(level=args.noise, sample_rate=args.rate, seed=args.seed)
    return TrackEngine(route, profile, noise, sample_rate=args.rate)


def _cmd_simulate(args: argparse.Namespace) -> int:
    route, note = _resolve_route(args)
    if note:
        print(note)
    engine = _build_engine(args, route)

    print(f"路线长度    {route.length():.1f} m（{len(route)} 个顶点）")
    print(f"速度        {args.speed} km/h    噪声 {args.noise}    种子 {args.seed}")
    print("正在离线仿真（假时钟，不占真实时间）…")

    samples = engine.simulate_offline()
    if not samples:
        print("没有产出任何采样点", file=sys.stderr)
        return 1

    last = samples[-1]
    duration = last.t
    mean = (last.s / duration) * 3.6 if duration > 0 else 0.0

    print()
    print(f"采样点数    {len(samples)}")
    print(f"总时长      {duration:.1f} s")
    print(f"总距离      {last.s:.1f} m")
    print(f"平均速度    {mean:.3f} km/h")
    print(f"终点        {last.lat:.6f}, {last.lon:.6f}")

    if args.csv:
        _write_csv(samples, args.csv)
        print(f"已写出 {args.csv}")
    return 0


def _cmd_gpx(args: argparse.Namespace) -> int:
    """导出可回放的 GPX —— 它同时是「存档」和「真机注入的输入」。

    ``pymobiledevice3 developer dvt simulate-location play`` 会按每个点的
    ``<time>`` 差值 sleep 再循环 ``set()``，所以一份带时间戳的 GPX
    就是一条能直接打到真机上的轨迹。
    """
    from .recorder import summarize, write_csv, write_gpx

    route, note = _resolve_route(args)
    if note:
        print(note)
    engine = _build_engine(args, route)
    samples = engine.simulate_offline()
    if not samples:
        print("没有产出任何采样点", file=sys.stderr)
        return 1

    path = write_gpx(samples, args.out, name=f"VRun {args.speed:g}km/h")
    stats = summarize(samples)

    print(f"路线长度    {route.length():.1f} m（{len(route)} 个顶点）")
    print(f"采样点数    {stats['samples']}")
    print(f"总时长      {stats['duration_s']:.1f} s")
    print(f"总距离      {stats['distance_m']:.1f} m")
    print(f"平均速度    {stats['mean_speed_kmh']:.3f} km/h")
    print(f"表观速度    {stats['apparent_speed_min_kmh']:.2f} – "
          f"{stats['apparent_speed_max_kmh']:.2f} km/h"
          f"（1σ = {stats['apparent_speed_sigma_kmh']:.2f}）")
    print()
    print(f"已写出 GPX  {args.out}")

    if args.csv:
        write_csv(samples, args.csv)
        print(f"已写出 CSV  {args.csv}")

    print()
    print("回放到真机：")
    print(f'  "$PY" -m pymobiledevice3 developer dvt simulate-location play -- {args.out}')
    return 0


def _cmd_device_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    """探测设备状态。

    ⚠️ 刻意**不因为「没插手机」而报错** —— 这是状态查询，不是操作。
       返回非零会让 ``panic-clear.sh`` 的调用链误判。
    """
    import asyncio

    try:
        from .device import DeviceBridge
    except ImportError as exc:
        print(f"设备层不可用：{exc}", file=sys.stderr)
        return 2

    bridge = DeviceBridge()
    snapshot = None

    async def probe() -> None:
        nonlocal snapshot
        try:
            await bridge.connect()
        except Exception as exc:
            bridge._set_status(error=str(exc))     # 如实记录，不吞
        finally:
            # ★ 必须在 aclose() **之前**取快照。
            #   aclose() 会把 connected/tunnel/phase 重置为断开态 ——
            #   读在它之后的话，这命令永远只会打印「没连」，
            #   哪怕刚才隧道真的建起来了。这正是 2026-10-14 首跑就撞上的 bug。
            snapshot = bridge.status()
            await bridge.aclose()

    asyncio.run(probe())

    st = snapshot if snapshot is not None else bridge.status()
    print(f"阶段      {st.phase}")
    print(f"已连接    {'是' if st.connected else '否'}"
          + (f"（{st.device_name}）" if st.device_name else ""))
    print(f"DDI       {'已挂载' if st.ddi_mounted else '未确认'}")
    print(f"隧道      {'已建立' if st.tunnel_up else '未建立'}")
    print(f"已注入    {st.injected} 点（丢弃 {st.dropped} 点）")
    if st.error:
        print(f"错误      {st.error}", file=sys.stderr)
    return 0


def _parse_center(text: str) -> tuple[float, float]:
    """``"纬度,经度"`` → ``(lat, lon)``。

    纬度在前是刻意的：绝大多数地图 App 显示的顺序也是纬度在前，
    用户从地图上抄下来能直接用，不用自己在脑子里换一次。
    """
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"--center 要写成「纬度,经度」，收到 {text!r}"
        )
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--center 的两个分量都得是数字，收到 {text!r}"
        ) from exc
    if not (-90.0 <= lat <= 90.0):
        raise argparse.ArgumentTypeError(f"纬度 {lat} 超出 [-90, 90]")
    if not (-180.0 <= lon <= 180.0):
        raise argparse.ArgumentTypeError(f"经度 {lon} 超出 [-180, 180]")
    return lat, lon


def _build_soak_route(args, speed: float) -> tuple[Route, object]:
    """构造压测路线。返回 ``(route, preview_or_None)``。

    给了 ``--center`` 就走**圆模式**：以该点为圆心重复同一个圆。
    """
    if not getattr(args, "center", None):
        return make_long_route_for_soak(args, speed), None

    from .preview import plan_laps, preview

    center = _parse_center(args.center)
    diameter = args.diameter
    fence = args.fence_radius

    if args.laps:
        route = circle_route(center, diameter, laps=args.laps)
        pv = preview(
            route, center=center, diameter_m=diameter, laps=args.laps,
            speed_kmh=speed, rate_hz=args.rate, noise_level=args.noise,
            seed=args.seed, smooth_s=args.smooth, radius_limit_m=fence,
        )
    else:
        pv = plan_laps(
            center, diameter, speed_kmh=speed, rate_hz=args.rate,
            noise_level=args.noise, seed=args.seed, smooth_s=args.smooth,
        )
        # 把用户给的围栏半径补回去 —— plan_laps 搜索时不带它
        pv.radius_limit_m = fence
        route = circle_route(center, diameter, laps=pv.laps)
    return route, pv


def make_long_route_for_soak(args, speed: float) -> Route:
    """定时/到点两种模式各自的旧路线构造逻辑（圆模式之外的分支）。"""
    from .soak import make_long_route

    if args.to_end:
        distance = args.distance if args.distance is not None else 3000.0
        return make_long_route(distance, DEFAULT_ORIGIN)
    planned_m = speed / 3.6 * args.minutes * 60.0
    return make_long_route(planned_m * 2.0, DEFAULT_ORIGIN)


def _cmd_soak(args: argparse.Namespace) -> int:
    """真机长跑压测（M4 / works.md §6 M2 那条「10 分钟统计丢失点与延迟分布」）。

    ⚠️ 它**不走 GUI**，是有意的：压测要连续占着手机 10–20 分钟，
       而 GUI 那套「预置 → 开始」要人点按钮。自动化压测不该依赖点击。

    ⚠️ 跑完**一定**自己清理。若报告里「清理」那一行不是「已确认」，
       立刻手动跑 ``scripts/panic-clear.sh``。
    """
    import asyncio

    try:
        from .device import DeviceBridge
        from .soak import make_long_route, report_lines, soak, write_report
    except ImportError as exc:
        print(f"设备层不可用：{exc}", file=sys.stderr)
        return 2

    speed = args.speed if args.speed is not None else 10.0

    route, pv = _build_soak_route(args, speed)

    # ── 离线预览：不碰设备 ──
    # 「要不要占手机 18 分钟」这个决定，必须能在**不占**的前提下做出。
    if args.dry_run and pv is None:
        # 非圆模式没有可推算的预览。静默返回 0 等于告诉调用方
        # 「预览通过了」—— 那比直接报错糟糕得多。
        print("--dry-run 需要配合 --center（圆模式）使用："
              "非圆模式没有可离线推算的预览。", file=sys.stderr)
        return 2

    if pv is not None:
        from .preview import preview_lines, rule_table_lines

        print(f"★ 离线预览（未连接设备）—— 圆心 {args.center}，"
              f"直径 {args.diameter:g} m，以 {args.smooth:g} s 平滑口径为准\n")
        for line in preview_lines(pv):
            print(line)
        print()
        for line in rule_table_lines(pv):
            print(line)
        print()

        if args.dry_run:
            return 0
        print("以上是**离线推算**，只说明「按这些参数注入会是什么样」，"
              "不代表真机注入成功。接下来才真的连手机跑。\n")

    print("设备层就绪，连线中…")
    bridge = DeviceBridge()
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] < 5.0 and done < total:
            return
        last[0] = now
        pct = done / total * 100 if total else 0.0
        print(f"  …{done}/{total} 点（{pct:.0f}%）", flush=True)

    # 到点模式下 duration_s 只用作「引擎超时上限」的基数
    budget = args.minutes * 60.0
    if args.to_end:
        budget = route.length() / (speed / 3.6) * 1.6 + 120.0

    rep = asyncio.run(soak(
        bridge, route,
        duration_s=budget,
        sample_rate_hz=args.rate,
        speed_kmh=speed,
        noise_level=args.noise,
        seed=args.seed,
        to_end=args.to_end,
        settle_s=args.settle,
        on_progress=progress,
        on_prepared=lambda: print(
            f"\n✅ 已预置到起点并静置 {args.settle:.0f} s —— "
            "**现在可以在校园 App 里点开始跑步了**\n", flush=True),
    ))

    print()
    for line in report_lines(rep):
        print(line)

    if args.json:
        write_report(rep, args.json)
        print(f"\n报告已写出 {args.json}")

    if args.to_end:
        ok = (rep.dropped == 0 and rep.route_completed and rep.cleared
              and rep.eta_error_s is not None
              and abs(rep.eta_error_s) < rep.eta_tolerance_s)
    else:
        ok = (rep.dropped == 0 and rep.reached_duration
              and not rep.error and rep.cleared)
    return 0 if ok else 1


def _cmd_panic_clear(args: argparse.Namespace) -> int:  # noqa: ARG001
    """紧急清除注入的位置（works.md §2.3）。

    这是 safety.py 最后一道保险的下游，**必须自己就能跑**：
    它被调用时，主进程很可能已经没了。

    退出码 0 = 清掉了；2 = 没清掉。上层据此决定要不要继续报警。
    """
    import asyncio

    try:
        from .device import DeviceBridge
    except ImportError as exc:
        print(f"设备层不可用：{exc}", file=sys.stderr)
        return 2

    bridge = DeviceBridge()

    async def run() -> bool:
        try:
            # 必须先连上才谈得上 clear：没通道时 clear 是空操作，
            # 而**空操作会被误读成「清干净了」**。
            await bridge.connect()
        except Exception as exc:
            print(f"✗ 连不上设备，无法清除：{exc}", file=sys.stderr)
            return False

        try:
            ok = await bridge.clear()
            if ok:
                print("✓ 已清除虚拟定位。")
            else:
                print("✗ 清除指令未生效。", file=sys.stderr)
            return ok
        finally:
            await bridge.aclose()

    return 0 if asyncio.run(run()) else 2


# ══════════════════════════════════════════════════════════════
#  辅助
# ══════════════════════════════════════════════════════════════

def _synthetic_route(distance_m: float):
    """生成一个边长可调的方形环，再缩放到目标长度（仅用于离线演示）。"""
    from .route import Route

    lat0, lon0 = DEFAULT_ORIGIN
    d = 0.0045  # ≈ 500 m 见方
    pts = [
        (lat0, lon0),
        (lat0 + d, lon0),
        (lat0 + d, lon0 + d),
        (lat0, lon0 + d),
        (lat0, lon0),
    ]
    return Route(pts).scaled_to(distance_m)


def _result_to_dict(r: EstimateResult) -> dict:
    return {
        "speed_kmh": r.speed_kmh,
        "distance_m": r.distance_m,
        "noise_level": r.noise_level,
        "sample_rate": r.sample_rate,
        "pace_s_per_km": r.pace_s_per_km,
        "pace_str": r.pace_str,
        "eta_naive_s": r.eta_naive_s,
        "eta_s": r.eta_s,
        "eta_closed_form_s": r.eta_closed_form_s,
        "eta_str": r.eta_str,
        "sigma_v": r.sigma_v,
        "band_1sigma": list(r.band_1sigma) if r.band_1sigma else None,
        "band_2sigma": list(r.band_2sigma) if r.band_2sigma else None,
        "safety": r.safety,
        "v_lo": r.v_lo,
        "v_hi": r.v_hi,
        "clamp_enabled": r.clamp_enabled,
        "verdict": r.verdict,
    }


def _write_csv(samples, path: str) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_s", "lat", "lon", "s_m", "v_target_kmh", "v_actual_kmh"])
        for s in samples:
            writer.writerow(
                [f"{s.t:.3f}", f"{s.lat:.7f}", f"{s.lon:.7f}",
                 f"{s.s:.2f}", f"{s.v_target:.3f}", f"{s.v_actual:.3f}"]
            )


# ══════════════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════════════

def _cmd_serve(args: argparse.Namespace) -> int:  # noqa: ARG001
    """启动侧车：JSON Lines 走 stdin/stdout。

    这是 **GUI 唯一会调用的子命令**。管道一断（GUI 退出或崩溃），
    ``readline()`` 立刻返回 EOF，服务循环随即退出 —— 生命周期天然绑定，
    不留孤儿进程（works.md §4.2）。
    """
    import asyncio

    from .ipc import serve

    # 提示写到 **stderr**：stdout 是协议通道，写进去会污染 GUI 的解析。
    print("VRun 侧车已启动，监听 stdin…（当前仅 estimate / device.status 可用）",
          file=sys.stderr)
    return asyncio.run(serve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrun",
        description="VRun —— 校园跑步 App 的离线验证工具（Python 侧车）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── estimate ──
    p_est = sub.add_parser("estimate", help="单点预览：配速 / ETA / 安全带 / 判定预期")
    p_est.add_argument("--speed", type=float, default=10.0, help="目标速度 km/h（默认 10）")
    p_est.add_argument("--distance", type=float, default=D_MIN_M, help="目标距离 米（默认 3000）")
    p_est.add_argument("--noise", choices=NOISE_LEVELS,
                       default="realistic", help="噪声档位（默认 realistic）")
    p_est.add_argument("--rate", type=float, default=1.0, help="采样率 Hz（默认 1）")
    p_est.add_argument("--json", action="store_true", help="额外打印 JSON")
    p_est.set_defaults(func=_cmd_estimate)

    # ── golden ──
    p_gold = sub.add_parser("golden", help="works.md §5.4 基准表（11 行）")
    p_gold.add_argument("--distance", type=float, default=D_MIN_M)
    p_gold.add_argument("--noise", choices=NOISE_LEVELS,
                        default="realistic")
    p_gold.add_argument("--rate", type=float, default=1.0)
    p_gold.add_argument("--json", default=None, help="把基准表写成 JSON 文件")
    p_gold.set_defaults(func=_cmd_golden)

    # ── simulate ──
    p_sim = sub.add_parser("simulate", help="离线跑完整轨迹（假时钟，秒级完成）")
    p_sim.add_argument("--speed", type=float, default=10.0)
    p_sim.add_argument(
        "--distance", type=float, default=None,
        help="目标距离 米。缺省时：给了 --route 就保持路线原长，没给则用 3000",
    )
    p_sim.add_argument("--noise", choices=NOISE_LEVELS,
                       default="realistic")
    p_sim.add_argument("--rate", type=float, default=1.0)
    p_sim.add_argument("--seed", type=int, default=0)
    p_sim.add_argument("--route", default=None, help="路线 JSON 路径（缺省用合成方形环）")
    p_sim.add_argument("--csv", default=None, help="把采样点写成 CSV")
    p_sim.set_defaults(func=_cmd_simulate)

    # ── gpx（可回放到真机）──
    p_gpx = sub.add_parser(
        "gpx", help="导出带时间戳的 GPX —— 可被 simulate-location play 直接回放")
    p_gpx.add_argument("--speed", type=float, default=10.0)
    p_gpx.add_argument("--distance", type=float, default=None,
                       help="目标距离 米。缺省时：给了 --route 就保持路线原长，没给则用 3000")
    p_gpx.add_argument("--noise", choices=NOISE_LEVELS,
                       default="realistic")
    p_gpx.add_argument("--rate", type=float, default=1.0)
    p_gpx.add_argument("--seed", type=int, default=0)
    p_gpx.add_argument("--route", default=None, help="路线 JSON 路径（缺省用合成方形环）")
    p_gpx.add_argument("--out", default="logs/route.gpx", help="GPX 输出路径")
    p_gpx.add_argument("--csv", default=None, help="顺便写一份 CSV")
    p_gpx.set_defaults(func=_cmd_gpx)

    # ── serve（侧车入口，GUI 唯一会调的子命令）──
    p_serve = sub.add_parser(
        "serve", help="启动侧车：JSON Lines 走 stdin/stdout（GUI 用）")
    p_serve.set_defaults(func=_cmd_serve)

    # ── 设备相关（M2）──
    p_dev = sub.add_parser("device-status", help="[M2] 查看设备与隧道状态")
    p_dev.set_defaults(func=_cmd_device_status)

    p_soak = sub.add_parser(
        "soak",
        help="[M4] 真机长跑压测：统计丢失点数与注入延迟分布")
    p_soak.add_argument("--minutes", type=float, default=5.0,
                        help="定时模式的压测时长（分钟）。默认 5")
    p_soak.add_argument("--to-end", action="store_true",
                        help="跑到路线终点才停（M4 的 3 km 验收用这个），"
                             "并比对预览 ETA 与实际用时")
    p_soak.add_argument("--distance", type=float, default=None,
                        help="--to-end 的路线长度（米）。"
                             "缺省 3000（works.md §6 M4 的验收距离）")
    p_soak.add_argument("--rate", type=float, default=1.0, help="采样率 Hz")
    p_soak.add_argument("--speed", type=float, default=None,
                        help="速度 km/h（默认 10）")
    p_soak.add_argument("--noise", default="realistic",
                        choices=NOISE_LEVELS)
    p_soak.add_argument("--seed", type=int, default=0)
    p_soak.add_argument("--settle", type=float, default=0.0,
                        help="预置后的静置秒数。默认 0（压测用）；"
                             "要让用户在 App 里操作时必须给足（works.md §4.3.2 "
                             "要求 10–20 s，否则瞬移到起点那跳会落在记录窗口内）")
    p_soak.add_argument("--center", default=None,
                        help="圆心，格式「纬度,经度」。给了就启用**圆模式**："
                             "以该点为圆心重复同一个圆，圈与圈几何完全重合")
    p_soak.add_argument("--diameter", type=float, default=30.0,
                        help="圆的直径（米），仅圆模式有效。默认 30")
    p_soak.add_argument("--laps", type=int, default=0,
                        help="固定圈数。仅圆模式有效；"
                             "缺省（0）= 自动搜出「两种口径都过 R1/R4 且余量 2%%」的最小圈数")
    p_soak.add_argument("--fence-radius", type=float, default=None,
                        help="电子围栏半径（米）。仅用于报告里判定会不会出界；"
                             "不给就只报最远距离，不下结论")
    p_soak.add_argument("--smooth", type=float, default=5.0,
                        help="假定 App 的平滑窗口（秒），仅圆模式有效。默认 5")
    p_soak.add_argument("--dry-run", action="store_true",
                        help="只跑离线预览并打印 App 口径的判定，**完全不连手机**")
    p_soak.add_argument("--json", default=None,
                        help="把报告写成 JSON 落盘（默认不写）")
    p_soak.set_defaults(func=_cmd_soak)

    p_clear = sub.add_parser("panic-clear", help="[M2] 紧急清除注入的位置")
    p_clear.set_defaults(func=_cmd_panic_clear)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
