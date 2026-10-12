"""recorder.py — 采样序列的落盘与统计。

职责
    把 :class:`vrun.engine.Sample` 序列写成 CSV（供统计）与 GPX（供复盘/回放），
    并提供 :func:`summarize` 算出 M1 验收要用的那几个量。

为什么 GPX 很重要（不只是存档）
    ``pymobiledevice3`` 自带的 ``simulate-location play`` 会读 GPX，
    **按每个点的 ``<time>`` 差值 sleep，然后循环 ``set()``**。
    也就是说，**一份带时间戳的 GPX 就是一条可直接回放的轨迹** ——
    它让「离线算出的轨迹」与「真机上的注入」共用同一份数据，
    不需要在真机上重跑一遍引擎。

    这也是 works.md §1.2 那句「``play_gpx_file()`` 本质是按时间戳 sleep 后循环
    ``set()``，可仿此自写循环」的落地方式。

依赖
    仅标准库（csv / xml）。XML 用 ``ElementTree`` 手动拼 —— GPX 结构极简，
    引第三方库不划算，且生成的字节更可控。
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from xml.sax.saxutils import escape

# GPX 的默认基准时刻。用固定值而不是 ``now()`` —— GPX 里的绝对时间没有物理意义
# （works.md §2.1：时间不可伪造），真正被使用的是**点与点之间的差值**。
# 固定它还让「同种子 ⇒ 同字节」在 GPX 上也成立。
GPX_EPOCH: datetime = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

CSV_COLUMNS = ["t_s", "lat", "lon", "s_m", "v_target_kmh", "v_actual_kmh"]


# ══════════════════════════════════════════════════════════════
#  CSV
# ══════════════════════════════════════════════════════════════

def write_csv(samples: Iterable, path: str) -> str:
    """写出 CSV，列顺序固定为 ``t,lat,lon,s,v_target,v_actual``。

    固定列序是刻意的：验收脚本与 ``csv.DictReader`` 都依赖它。
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for s in samples:
            writer.writerow([
                f"{s.t:.3f}",
                f"{s.lat:.7f}",
                f"{s.lon:.7f}",
                f"{s.s:.2f}",
                f"{s.v_target:.3f}",
                f"{s.v_actual:.3f}",
            ])
    return path


# ══════════════════════════════════════════════════════════════
#  GPX
# ══════════════════════════════════════════════════════════════

def write_gpx(samples: Iterable, path: str, name: str = "VRun") -> str:
    """写出 GPX 1.1，可直接被 ``simulate-location play`` 回放。

    每个 ``<trkpt>`` 都带 ``<time>``，其**间隔**就是注入节奏。
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="VRun" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <trk><name>{escape(name)}</name><trkseg>",
    ]
    for s in samples:
        stamp = (GPX_EPOCH + timedelta(seconds=s.t)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lines.append(f'    <trkpt lat="{s.lat:.7f}" lon="{s.lon:.7f}">')
        lines.append(f"      <time>{stamp}</time>")
        lines.append("    </trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>", ""]

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


# ══════════════════════════════════════════════════════════════
#  统计
# ══════════════════════════════════════════════════════════════

def summarize(samples: Iterable) -> dict:
    """算出 M1 验收要的量：总距离 / 用时 / 平均速度 / 表观速度标准差。

    ``mean_speed_kmh`` 用**总弧长 ÷ 总时长**算，而不是对瞬时速度求平均 ——
    后者会被起步段的低值系统性拉低，与 works.md §3.1 的口径一致。
    """
    samples = list(samples)
    if not samples:
        return {
            "samples": 0, "distance_m": 0.0, "duration_s": 0.0,
            "mean_speed_kmh": 0.0, "apparent_speed_sigma_kmh": 0.0,
            "apparent_speed_min_kmh": 0.0, "apparent_speed_max_kmh": 0.0,
        }

    last = samples[-1]
    duration = last.t
    distance = last.s
    apparent = [s.v_actual for s in samples[1:]]      # 首点无「前一点」，无法差分

    return {
        "samples": len(samples),
        "distance_m": distance,
        "duration_s": duration,
        "mean_speed_kmh": (distance / duration) * 3.6 if duration > 0 else 0.0,
        "apparent_speed_sigma_kmh": stdev(apparent),
        "apparent_speed_min_kmh": min(apparent) if apparent else 0.0,
        "apparent_speed_max_kmh": max(apparent) if apparent else 0.0,
    }


def stdev(values: list[float]) -> float:
    """样本标准差（n−1）。空序列返回 0。

    刻意不用 :mod:`statistics`：① 纯计算层的依赖纪律见 works.md §4.5；
    ② 这里只需要一个函数，内联更直白。
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
