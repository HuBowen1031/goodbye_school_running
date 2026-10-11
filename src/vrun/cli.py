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


def _cmd_simulate(args: argparse.Namespace) -> int:
    # 延迟导入：引擎依赖 numpy，但路径很短，放在函数内也无碍可读性
    from .engine import TrackEngine
    from .noise import GPSNoise
    from .route import Route

    if args.route:
        route = Route.from_json(args.route)
        if args.distance and abs(route.length() - args.distance) > 1.0:
            route = route.scaled_to(args.distance)
    else:
        route = _synthetic_route(args.distance)

    profile = SpeedProfile(v_bar=args.speed)
    noise = GPSNoise(level=args.noise, sample_rate=args.rate, seed=args.seed)
    engine = TrackEngine(route, profile, noise, sample_rate=args.rate)

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


def _cmd_device_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    return _device_stub("device-status")


def _cmd_panic_clear(args: argparse.Namespace) -> int:  # noqa: ARG001
    return _device_stub("panic-clear")


def _device_stub(name: str) -> int:
    try:
        from . import device  # noqa: F401
    except ImportError:
        print(
            f"`{name}` 需要设备层（src/vrun/device.py），它属于里程碑 M2，尚未实现。\n"
            "当前可用的子命令：estimate / golden / simulate —— 三者都不需要 iPhone。",
            file=sys.stderr,
        )
        return 2
    print(f"`{name}` 已就绪（M2 实现）", file=sys.stderr)
    return 0


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
    p_sim.add_argument("--distance", type=float, default=D_MIN_M)
    p_sim.add_argument("--noise", choices=sorted(("off", "realistic", "harsh")),
                       default="realistic")
    p_sim.add_argument("--rate", type=float, default=1.0)
    p_sim.add_argument("--seed", type=int, default=0)
    p_sim.add_argument("--route", default=None, help="路线 JSON 路径（缺省用合成方形环）")
    p_sim.add_argument("--csv", default=None, help="把采样点写成 CSV")
    p_sim.set_defaults(func=_cmd_simulate)

    # ── 设备相关（M2）──
    p_dev = sub.add_parser("device-status", help="[M2] 查看设备与隧道状态")
    p_dev.set_defaults(func=_cmd_device_status)

    p_clear = sub.add_parser("panic-clear", help="[M2] 紧急清除注入的位置")
    p_clear.set_defaults(func=_cmd_panic_clear)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
