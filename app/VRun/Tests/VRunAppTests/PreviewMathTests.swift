import XCTest
@testable import VRunApp

// ══════════════════════════════════════════════════════════════════
//  PreviewMathTests —— Swift 侧与 Python 侧的**跨语言契约**
//
//  ★ works.md §5.6 的核心保证
//     `tests/golden/estimate.json` 是两侧共享的基准。
//     Python 的 test_estimate.py 与这里的 PreviewMathTests 都读它 ——
//     **两边都过，才说明「预览与实际一致」不是巧合。**
//
//  ⚠️ 这些断言用的是 works.md §5.4 的**文档值**（含 F1 记录的
//     σ 舍入偏差），容差已按 findings.md 的说明放宽。
// ══════════════════════════════════════════════════════════════════

final class PreviewMathTests: XCTestCase {

    // MARK: - §3.5 表观抖动（§3.5 的表，含 F1 的舍入口径）

    func testSigmaApparentMatchesDocTable() {
        // §3.5 表：0.2s→4.7 / 0.5s→2.9 / 1.0s→2.0 / 2.0s→1.3 / 5.0s→0.7
        let expected: [(Double, Double)] = [(0.2, 4.7), (0.5, 2.9), (1.0, 2.0),
                                              (2.0, 1.3), (5.0, 0.7)]
        for (dt, doc) in expected {
            let got = PreviewMath.sigmaApparent(dt: dt, level: .realistic)
            XCTAssertEqual(got, doc, accuracy: 0.06,
                           "Δt=\(dt)s 文档 \(doc) vs 实现 \(got)")
        }
    }

    /// ★ 反直觉但重要：采样率越高，朴素差分的速度噪声越大（∝ 1/√Δt）
    func testHigherRateMeansMoreNoise() {
        let slow = PreviewMath.sigmaApparent(dt: 5.0, level: .realistic)
        let fast = PreviewMath.sigmaApparent(dt: 0.2, level: .realistic)
        XCTAssertGreaterThan(fast, slow, "1/√Δt 关系反了")
    }

    func testOffIsExactlyZero() {
        XCTAssertEqual(PreviewMath.sigmaApparent(dt: 1.0, level: .off), 0, accuracy: 1e-12)
    }

    func testHarshIsOnePointFiveTimes() {
        let r = PreviewMath.sigmaApparent(dt: 1.0, level: .realistic)
        let h = PreviewMath.sigmaApparent(dt: 1.0, level: .harsh)
        XCTAssertEqual(h / r, 1.5, accuracy: 1e-9)
    }

    // MARK: - §3.6 配速 / ETA（§5.4 基准表）

    func testPaceAtGoldenSpeeds() {
        let expected: [(Double, String)] = [(6.0, "10:00"), (8.0, "7:30"), (9.0, "6:40"),
                                            (10.0, "6:00"), (10.2, "5:53"), (12.0, "5:00"),
                                            (12.4, "4:50"), (13.0, "4:37"), (14.0, "4:17"),
                                            (16.5, "3:38"), (17.0, "3:32")]
        for (kmh, doc) in expected {
            XCTAssertEqual(PreviewMath.paceText(PreviewMath.pace(kmh: kmh)), doc,
                           "\(kmh) km/h 的配速与基准表不符")
        }
    }

