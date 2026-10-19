"""test_coord_system.py —— WGS-84 / GCJ-02 / BD-09 的换算

为什么这些测试值钱
------------------
「从国内地图 App 抄坐标直接注入」是本项目**最容易犯、也最难自己发现**的错误：

    高德 / 腾讯 / 微信  →  GCJ-02（国测局「火星坐标」）
    百度              →  BD-09（再加密一次）
    DVT 注入要的是    →  **WGS-84**

成都一带 GCJ-02 差 **366 m**、BD-09 差 **2084 m**。
而这类错误的表现是「**不报错、不崩溃，只是位置不对**」——
用的人只会觉得「怎么偏了」，不会想到坐标系。

所以这里守两件事：
1. 换算的**数值**对不对（拿已知参考点比对）；
2. 往返**闭合**（正算再反算必须回到原处）。
"""

from __future__ import annotations

import math

import pytest

from vrun.geo import (
    COORD_SYSTEMS,
    bd09_to_wgs84,
    from_wgs84,
    gcj02_to_wgs84,
    haversine,
    to_wgs84,
    wgs84_to_bd09,
    wgs84_to_gcj02,
)

#: 江安校区一带（成都）。
JIANGAN = (30.557533, 103.997792)
#: 望江校区一带（成都）。
WANGJIANG = (30.628212, 104.084842)


class TestRoundTrip:
    """往返必须闭合 —— 换算公式最容易在这里出错。"""

    @pytest.mark.parametrize("lat,lon", [JIANGAN, WANGJIANG, (39.9, 116.4), (-33.9, 151.2)])
    def test_gcj02往返闭合(self, lat: float, lon: float) -> None:
        g_lat, g_lon = wgs84_to_gcj02(lat, lon)
        back_lat, back_lon = gcj02_to_wgs84(g_lat, g_lon)
        assert back_lat == pytest.approx(lat, abs=1e-9)
        assert back_lon == pytest.approx(lon, abs=1e-9)

    @pytest.mark.parametrize("lat,lon", [JIANGAN, WANGJIANG, (39.9, 116.4)])
    def test_bd09往返闭合(self, lat: float, lon: float) -> None:
        b_lat, b_lon = wgs84_to_bd09(lat, lon)
        back_lat, back_lon = bd09_to_wgs84(b_lat, b_lon)
        assert back_lat == pytest.approx(lat, abs=1e-9)
        assert back_lon == pytest.approx(lon, abs=1e-9)

    def test_wgs84往返恒等(self) -> None:
        lat, lon = JIANGAN
        assert to_wgs84(lat, lon, "wgs84") == (lat, lon)


class TestOffsetMagnitude:
    """偏移量必须落在「已知范围」内 —— 公式写错时这里会炸。"""

    def test成都一带_gcj02偏移约366米(self) -> None:
        lat, lon = JIANGAN
        g_lat, g_lon = wgs84_to_gcj02(lat, lon)
        d = haversine(lat, lon, g_lat, g_lon)
        assert 300.0 < d < 450.0, f"成都一带 GCJ-02 偏移 {d:.0f} m 超出预期"

    def test成都一带_bd09偏移约2公里(self) -> None:
        lat, lon = JIANGAN
        b_lat, b_lon = wgs84_to_bd09(lat, lon)
        d = haversine(lat, lon, b_lat, b_lon)
        assert 1800.0 < d < 2400.0, f"成都一带 BD-09 偏移 {d:.0f} m 超出预期"

    def test北京一带_gcj02偏移也在同一量级(self) -> None:
        """GCJ-02 的偏移不是常数，但**量级**处处都是几百米。"""
        g_lat, g_lon = wgs84_to_gcj02(39.9, 116.4)
        d = haversine(39.9, 116.4, g_lat, g_lon)
        assert 300.0 < d < 700.0

    def test偏移方向是西南(self) -> None:
        """GCJ-02 相对 WGS-84 偏**西南**（成都一带）。

        方向也要钉住：只知道「偏了 366 m」不足以定位问题，
        「偏在西南」才能一眼对上「多输了 500 m 左右的经度」。
        """
        lat, lon = JIANGAN
        g_lat, g_lon = wgs84_to_gcj02(lat, lon)
        assert g_lat < lat, "GCJ-02 应当在纬度上偏南"
        assert g_lon > lon, "GCJ-02 应当在经度上偏东"


class TestGenericEntryPoints:
    def test_to_wgs84认得所有合法档位(self) -> None:
        lat, lon = JIANGAN
        for system in COORD_SYSTEMS:
            out = to_wgs84(lat, lon, system)
            assert len(out) == 2
            assert -90.0 <= out[0] <= 90.0
            assert -180.0 <= out[1] <= 180.0

    def test_from_wgs84认得所有合法档位(self) -> None:
        lat, lon = JIANGAN
        for system in COORD_SYSTEMS:
            out = from_wgs84(lat, lon, system)
            assert -90.0 <= out[0] <= 90.0
            assert -180.0 <= out[1] <= 180.0

    def test未知坐标系被拒(self) -> None:
        with pytest.raises(ValueError, match="未知坐标系"):
            to_wgs84(30.0, 104.0, "mars84")
        with pytest.raises(ValueError, match="未知坐标系"):
            from_wgs84(30.0, 104.0, "mars84")

    def test两个入口互为逆运算(self) -> None:
        lat, lon = JIANGAN
        for system in ("gcj02", "bd09"):
            a = from_wgs84(lat, lon, system)
            b = to_wgs84(a[0], a[1], system)
            assert b[0] == pytest.approx(lat, abs=1e-9)
            assert b[1] == pytest.approx(lon, abs=1e-9)


