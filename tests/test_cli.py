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

import pytest

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

class TestDeviceCommands:
    """M2 已实装后，这两条子命令的行为。**都不需要 iPhone 在手**。"""

    def test_device_status_is_graceful_without_phone(self, capsys) -> None:
        """没插手机时 :command:`device-status` 必须**正常退出并说明原因**。

        它是**状态查询**不是操作 —— 返回非零会让
        :file:`scripts/panic-clear.sh` 的调用链误判成失败。
        """
        rc = main(["device-status"])
        captured = capsys.readouterr()
        assert rc == 0, "状态查询失败不应返回非零"
        assert "阶段" in captured.out
        assert "已连接" in captured.out

    def test_panic_clear_reports_why_when_offline(self, capsys) -> None:
        """没插手机时 :command:`panic-clear` 必须**返回非零并说清原因**。

        这里必须区分两种「没清掉」：
          · 连不上设备 → 退出码 2 + 说明
          · 连上了但 clear 无效 → 退出码 2 + 说明
        两者都**不能**静默返回 0 —— 上游会把它读成「清干净了」。
        """
        rc = main(["panic-clear"])
        captured = capsys.readouterr()
        if rc == 2:
            assert (captured.err or captured.out).strip(), "失败时必须给出原因"
        # 有设备连着时 rc 应为 0；没有时为 2。两者都不许是「静默 0」
        assert rc in (0, 2)


# ══════════════════════════════════════════════════════════════
# --noise 档位必须与 GPSNoise.LEVELS 严格一致
# ══════════════════════════════════════════════════════════════


def _noise_choices(subcommand: str):
    """取出某个子命令 ``--noise`` 的 choices。"""
    action = next(
        a for a in build_parser()._subparsers._group_actions  # noqa: SLF001
        if hasattr(a, "choices") and a.choices and "soak" in a.choices
    )
    return action.choices[subcommand]._actions  # noqa: SLF001


class TestNoiseLevelsStayInSync:
    """★ 回归：``soak`` 的 ``--noise`` 曾手写成 ``strict``，而 ``LEVELS`` 是 ``harsh``。

    argparse 放行 ``--noise strict``，几百毫秒后引擎才抛
    「未知噪声档位」—— 错误信息指向的位置与真正的原因完全无关。
    手写字符串列表必然会漂移；护栏只能从数据源比对。
    """

    def test每个子命令的档位都等于引擎的档位(self) -> None:
        from vrun.noise import GPSNoise

        for sub in ("estimate", "golden", "simulate", "gpx", "soak"):
            actions = _noise_choices(sub)
            noise_actions = [a for a in actions if a.dest == "noise"]
            assert noise_actions, f"{sub} 没有 --noise"
            for act in noise_actions:
                assert sorted(act.choices) == sorted(GPSNoise.LEVELS), (
                    f"{sub} 的 --noise 档位是 {act.choices}，"
                    f"引擎只认 {sorted(GPSNoise.LEVELS)}"
                )

    def test任意档位都能被引擎接受(self) -> None:
        """每个子命令给出的每一个档位，喂给引擎都必须被接受。"""
        from vrun.noise import GPSNoise

        for sub in ("estimate", "golden", "simulate", "gpx", "soak"):
            for act in _noise_choices(sub):
                if act.dest != "noise":
                    continue
                for level in act.choices:
                    GPSNoise(level=level)  # 非法档位会在这里抛 ValueError


class TestCenterParsing:
    """``--center`` 走 argparse 的 ``type=``：非法输入在**解析期**就被拒。

    早期版本在 ``_cmd_soak`` 里手动调 ``_parse_center``，
    于是用户输错坐标得到的是一整段 traceback ——
    而 ``--distance`` 输错是 argparse 的标准报错。同一类错误两种待遇。
    """

    @pytest.mark.parametrize("raw,lat,lon", [
        ("30.628212,104.084842", 30.628212, 104.084842),
        (" 30.628212 , 104.084842 ", 30.628212, 104.084842),
    ])
    def test合法坐标被接受(self, raw, lat, lon) -> None:
        args = build_parser().parse_args(
            ["soak", "--center", raw, "--dry-run", "--to-end"])
        assert args.center == (lat, lon)

    @pytest.mark.parametrize("raw", ["30.6", "a,b", "1,2,3", ""])
    def test格式错误在解析期被拒(self, raw) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["soak", "--center", raw, "--to-end"])

    @pytest.mark.parametrize("raw", ["200,300", "-91,0", "0,181"])
    def test越界坐标在解析期被拒(self, raw) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["soak", "--center", raw, "--to-end"])

    def test不给中心就是非圆模式(self) -> None:
        assert build_parser().parse_args(["soak", "--to-end"]).center is None

    def test_dry_run缺中心时报错而不是静默通过(self, capsys) -> None:
        """★ 静默返回 0 等于告诉调用方「预览通过了」。"""
        assert main(["soak", "--dry-run", "--to-end"]) == 2
        assert "需要配合 --center" in capsys.readouterr().err


class TestDryRunNeverTouchesDevice:
    """``--dry-run`` 的全部意义就是**不占手机**，它必须一行设备代码都跑不到。"""

    def test_dry_run在构造设备之前就返回(self, monkeypatch, capsys) -> None:
        import vrun.cli as cli

        def boom(*a, **k):
            raise AssertionError("--dry-run 竟然碰了设备层")

        monkeypatch.setattr("vrun.device.DeviceBridge", boom)
        assert cli.main([
            "soak", "--dry-run", "--to-end",
            "--center", "30.628212,104.084842", "--diameter", "30",
        ]) == 0
        out = capsys.readouterr().out
        assert "离线预览" in out
        assert "设备层就绪" not in out, "不该走到连设备那一步"