    /// ★ §3.6 修正 ETA —— 逐行对齐 §5.4（3 km 距离）
    ///
    /// ⚠️ **比的是秒，不是字符串。** §5.4 的「修正 ETA」列是 Python 侧
    /// `expected_duration()` 的**数值积分**结果；这里的 `closedFormEta`
    /// 是 GUI 拖动时用的一阶近似（§3.6）。两者在 §5.6 的契约内允许差 1 s，
    /// 而 1 s 足以让**显示的秒数**跳一格 —— 12.4 km/h 那行就是这个情况
    /// （近似 878.76 s vs 精确 878.0 s，取整后 14:39 vs 14:38，findings F10）。
    /// 所以真正的验收标准是秒级容差，不是文本相等。
    func testClosedFormETAMatchesGoldenTable() {
        // (速度, §5.4 表格文本, 该行的精确积分秒数)
        let expected: [(Double, String, Double)] = [
            (6.0, "30:13", 1813), (8.0, "22:40", 1360), (9.0, "20:10", 1210),
            (10.0, "18:09", 1089), (10.2, "17:48", 1068), (12.0, "15:08", 908),
            (12.4, "14:38", 878), (13.0, "13:58", 838), (14.0, "12:59", 779),
            (16.5, "11:01", 661),
            // ⚠️ 17.0 行**故意排除**：文档 10:47 与两侧实现都对不上，
            // findings.md F3 已登记为文档异常（疑似按 v_ceil 算过）。
        ]
        for (kmh, doc, exactS) in expected {
            let got = PreviewMath.closedFormEta(distanceM: 3000, kmh: kmh)
            // ★ 契约本身：§5.6 允许 1 s，超出即意味着近似该被换掉
            XCTAssertEqual(got, exactS, accuracy: 1.0,
                           "\(kmh) km/h：闭式解 \(got) s 偏离精确值 \(exactS) s 超过 1 s")
            // 绝大多数行的显示文本也应当一致（F10 那行除外）
            if kmh != 12.4 {
                XCTAssertEqual(PreviewMath.durationText(got), doc,
                               "\(kmh) km/h 的修正 ETA：基准 \(doc) vs 实现 \(PreviewMath.durationText(got))")
            }
        }
    }

    /// ★ F10：12.4 km/h 处闭式解比精确积分高 0.76 s，**刚好跨过取整边界**，
    /// 于是「预览显示 14:39、实测跑出 14:38」。§5.6 的 1 s 阈值抓不到它。
    /// 这条测试把这个已知偏差钉死，改动闭式解时会被立刻发现。
    func testF10RoundingBoundaryAtTwelvePointFour() {
        let closed = PreviewMath.closedFormEta(distanceM: 3000, kmh: 12.4)
        XCTAssertEqual(closed, 878.758, accuracy: 0.01)
        XCTAssertEqual(PreviewMath.durationText(closed), "14:39")
        XCTAssertEqual(PreviewMath.durationText(878.0), "14:38",
                       "精确积分值应显示 14:38 —— 预览与实测的秒数差异即源于此")
        // 偏差在 §5.6 阈值内，所以 GUI 不会高亮提示，但显示值确实差 1 s
        XCTAssertLessThan(abs(closed - 878.0), 1.0)
    }

    /// 10 km/h @ 3 km 的标志性数值：18:09（朴素 18:00 + 9 s 修正）
    func testDefaultIsEighteenOhNine() {
        XCTAssertEqual(PreviewMath.durationText(PreviewMath.naiveEta(distanceM: 3000, kmh: 10)), "18:00")
        XCTAssertEqual(PreviewMath.durationText(PreviewMath.closedFormEta(distanceM: 3000, kmh: 10)), "18:09")
    }

    // MARK: - §5.3 安全带

    /// ★ 那个反直觉的核心结论：合法窗口 6–17，但 2σ 安全带只有 10.06–12.47
    func testGreenBandIsFarNarrowerThanWindow() {
        let sigma = PreviewMath.sigmaApparent(dt: 1.0, level: .realistic)
        guard let b2 = PreviewMath.band(sigmaV: sigma, levels: 2) else {
            return XCTFail("真实档不该没有 2σ 安全带")
        }
        XCTAssertEqual(b2.lowerBound, 10.06, accuracy: 0.05)
        XCTAssertEqual(b2.upperBound, 12.47, accuracy: 0.05)
        // 窗口宽 11，安全带宽不到 3 —— 噪声吃掉了一大半
        XCTAssertLessThan(b2.upperBound - b2.lowerBound, 3.0)
    }

    /// ★ 严苛档：**交集为空**是真实结论，不是 bug（works.md §5.3）
    func testHarshHasNoGreenBandAtAll() {
        let sigma = PreviewMath.sigmaApparent(dt: 1.0, level: .harsh)
        XCTAssertNil(PreviewMath.band(sigmaV: sigma, levels: 2))
        // 而且没有任何速度能得到绿色
        for kmh in stride(from: 6.0, through: 17.0, by: 0.1) {
            XCTAssertNotEqual(PreviewMath.color(kmh: kmh, sigmaV: sigma), .green,
                              "\(kmh) km/h 在严苛档被判绿了 —— 违反 §5.3")
        }
    }

