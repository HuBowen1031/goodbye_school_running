"""test_cli.py —— 命令行入口的测试。

★ 本文件存在的意义
    守住 ``--distance`` 的**缺省语义**：**没给就是没给**。

    这里曾经有一个真实 bug —— argparse 的默认值取了 3000，
    于是 ``vrun simulate --route X.json`` 会把用户画好的路线
    **无声地**缩放到 3 km：用户没要求，却已经发生了，
    且界面上没有任何提示。根因只是一行默认值，所以护栏也从默认值开始。
"""

from __future__ import annotations

from pathlib import Path

from vrun.cli import build_parser, main
from vrun.route import Route


def _write_route(tmp_path: Path, name: str = "route.json") -> Path:
    """写一条约 2 km 的折线路线到磁盘（三顶点，非闭合环）。"""
    lat0, lon0 = 30.5535, 103.9910
    d = 0.0045
    route = Route([(lat0, lon0), (lat0 + d, lon0), (lat0 + d, lon0 + d)])
    path = tmp_path / name
    route.to_json(str(path))
    return path


# ══════════════════════════════════════════════════════════════
#  --distance 的缺省语义
# ══════════════════════════════════════════════════════════════

class TestDistanceDefault:
    def test_default_is_none(self) -> None:
        """★ 根因守卫：默认值必须是 None，不能是 3000。

        只要这一行被改回去，下面两条测试就会红。
        """
        assert build_parser().parse_args(["simulate"]).distance is None

    def test_route_keeps_its_own_length(self, tmp_path, capsys) -> None:
        """给了 --route 却没给 --distance ⇒ 路线长度**原样保留**。"""
        path = _write_route(tmp_path)
        original_m = Route.from_json(str(path)).length()

        assert main(["simulate", "--route", str(path)]) == 0

        out = capsys.readouterr().out
        assert "路线缩放" not in out, "没要求缩放，却缩放了"
        assert f"{original_m:.1f} m" in out

    def test_explicit_distance_scales_and_says_so(self, tmp_path, capsys) -> None:
        """显式给了 --distance ⇒ 缩放，而且**必须打印出来**。"""
        path = _write_route(tmp_path)

        assert main(["simulate", "--route", str(path), "--distance", "3000"]) == 0

        out = capsys.readouterr().out
        assert "路线缩放" in out, "缩放是用户可见的副作用，必须显式提示"
        assert "3000.0 m" in out

    def test_matching_distance_is_not_reported_as_scaling(
        self, tmp_path, capsys
    ) -> None:
        """--distance 与原长一致时不必谎称缩放过。"""
        path = _write_route(tmp_path)
        original_m = Route.from_json(str(path)).length()

        assert main(["simulate", "--route", str(path),
                     "--distance", f"{original_m:.1f}"]) == 0
        assert "路线缩放" not in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════
#  合成路线
# ══════════════════════════════════════════════════════════════

class TestSyntheticRoute:
    def test_defaults_to_three_km(self, capsys) -> None:
        """没给 --route 也没给 --distance ⇒ 用 3.00 km 的合成环。"""
        assert main(["simulate"]) == 0
        assert "3000.0 m" in capsys.readouterr().out

    def test_honours_explicit_distance(self, capsys) -> None:
        assert main(["simulate", "--distance", "5000"]) == 0
        assert "5000.0 m" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════
#  设备相关子命令（M2 未实现时的降级路径）
# ══════════════════════════════════════════════════════════════

class TestDeviceStubs:
    def test_device_status_reports_m2_pending(self, capsys) -> None:
        """M2 未实现时必须**明确报错**并列出可用子命令，而不是崩栈。"""
        rc = main(["device-status"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "M2" in captured.err
        assert "estimate" in captured.err

    def test_panic_clear_reports_m2_pending(self, capsys) -> None:
        rc = main(["panic-clear"])
        assert rc == 2
        assert "M2" in capsys.readouterr().err
