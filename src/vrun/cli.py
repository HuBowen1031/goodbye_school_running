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
from .profile import PRESETS, SpeedProfile

# 江安校区附近的合成起点。**只用于离线演示**：真实使用时应由 GUI 从地图取点。
DEFAULT_ORIGIN: tuple[float, float] = (30.5535, 103.9910)


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
    from .noise import GPSNoise

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
    # 路线给 2 倍余量，保证任何速度下都不会「先到终点」而提前结束压测
    planned_m = speed / 3.6 * args.minutes * 60.0
    route = make_long_route(planned_m * 2.0, DEFAULT_ORIGIN)

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

    rep = asyncio.run(soak(
        bridge, route,
        duration_s=args.minutes * 60.0,
        sample_rate_hz=args.rate,
        speed_kmh=speed,
        noise_level=args.noise,
        seed=args.seed,
        on_progress=progress,
    ))

    print()
    for line in report_lines(rep):
        print(line)

    if args.json:
        write_report(rep, args.json)
        print(f"\n报告已写出 {args.json}")

    return 0 if (rep.dropped == 0 and rep.reached_duration
                 and not rep.error and rep.cleared) else 1


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
    p_est.add_argument("--noise", choices=sorted(("off", "realistic", "harsh")),
                       default="realistic", help="噪声档位（默认 realistic）")
    p_est.add_argument("--rate", type=float, default=1.0, help="采样率 Hz（默认 1）")
    p_est.add_argument("--json", action="store_true", help="额外打印 JSON")
    p_est.set_defaults(func=_cmd_estimate)

    # ── golden ──
    p_gold = sub.add_parser("golden", help="works.md §5.4 基准表（11 行）")
    p_gold.add_argument("--distance", type=float, default=D_MIN_M)
    p_gold.add_argument("--noise", choices=sorted(("off", "realistic", "harsh")),
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
    p_sim.add_argument("--noise", choices=sorted(("off", "realistic", "harsh")),
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
    p_gpx.add_argument("--noise", choices=sorted(("off", "realistic", "harsh")),
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
                        help="压测时长（分钟）。默认 5；works.md 要求 10")
    p_soak.add_argument("--rate", type=float, default=1.0, help="采样率 Hz")
    p_soak.add_argument("--speed", type=float, default=None,
                        help="速度 km/h（默认 10）")
    p_soak.add_argument("--noise", default="realistic",
                        choices=["off", "realistic", "strict"])
    p_soak.add_argument("--seed", type=int, default=0)
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