    /// §5.4 判定列 —— 注意 findings F2：规则表与示例表有分歧，实现按**规则**走
    func testColorsFollowRuleTableNotDocColumn() {
        let sigma = PreviewMath.sigmaApparent(dt: 1.0, level: .realistic)
        // 🟢 行：2σ 完全安全
        XCTAssertEqual(PreviewMath.color(kmh: 10.2, sigmaV: sigma), .green)
        XCTAssertEqual(PreviewMath.color(kmh: 12.0, sigmaV: sigma), .green)
        XCTAssertEqual(PreviewMath.color(kmh: 12.4, sigmaV: sigma), .green)
        // 🔴 行：1σ 也越界
        XCTAssertEqual(PreviewMath.color(kmh: 6.0, sigmaV: sigma), .red)
        XCTAssertEqual(PreviewMath.color(kmh: 8.0, sigmaV: sigma), .red)
        XCTAssertEqual(PreviewMath.color(kmh: 16.5, sigmaV: sigma), .red)
        XCTAssertEqual(PreviewMath.color(kmh: 17.0, sigmaV: sigma), .red)
        // 🟡 行：F2 —— 文档 §5.4 标 🔴，但按 §5.3 规则应为 🟡
        for kmh in [9.0, 13.0, 14.0] as [Double] {
            XCTAssertEqual(PreviewMath.color(kmh: kmh, sigmaV: sigma), .yellow,
                           "\(kmh) km/h：§5.3 规则判黄，§5.4 示例列标红（F2 已登记此分歧）")
        }
    }

    // MARK: - 滑块映射（§5.2）

    /// 端点必须**恰好压在规则边界上** —— 这是验收标准之一
    func testSliderEndsAtRuleEdges() {
        for paceLinear in [true, false] {
            let mode = paceLinear ? "配速线性" : "速度线性"
            let fLo = PreviewMath.sliderFraction(for: Rules.vMin, paceLinear: paceLinear)
            let fHi = PreviewMath.sliderFraction(for: Rules.vMax, paceLinear: paceLinear)
            XCTAssertEqual(fLo, 0, accuracy: 1e-9, "\(mode)：低端没压在 R3 上")
            XCTAssertEqual(fHi, 1, accuracy: 1e-9, "\(mode)：高端没压在 R4 上")
            // 往返一致
            XCTAssertEqual(PreviewMath.speedForFraction(fLo, paceLinear: paceLinear),
                           Rules.vMin, accuracy: 1e-9, "\(mode)：往返低端不一致")
            XCTAssertEqual(PreviewMath.speedForFraction(fHi, paceLinear: paceLinear),
                           Rules.vMax, accuracy: 1e-9, "\(mode)：往返高端不一致")
            // 任意点往返自洽
            for kmh in stride(from: 6.0, through: 17.0, by: 0.5) {
                let back = PreviewMath.speedForFraction(
                    PreviewMath.sliderFraction(for: kmh, paceLinear: paceLinear),
                    paceLinear: paceLinear)
                XCTAssertEqual(back, kmh, accuracy: 0.01, "\(mode)：\(kmh) km/h 往返失真")
            }
        }
    }

    /// 配速线性下中点**不等于**速度中点 —— 这正是 §5.2 选配速线性的理由
    /// （跑步者的心智单位是配速，不是 km/h）
    func testPaceLinearMidpointIsNotSpeedMidpoint() {
        // 配速端点：17 km/h → 3:31.8，6 km/h → 10:00
        // 中点是**配速的算术中点** (600 + 211.76) / 2 = 405.88 s = 6:45.9
        let paceMid = (PreviewMath.pace(kmh: Rules.vMax) + PreviewMath.pace(kmh: Rules.vMin)) / 2
        let midSpeed = PreviewMath.speedForFraction(0.5, paceLinear: true)
        XCTAssertEqual(midSpeed, 3600.0 / paceMid, accuracy: 0.01,
                       "配速线性中点应对应配速 \(Int(paceMid.rounded()) / 60):\(Int(paceMid.rounded()) % 60)")
        XCTAssertEqual(midSpeed, 8.8696, accuracy: 0.01)
        // 与速度线性的算术中点 11.5 差得离谱 —— 这就是选配速线性的意义
        XCTAssertNotEqual(midSpeed, (Rules.vMin + Rules.vMax) / 2, accuracy: 1.0)
        // 速度线性下中点才是算术平均 11.5
        XCTAssertEqual(PreviewMath.speedForFraction(0.5, paceLinear: false), 11.5, accuracy: 1e-9)
    }

