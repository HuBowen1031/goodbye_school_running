"""VRun —— 校园跑步 App 的离线验证工具（Python 侧车）。

分层（详见 ``docs/file-manifest.md`` §1）::

    ① 纯计算层   geo / route / profile / noise / engine / estimate
    ② 设备层     device / safety      ← 全项目唯一接触 pymobiledevice3 的地方
    ③ 编排层     ipc / recorder / cli

⚠️ 本模块**刻意不 import ② 层**。
   若 ``pymobiledevice3`` 成为包级强依赖，那么在一台没装好它、也没插手机的
   机器上连 ``vrun estimate`` 都跑不起来 —— 而 M1 的全部价值恰恰在于
   「手机不在场也能推进」。设备相关一律在 ``cli.py`` 中**延迟导入**。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .geo import EARTH_RADIUS_M, ENUProjector, bearing, haversine
from .noise import AR1, GPSNoise
from .profile import PRESETS, SpeedProfile
from .route import Route

__all__ = [
    "__version__",
    "EARTH_RADIUS_M",
    "ENUProjector",
    "haversine",
    "bearing",
    "Route",
    "SpeedProfile",
    "PRESETS",
    "AR1",
    "GPSNoise",
]