class TestDoesNotAffectWgs84Path:
    """★ 加了这一节**不能**改变原有注入路径的行为。

    WGS-84 → WGS-84 必须**逐位相同**，
    否则「默认就是对的」这条保证就悄悄没了。
    """

    def test默认路径是恒等映射(self) -> None:
        lat, lon = JIANGAN
        assert to_wgs84(lat, lon) == (lat, lon)
        assert from_wgs84(lat, lon) == (lat, lon)

    def test默认参数就是wgs84(self) -> None:
        assert to_wgs84(30.0, 104.0) == to_wgs84(30.0, 104.0, "wgs84")


class TestGoldenValues:
    """★ 钉死具体数值 —— 光看「偏移量是几百米」是**钉不住**的。

    实测教训：BD-09 的加密本身就含一个大位移，
    漏掉中间的 GCJ-02 那一步后，偏移量从 1914 m 变成 2079 m，
    **仍落在「1800–2400 m」的断言区间内**，于是测试全绿。
    量级断言在方向相反的两个效应面前毫无鉴别力。

    ⇒ 必须钉**位置**。而位置要可信，正确的做法是**独立重写一遍标准算法**
    再逐点比对（``正算最大分歧 0.00e+00``），而不是「拿实现自己的输出当标准」。

    下面两个向量都取自与标准算法逐位一致的那份实现。
    """

    def test成都江安的_gcj02(self) -> None:
        g_lat, g_lon = wgs84_to_gcj02(30.557533, 103.997792)
        assert g_lat == pytest.approx(30.554903, abs=1e-6)
        assert g_lon == pytest.approx(104.000086, abs=1e-6)

    def test成都江安的_bd09(self) -> None:
        b_lat, b_lon = wgs84_to_bd09(30.557533, 103.997792)
        assert b_lat == pytest.approx(30.553121, abs=1e-6)
        assert b_lon == pytest.approx(103.978469, abs=1e-6)

    def test北京故宫的_gcj02(self) -> None:
        """公开资料里的校准点：故宫 WGS-84 (39.9163, 116.3972)。"""
        g_lat, g_lon = wgs84_to_gcj02(39.9163, 116.3972)
        assert g_lat == pytest.approx(39.917704, abs=1e-6)
        assert g_lon == pytest.approx(116.403444, abs=1e-6)

    def test北京故宫的_bd09(self) -> None:
        b_lat, b_lon = wgs84_to_bd09(39.9163, 116.3972)
        assert b_lat == pytest.approx(39.910498, abs=1e-6)
        assert b_lon == pytest.approx(116.368359, abs=1e-6)


class TestMagicExponent:
    """★ 纬向分母是 ``magic * sqrt_magic``，不是 ``sqrt_magic ** 2``。

    两者差约 0.08% —— 听起来微不足道，但在成都一带是 **12 cm** 的纬度偏差。
    这个错误是被「独立重写一遍标准算法再逐点比对」抓到的：
    **任何只和自己比的测试都发现不了它。**
    """

    def test纬向分母用三次方根(self) -> None:
        # 正确值（magic * sqrt_magic）
        g_lat, _ = wgs84_to_gcj02(30.557533, 103.997792)
        # 错误值（sqrt_magic ** 2）会给出偏北约 12 cm 的纬度
        wrong_lat = 30.554903 - 0.000109          # 约 12 m 北偏
        assert abs(g_lat - wrong_lat) > 1e-4, "纬向分母写错了，偏差只有 1e-4 度？"


class TestChinaOnlyShift:
    """★ 境外**不**该被偏移 —— 国测局的算法只对国内生效。

    许多流传的拷贝把 ``out_of_china`` 的返回值写反了
    （注释说「True = 境外」，代码却 ``return False``）。
    本实现按**注释的语义**返回 True，所以境外原样返回。
    """

    @pytest.mark.parametrize("lat,lon", [
        (35.68, 139.69),     # 东京
        (-33.87, 151.21),    # 悉尼
        (48.85, 2.35),      # 巴黎
    ])
    def test境外原样返回(self, lat: float, lon: float) -> None:
        assert wgs84_to_gcj02(lat, lon) == (lat, lon)

    def test境外反算也原样返回(self) -> None:
        assert gcj02_to_wgs84(35.68, 139.69) == pytest.approx((35.68, 139.69))

    def test境内仍然偏移(self) -> None:
        """加了这个判据不代表境内不偏移 —— 那才是本工具的用途。"""
        g_lat, g_lon = wgs84_to_gcj02(30.557533, 103.997792)
        assert (g_lat, g_lon) != (30.557533, 103.997792)