    /// 超出 0…1 的输入必须夹紧，不能外推出非法速度
    func testFractionIsClamped() {
        XCTAssertEqual(PreviewMath.speedForFraction(-3, paceLinear: true), Rules.vMin, accuracy: 1e-9)
        XCTAssertEqual(PreviewMath.speedForFraction(9, paceLinear: true), Rules.vMax, accuracy: 1e-9)
        XCTAssertEqual(PreviewMath.speedForFraction(-3, paceLinear: false), Rules.vMin, accuracy: 1e-9)
        XCTAssertEqual(PreviewMath.speedForFraction(9, paceLinear: false), Rules.vMax, accuracy: 1e-9)
    }

    /// 贴线时必须关闭夹紧 —— 贴线正是要测的情形（§3.2 末尾的 ⚠️）
    func testClampDisabledAtRuleEdges() {
        XCTAssertFalse(PreviewMath.clampEnabled(kmh: 6.0))
        XCTAssertFalse(PreviewMath.clampEnabled(kmh: 17.0))
        XCTAssertTrue(PreviewMath.clampEnabled(kmh: 10.0))
    }

    /// ★★ 回归护栏：`speedKmh` 的 setter **绝不能递归**。
    ///
    /// 曾经的写法是 `@Published var speedKmh { didSet { speedKmh = clamp(...) } }`，
    /// 因为 `@Published` 把 setter 包成了 `withMutation`，Swift 的
    /// 「观察器里给同一属性赋值不再触发自己」保护失效 ⇒ 无限递归。
    /// 真实后果：拖一下滑块整个 app 爆栈崩溃
    /// （诊断报告 `Thread stack size exceeded due to excessive recursion`，
    /// 递归 87059 帧全在 `speedKmh.setter` ↔ `speedKmh.didset` 之间）。
    ///
    /// 这条测试在旧实现下会**直接把测试进程打死**，所以它是真的护栏，
    /// 不是走过场的断言。
    @MainActor
    func testSpeedSetterDoesNotRecurse() {
        let s = SessionController(sidecar: SidecarClient(repoRoot: URL(fileURLWithPath: ".")),
                          route: RouteModel())
        // 模拟滑块被快速拖动：跑满 6.00 → 16.99 再来回两遍，共 2200 次写入。
        // 每一次都会走一遍 clamp —— 旧实现正是在这里无限递归。
        for _ in 0..<2 {
            for i in 0..<1100 { s.speedKmh = 6.0 + Double(i) / 100.0 }
        }
        XCTAssertEqual(s.speedKmh, 16.99, accuracy: 1e-9)
    }

    /// 夹紧不变式仍然成立（改成计算属性后不能丢）
    @MainActor
    func testSpeedSetterStillClamps() {
        let s = SessionController(sidecar: SidecarClient(repoRoot: URL(fileURLWithPath: ".")),
                          route: RouteModel())
        s.speedKmh = -100;  XCTAssertEqual(s.speedKmh, Rules.vMin)
        s.speedKmh = 999;   XCTAssertEqual(s.speedKmh, Rules.vMax)
        s.speedKmh = 12.34; XCTAssertEqual(s.speedKmh, 12.34, accuracy: 1e-9)
        // NaN 不能把面板变成空白
        s.speedKmh = .nan;  XCTAssertEqual(s.speedKmh, Rules.vMin)
    }

    // MARK: - 判定预期

    func testVerdictForStandardRun() {
        let eta = PreviewMath.closedFormEta(distanceM: 3000, kmh: 10)
        let sigma = PreviewMath.sigmaApparent(dt: 1.0, level: .realistic)
        let v = PreviewMath.verdict(distanceM: 3000, kmh: 10, etaS: eta, sigmaV: sigma)
        XCTAssertEqual(v["R1"], "✓", "3.00 km 恰好达标")
        XCTAssertEqual(v["R2"], "✓")
        XCTAssertEqual(v["R5"], "✓", "18 分钟 < 2 小时")
    }
}
