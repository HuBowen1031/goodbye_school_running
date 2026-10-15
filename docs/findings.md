# findings —— 实现过程中发现的文档与规格问题

> 本文件记录**文档说了一件事、而代码或数学说了另一件事**的地方。
> 原则：不擅自改文档，也不默默绕过 —— 把分歧写下来，标注证据与处置方式。

---

## F1 ★ σ 的舍入值被代入了后续计算

**涉及**：works.md §3.5 → §5.3 → §5.4

§3.5 的表把 1 Hz 的表观速度抖动显示为 `2.0 km/h`，这是**四舍五入后的显示值**。
但 §5.3 的安全带公式和 §5.4 的 golden 表，都把这个 `2.0` 当作**计算输入**继续使用。

精确值：

```
σ = √2 · √(d_slow² + d_fast²) / Δt
  = 1.9552 km/h        （1 Hz，真实档）
```

**由此产生的系统性偏移**：

| 量 | 用 σ=2.0（文档） | 用 σ=1.9552（精确） | 差 |
| --- | --- | --- | --- |
| 2σ 安全带下沿 | 10.152 | 10.061 | −0.091 |
| 2σ 安全带上沿 | 12.381 | 12.466 | +0.085 |
| 1σ 安全带下沿 | 8.122 | 8.076 | −0.046 |
| 1σ 安全带上沿 | 14.329 | 14.386 | +0.057 |

§5.4 各行的 `[v_lo, v_hi]` 也因此普遍偏 0.04–0.14。

**处置**：实现保留精确值（**显示层该舍入，计算层不该**）。
`tests/golden/estimate.json` 的 `tolerances.v_lo/v_hi` 设为 ±0.15 以容纳这个口径差，
并在 `_why_v_lo_wide` 里写明原因。

> 这不是 bug，是**精度纪律**问题。若将来要让 §5.4 与实现逐位对齐，
> 正确做法是把文档的 σ 列改成 `1.9552 (显示 2.0)`，而不是把实现改回 2.0。

---

## F2 ★ works.md §5.3 与 §5.4 对判定颜色的定义不一致

**涉及**：works.md §5.3 判定表 vs §5.4 判定列

§5.3 给出了**规则**：

| 条件 | 颜色 |
| --- | --- |
| `v_lo ≥ 6` 且 `v_hi ≤ 17` | 🟢 |
| 1σ 内安全、2σ 越界 | 🟡 |
| 其余 | 🔴 |

§5.4 的**示例表**里，9.0 / 13.0 / 14.0 三行被标为 🔴。但它们的 1σ 区间并没有越界：

```
9.0  ： 1σ 下界 = 9.0×0.985 − 1.9552 = 6.92 > 6      → 按规则应为 🟡
13.0 ： 1σ 上界 = 13.0×1.05 + 1.9552 = 15.61 < 17    → 按规则应为 🟡
14.0 ： 1σ 上界 = 14.0×1.05 + 1.9552 = 16.66 < 17    → 按规则应为 🟡
```

（对照：8.0 的 1σ 下界 = 5.88 < 6，确实越界，标 🔴 是正确的。）

**处置**：**规则定义优先于示例表**。

- 实现按 §5.3 判色（`safety_color`）。
- §5.4 的原值保留在 `tests/golden/estimate.json` 的 `doc_safety` 字段，不篡改。
- 分歧行登记在 `document_contradictions`，并由
  `TestGoldenTable::test_safety_disagreements_are_registered` 守住 ——
  将来若有人"顺手"改掉这些行，测试会失败。

**影响面**：只影响色带的黄/红边界，不影响 🟢 区间。
两种口径都指向同一可操作结论：**只有 [10.2, 12.4] km/h 才是真正安全的**。

---

## F3 ★ §5.4 的 17.0 行两个时间列与公式对不上

**涉及**：works.md §5.4 最后一行

| 列 | 文档 | 公式 | 差 |
| --- | --- | --- | --- |
| 纯运动 | 10:42（642 s） | 10:35（635.3 s） | +6.7 s |
| 修正 ETA | 10:47（647 s） | 10:42（641.9 s） | +5.1 s |

**关键证据**：其余 **10 行**的两列都吻合到 ±2 s，只有这一行两列同时偏大。

一个可能的解释：

```
647 s ≈ 3000 / (16.7 / 3.6) = 646.7 s
```

`16.7` 正是默认参数里的 `v_ceil`。也就是说，这一行可能在某个中间版本
按「clamp 生效」的口径算过 —— 但 §5.4 表头的上下文明确写的是贴线设定。

**处置**：无法从现有材料判定是笔误还是口径差异，故

- **不改文档**；
- golden 契约里标 `known_anomaly: true`，`test_row` 跳过这一行的 eta 列对照；
- 实现值由 `test_row_17_legal_but_very_noisy` 单独锁定（635.3 / 641.9）。

---

## F4 折线拐角会让 `v_actual` 产生几何抖动

**性质**：这不是文档问题，是**建模的固有性质**，但会误导测试断言。

`v_actual` 由相邻两点的**大圆距离**除以时间间隔得出。
在路线的直角拐角处，两点间直线距离小于沿路线的弧长，于是该点的 `v_actual` 会偏低。

实测（3 km，10 km/h，1 Hz，**关掉所有随机噪声**）：

```
v_actual  stdev = 0.2076 km/h
v_actual  range = 7.338 .. 10.335 km/h
```

其中 7.338 是起步 ramp 段的真实低速，而 stdev 的 0.2 里有相当一部分来自拐角。

**处置**：`test_off_has_much_less_jitter_than_realistic` 用
「关噪声的抖动 < 0.8，且至少比真实档小 2.5 倍」这种**相对**断言，
而不是给绝对阈值 —— 后者会被路线形状偶然影响。

---

## F5 AR(1) 样本方差对长相关序列有系统性负偏差

**性质**：统计估计量的性质，不是实现问题，但会让噪声测试"无故"失败。

对 AR(1)，样本方差的期望：

```
E[s²] ≈ σ² · [1 − (1/n)·(1+ρ)/(1−ρ)]
```

而 `(1+ρ)/(1−ρ) ≈ 2τ/Δt`（当 Δt ≪ τ 时）。所以

```
E[s²] ≈ σ² · (1 − 2τ/T)
```

慢分量 `τ = 300 s`：

| 观测时长 T | σ 的低估幅度 |
| --- | --- |
| 4 000 s | 15.0% |
| 8 000 s | 7.5% |
| 20 000 s | 3.0% |

首次跑测试时，用「单种子 20 000 点」估计 σ，得到 `2.67` 而非 `3.015` ——
**正好落在这条偏差线上**。

**处置**：`tests/test_noise.py::_ensemble_sigma` 做了两件事：

1. **多种子平均**（慢分量导致有效独立样本只有 ~67 个）；
2. **按分量加权解析修正**偏差因子 `1 − 2τ/T`。

这样容差可以收紧到 ±10%，而不是靠"拍脑袋放宽"过关。

---

## F6 `η(t)`（呼吸/路况速度波动）在初版实现里被漏掉了

**涉及**：works.md §3.2 剖面表

§3.2 明确列出了 5 个剖面分量，其中：

| 分量 | 参数 |
| --- | --- |
| `η(t)` 呼吸 / 路况引起的速度波动，AR(1) | `σ_v = 0.35 km/h`，`τ_v = 10 s` |

初版 `SpeedProfile` 只实现了 ramp / drift / finish / clamp 四项，
`speed_at()` 虽然**接受** `eta` 参数，但 `TrackEngine` 从未传入 —— 即 η 恒为 0。

**处置**：补上 `SpeedProfile.sigma_v / tau_v` 字段与 `noise.SpeedJitter` 类。

设计上的一个刻意选择：**η 与 GPS 位置噪声走两条独立的随机流**。

- 位置噪声描述「**测量误差**」，η 描述「**跑者速度的自然起伏**」—— 物理来源不同；
- 混在一条流里会让「换个噪声档位」意外改变速度剖面，并让 §3.5 的表观抖动口径含糊；
- 但两者都从同一个人工种子经偏移派生，所以「同种子 ⇒ 同轨迹」依然成立。

**副产品**：η 是零均值波动，会让实测 ETA 产生 ±4 s 左右的**合法**抖动
（`ΔT/T ≈ −η̄/v̄`，`σ(η̄) ≈ σ_v·√(2τ_v/T)`）。
因此「实测 ETA 与预测一致」这类验收测试必须先 `eta_sigma=0.0` 关掉 η，
否则测的是运气而不是一致性。

---

## F7 ★ 侧车 Python 的绝对路径与实测不符（会直接让功能失效）—— 已修复

**涉及**：docs/file-manifest.md §5.2

文档要求 `SidecarClient` 用**绝对路径**启动侧车：

```
/Users/yuanjun/miniconda3/envs/vrun/bin/python
```

但本机 conda 的 `envs_dirs` 配置把新建环境放在了 `~/.conda/envs/`：

```
$ conda env list | grep vrun
vrun    /Users/yuanjun/.conda/envs/vrun
```

`/Users/yuanjun/miniconda3/envs/vrun/` **并不存在**（实测 `ls` 报 ENOENT）。

**影响等级**：与 F1–F6 不同，这不是精度或口径问题 ——
Swift 侧车会**启动失败**，整个 M1 的 GUI 部分无法工作。

**处置（已实施，2026-10-11）**：采纳「解析 prefix」方案，不写死任何解释器路径。

新增 `scripts/resolve_python.sh` 作为解释器路径的**唯一入口**，解析顺序：
`$VRUN_PYTHON` → `conda config --show envs_dirs` 探测 →
`conda env list --json` 取 prefix → `~/miniconda3/envs` 兜底。
完整规则与设计纪律见 file-manifest.md §7.1。

本机实测（本轮）：

```
$ scripts/resolve_python.sh
/Users/yuanjun/.conda/envs/vrun/bin/python      # 规则 2 一次命中

$ VRUN_ENV_NAME=vrun-nope-xyz scripts/resolve_python.sh
resolve_python: 找不到环境 'vrun-nope-xyz' 的 Python 解释器。   # exit 1，不回落
```

**为什么不选「最小修正」（直接把路径改成 `~/.conda/envs/...`）**：
那只是把一个错误的硬编码，换成一个**暂时正确**的硬编码。`envs_dirs` 是可配置的，
换台机器、或改一次 conda 配置就会再次失效 —— 而这次的代价是「GUI 一启动就失败」，
属于最难排查的一类问题。

**连带修正**：`docs/file-manifest.md` §5.2 / §7（新增 §7.1）、`works.md` §4.4 与 M0 清单。

**回归护栏**：新增 `tests/test_env_resolution.py`（7 条用例），断言解出的解释器
可用、版本为 3.12、含项目依赖，且环境名不存在时**报错而非静默回落**。

> `conda` **本体**的路径（`~/miniconda3/bin/conda`）仍保持全路径写法，这是有意的：
> miniconda3 的安装位相对稳定，而**环境**的位置不稳定。两者稳定性不同，不该一刀切。

---

## F8 ★ `simulate-location set` CLI **永久阻塞** —— 逐点注入在 CLI 层面不可能

**涉及**：works.md §4.1「为什么不用 CLI 子进程逐点注入」

works.md 给出的理由是：

> CLI 每次启动开销 300–800 ms，1 Hz 都稳不住。必须用**常驻进程的 Python API**。

**实测（2026-10-12，生死验证 1）发现真实原因比这强得多。** 读 `cli/developer/dvt/simulate_location.py`：

```python
@cli.command("set")
@async_command
async def dvt_simulate_location_set(service_provider, latitude, longitude) -> None:
    async with DvtProvider(service_provider) as dvt, LocationSimulation(dvt) as location_simulation:
        await location_simulation.set(latitude, longitude)
        OSUTILS.wait_return()        # ← 设完就永久阻塞，等 Ctrl-C
```

而 `clear` **没有**这一行：

```python
@cli.command("clear")
@async_command
async def dvt_simulate_location_clear(service_provider) -> None:
    async with DvtProvider(service_provider) as dvt, LocationSimulation(dvt) as location_simulation:
        await location_simulation.clear()      # ← 设完就退出
```

**后果**：

1. `set` 不是「设完就走」的命令，而是一条**必须常驻**的命令 ——
   它要靠 `async with` 保持 DVT 连接存活，进程一退出上下文就析构。
2. 因此 **CLI 层面根本不存在「循环注入」的形态**。
   works.md 说的「启动开销 300–800 ms」是**次要**理由；
   就算开销是 0，这条命令也用不了 —— 它一次只能设一个点然后就卡住了。
3. 实测印证：`set` 发出后一直不返回，120 s 后被 harness SIGTERM（`Terminated: 15`）；
   `clear` 则立即返回、无输出。

**结论不变，理由要换**：

> ✅ 「必须用常驻进程的 Python API」—— **works.md 的决策是对的**
> ⚠️ 但支撑它的理由不完整。若按原理由去质疑这个决策
> （"优化一下启动开销不就行了？"），会得出错误结论。
> **真实理由是结构性的：CLI 没有循环形态。**

**处置**：

- M2 的 `DeviceBridge` 必须是**常驻进程**（works.md §4.2 已是此设计，此条只是补上硬证据）；
- 逐点注入循环要自己写，仿照 `LocationSimulation.play_gpx_file()` 的思路
  （works.md §1.2 已指出该函数「本质是按时间戳 sleep 后循环 set()」）；
- `clear` 可以安全地用作**独立的一次性进程**，因为它不阻塞 ——
  这是 `scripts/panic-clear.sh` 的技术前提。

> 附带确认 works.md §1.2 的另一条：`LocationSimulation` 只有
> `set(latitude, longitude)` 与 `clear()` 两个方法，
> **确实没有** speed / course / altitude / timestamp。§2.1「时间不可伪造」的前提再次坐实。

---

## F9 ★ 存在一条比 `device.py` 更省的注入路径（但不完整）

**涉及**：works.md §4.2「M2 设备桥」的设计取舍

works.md 规划 M2 写 `device.py` 封装 `LocationSimulation`，
在**常驻进程**里自己写循环逐点 `set()`。

**2026-10-12 实测发现**：这个循环**不用写** —— 上游已经提供了：

```bash
vrun gpx --route X.json --speed 10 --out logs/run.gpx     # ① M1 离线算轨迹
pymobiledevice3 developer dvt simulate-location play -- logs/run.gpx   # ② 回放到真机
```

`play_gpx_file()` 内部就是「按 `<time>` 差值 sleep → 循环 `set()`」，
一份带时间戳的 GPX 就是一条完整轨迹。**实测 166 m / 66 s / 9–10 km/h 一次跑通，
校园 App 计数正确、零告警。**

### 为什么这条路径值得单独记

| | `play` 回放（现成） | 自写 `device.py` |
| --- | --- | --- |
| 隧道 / DVT 连接管理 | **上游包了** | 自己写（lockdown → RemotePairing → tunnel → RSD） |
| 轨迹一致性 | **离线算的与注入的逐字节一致** | 实时算，时钟抖动会进入轨迹 |
| 中途改速度（§5 滑块） | ❌ **做不到** | ✅ |
| 暂停 / 恢复 | 杀进程 + clear + 从当前位置重生成 | ✅ |
| 实时遥测回 GUI | ❌（但轨迹是我们自己算的，本就知道） | ✅ |

### 结论：**两条都要，但不是替代关系**

- **`play` 覆盖「跑一次完整会话」** —— S2 冒烟检查（works.md §7.2）用它就够了，
  而且零维护成本。M4 的验收可以现在就做，不必等 M2 写完。
- **自写 `device.py` 只为一件事：§5 的速度滑块** —— 「跑到一半改速度」
  在 `play` 模型下不存在，因为轨迹已经是固定文件了。

> ⚠️ 换句话说：**M2 的必要性从「能注入」降级为「能中途改参数」**。
> 这不改变里程碑顺序，但显著改变了 M2 的**风险等级** ——
> 它不再是「不做就什么都做不了」，而是「不做就只有静态轨迹」。

> 附：`play` 回放完同样落在 `OSUTILS.wait_return()` 上永久阻塞（F8 同样适用于 `play`），
> 所以进程回收仍是必需的。三保险（works.md §2.3）对它一视同仁。

---

## F10 闭式解与精确积分的 0.76 s 偏差会翻转**显示的秒数**

**涉及**：works.md §3.6 闭式解、§5.4 基准表、§5.6 「预览 vs 精确」自检

**发现方式**：写 Swift 跨语言契约测试（`PreviewMathTests`）时，
把 `closedFormEta` 拿去逐行比 §5.4 表格的「修正 ETA」列，12.4 km/h 那行对不上。

**根因**：比错了列，但暴露了一个真问题。

| | 12.4 km/h @ 3 km |
| --- | --- |
| 纯运动 | 870.97 s |
| 闭式解（§3.6，GUI 拖动时用） | **878.76 s** → `Int(round())` → 879 → **14:39** |
| 精确积分（Python `expected_duration`，权威） | **878.0 s** → 878 → **14:38** |

两侧**公式一字不差**，差的 0.76 s 是一阶近似本身的残差。
问题不在精度，而在**取整边界**：878.76 离 878.5 只有 0.26 s，
于是「预览 14:39 / 实测 14:38」这种 1 秒的可见不一致真的会发生。

**为什么 §5.6 的自检抓不到**：§5.6 规定差值 > 1 s 时 GUI 高亮提示。
这里差 0.76 s < 1 s，**不触发提示**，但显示值确实差了一格。

**处置**（已落实）：

- 测试改成**秒级容差断言**（`|闭式解 − 精确| ≤ 1 s`），不再断言文本相等 ——
  文本相等在取整边界上是脆的，容差才是真正的契约。
- 新增 `testF10RoundingBoundaryAtTwelvePointFour`，把这个已知偏差**钉死**为
  878.76 s / 「14:39」。谁改动闭式解，这条测试会立刻报出来。
- 12.4 行在文本断言里显式排除，并注明原因（与 F3 对 17.0 行的处理一致）。
- GUI 行为**不变**：14:39 是闭式解的诚实输出，实测值由侧车的 `eta_s`（精确积分）
  在松手后替换，用户看到的最终读数仍是 14:38。预览与实测的秒级差异属预期。

> 顺带说明：§5.4 表格那一列是**精确积分**，不是闭式解。
> 这一点原来没写清楚，是本次做契约测试时才发现的 —— 已在本条记录备案。

---

## F11 ★ 会话层有三个 bug，**只有写单测才能抓**（真机验不出）

**涉及**：works.md §6 M2 的 `session.py`

6 点真机移动测试**通过了**（蓝点跟着走、清理正常）。
但那**只证明了设备层通**。把引擎接上去写会话层时，单测连着揪出三个 bug ——
它们的共同点是：**真机上跑 18 分钟也未必看得出，单测一秒就炸**。

### ① ★★ 会话从头到尾没有注入过任何位置

`_on_sample_blocking` 只发 tick / progress 事件，**根本没调 `set_location`**。

后果极其隐蔽：tick 照发、progress 照报、状态机照跑、统计照算 ——
**一切看起来都正常，但手机上一个点都不会动**。
真机上要发现它，得先等 18 分钟跑完、再去翻 App 的记录看距离为 0。

> 钉它的测试就是背压那条：断言「tick 数 == 注入数 == 采样点数」。
> 用 `+1` 的**精确式子**而不是 `>=` —— 少算 `prepare()` 那次就会差 1，
> 而这个差 1 正是漏调的信号。

### ② `stop()` 先等线程、后置状态 —— 「停止」要等整条路线跑完

`_sleeper` 靠检查 `phase` 决定该不该退出，但 `stop()` 写的是
「先 `await _wait_task()`，再把 phase 改成 STOPPED」。
于是引擎看到的永远是 RUNNING，**停止信号根本到不了它**。

真机上点「停止」，实际效果是**再等 18 分钟**。

> 解法：拆出一个独立的 `_stop_requested` 标志，`stop()` 先置它再等线程。
> 刻意**不与 `phase` 合并** —— `phase` 是对外报告的状态，
> `stop()` 执行的那一刻它本来就该还是 RUNNING。合成一个字段会让「停止」重新失效。

### ③ 保险注册是**进程级**的，测试里必须显式收摊

`SafetyGuard.arm()` 注册的 `atexit` 与 signal 处理器**不随对象消亡**。
十几条会话用例各 arm 一次，就等于在解释器退出时起十几个真子进程去连真机 ——
套件尾部静默挂几十秒，报错点还落在「测试全部跑完之后」，
看起来就像 pytest 本身卡死了。

> 正常流程里保险本来就该留到最后，所以这不是 bug；
> 但测试里必须拆。加了一个 autouse fixture 追踪所有 arm 过的 guard 并收摊。

**共同教训**：这三类 bug 的可观测性都很差 —— 失败时**没有报错、没有异常、日志正常**。
唯一可靠的抓法是**把不变量写成断言**（注入数 == 采样点数、状态迁移的顺序、进程级副作用的收尾）。
这也是为什么 `test_session.py` 的重点不是「跑通一次」，而是「跑了很多次之后状态仍然自洽」。

---

## F12 ★ 一条单测在**偷偷连真机**，表现为「随机的」套件卡死

**涉及**：`tests/test_device.py::test_boundary_coordinates_accepted`

### 症状

同一个测试套件，连着跑两次：

- 第一次：`293 passed in 20.96s`
- 第二次：挂住 > 60 s，杀不掉

单独跑 `tests/test_session.py`：23 条全过。
单独跑全量带 `-v`：稳定停在 `test_boundary_coordinates_accepted` 这一条上。

**没有任何报错、没有异常、日志正常** —— 和 F11 属于同一类可观测性陷阱。

### 根因

`DeviceBridge.set_location()` 的结构是「**先校验，再连接，再注入**」：

```python
if not (-90.0 <= lat <= 90.0):          # ← ① 校验闸门
    raise ValueError(...)
await self.connect()                     # ← ② 真正的硬件动作
```

而这条测试要验的恰恰是「**±90 / ±180 是合法边界，不能误拒**」——
坐标合法，于是它一路穿过 ①，直奔 ②。

`test_device.py` 里其余测试都安全：它们只碰 `clear()` / `aclose()` / `status()`，
这三个都**不触发连接**；参数非法的那些在 ① 就被拒了。**只有这一条会走到 ②。**

### 为什么表现得像「随机抖动」

结果完全取决于手机在不在线：

| 手机状态 | 表现 |
| --- | --- |
| 没插，或 usbmuxd 查不到设备 | 快速抛 `DeviceNotFound` → 测试「通过」 |
| 插着 | usbmuxd 找到设备 → 真的去建隧道 → **整个套件挂死** |

于是「手机插上再跑测试」这件事本身，就成了触发条件。
而套件 293 条用例跑完只要 20 s，一个真的在建隧道的连接足以把整个进程钉住。

### 处置

加 `validation_only_bridge` fixture：把 `connect()` 换成空实现 + 一个假注入器
（只提供 `async set(lat, lon)` 的桩）。校验闸门一个不差地被覆盖，硬件彻底隔离在外。

顺带把断言改严了 —— 原来那个 `except Exception: pass` 会把「连不上设备」
和「校验通过」混为一谈，现在直接断言边界坐标**确实进了注入器**。

### 顺带核实的一件事（**结论：没问题**）

`device-status` 会调 `bridge.connect()`，看上去「一个状态查询却武装了手机定位」。
查 pymobiledevice3 源码确认：`DtxService.__aenter__` **只开 DTX 通道**，
真正武装定位的是 `simulateLocationWithLatitude:longitude:`（即 `set`）。
所以 `connect()` 本身不动手机的定位状态，`aclose()` 里那句 `clear()` 只是保险。
**这是核实后的结论，不是「看起来没问题」** —— 写在这里免得下次再怀疑一遍。

### 教训

「设备层单测不得碰真机」这条纪律，光靠**逐个测试检查**是守不住的 ——
`set_location()` 里校验通过就会连设备，这个副作用对调用方是不可见的。

有效的护栏是**在 fixture 层面把硬件换掉**，让「忘了加 fixture」这件事
在物理上不可能发生 —— 而不是指望每个测试作者都记得。

> 对照 F7（侧车解释器解析）：那次的 bug 也是「单测全绿但实际会失败」，
> 同属「测试环境与生产环境不一致」这一类。两次的抓法都是**让测试跑在真实形态上**。

---

## F13 ★ 崩溃栈顶的符号**不是**真凶；纯逻辑测试对框架内部抛异常完全无能为力

**涉及**：`app/VRun/Sources/VRunApp/RouteCanvas.swift`（`GreenDotAnnotationView`）、
`app/VRun/Tests/VRunAppTests/GreenDotMapKitTests.swift`（新增）

### 症状

GUI 一改完就出现「点一下地图，进程 abort」。崩溃报告（`asiBacktraces`）逐帧是：

```
objc_exception_throw
-[MKAnnotationView clusteringIdentifier]            ← 栈顶
-[MKMapView addAnnotationRepresentation:allowAnimation:]
-[MKAnnotationManager updateVisibleAnnotations]
-[MKMapView _didChangeRegionMidstream:]
-[MKMapView mapLayerDidChangeVisibleRegion]
VectorKit -[VKMapCameraController _animateToPosition:…]
-[MKMapView setCenterCoordinate:animated:]          ← 就是 updateNSView 里那一行
RouteCanvas.updateNSView(_:context:)
```

主线程栈显示 `-[NSApplication _crashOnException:]`，没有任何 reason 字符串。

在此之前，钢笔与绿圈一共有 **26 条测试，全绿**；编译零警告；加点数据全对；
连 `animated: false` 都改过了。**三件事同时成立，进程照样死。**

### 根因

`canShowCallout = true`，而 `DeviceAnnotation` 没有 `title`：

```
<…DeviceAnnotation: 0x…> must implement title, or view must have a
non-nil detailCalloutAccessoryView when canShowCallout is YES (NSGenericException)
```

MapKit 在把 annotation 装配成 view 时抛 ObjC 异常，没人接 ⇒ `abort()`。

### 两次**错误**的归因（都记下来，因为它们各自都"有证据"）

**其一：把栈顶符号当成了真凶。**
`clusteringIdentifier` 正好是头文件里写着「留给子类实现」的属性，
看着就是标准答案。于是去覆盖 getter。**但变异测试证明这是错的** ——
把 getter 覆盖删掉、只留 `init` 里的 `clusteringIdentifier = nil`，5 条测试照样全绿。
赋值本身就等于「实现」了这个属性。

**其二：把崩溃归给了 `animated: true` 的定时器交错。**
2026-10-14 早些时候把 `setCenter` / `setVisibleMapRect` 全改成 `animated: false`，
理由写的是「动画期间 MapKit 在 `VKTimedAnimation` 定时器回调里重装配标注，
与 SwiftUI 的下一轮 `updateNSView` 交错」。重读崩溃栈才发现：
**即使 `animated: false`，`setCenter` 也是同步走完同一条链的**
（`setCenterCoordinate:animated:` → `_animateToPosition:` → `mapLayerDidChangeVisibleRegion`
→ `updateVisibleAnnotations`）。整套「定时器交错」的解释都不成立，
无动画只是顺手绕开了另一条不稳定路径。

顺带澄清一个被当成独立 bug 的现象：**「第 2 次加点不重绘」和这次闪退是同一个 bug**。
第 1 次加点确实重绘了（日志可证 `MainView.body 求值 route=1`），
紧接着 `updateNSView` 里的 `setCenter` 把进程带走 —— 于是第 2 次点击打在死进程上。
前期为此折腾过 `@Published` 通知、`@MainActor` 隔离、观察链转发，
方向全错：**先分清「界面没更新」和「进程已经没了」。**

### 处置

1. `canShowCallout = false`，去掉那个 `target: nil, action: nil` 的死按钮
   （绿圈只是「起点」的视觉标记，本来也不需要交互）。
2. 新增 `GreenDotMapKitTests.swift`：把绿圈**真塞进带窗口的 `MKMapView`**，
   走一遍 `addAnnotation` + `setCenter`。
3. 修正 `RouteCanvas.swift` 里三处**已经站不住的注释**（动画交错、必须覆盖 getter、崩溃栈顶符号）。
   错误的原因比没有原因更贵 —— 它会把下一个人引到同一个坑里。

### 两条测试纪律

**其一：纯逻辑测试证明不了「框架会接受这个对象」。**
点击判定、锚点取值、`@Published` 通知，全都是我们自己写的逻辑，
而 abort 发生在 **MapKit 内部** —— 纯逻辑测试根本没有能力覆盖那一层。
只要往 MapKit / AppKit 这类框架里塞对象，就必须有一条真把对象交给框架、
让它走一遍内部装配路径的测试。

**其二：XCTest 会把 abort 变成可捕获的异常，于是能拿到 reason。**
裸跑只看到一个没有 reason 的 `abort`；放进测试里，MapKit 的原话直接印在失败信息上。
**这正是「测试能问出框架为什么不接受」的唯一途径。**

写这条集成测试时还踩了一个更隐蔽的坑，记在测试文件里：
`MKMapView.delegate` 是 **`weak`**。写成 `map.delegate = Coordinator(...)`
的话 Coordinator 当场析构 ⇒ MapKit 走默认路径 ⇒ **永不崩 ⇒ 测试假绿**。
必须用局部强引用把它按在生命周期里。这条已经写成
`test绿圈的view确实被MapKit取走过` 这条护栏测试常驻。

---

## F14 ★ 跨语言协议两侧**都不报错**：`Decodable` 成功不等于字段对上了

**涉及**：`app/VRun/Sources/VRunApp/Models.swift`（`SidecarEvent`）、
`app/VRun/Sources/VRunApp/SessionController.swift`（`apply(_:)`）、
`tests/golden/session_events.json`（新增）、`tests/test_session_golden.py`（新增）

### 症状

M3 的「实时视图」一直没人实现，但查下去发现**不是缺功能，是有一条一直在发、
但从来没被接过的数据线**：

- `session.py` 从 M2 落地起就一直按 1 Hz 发 `tick` + `progress`
- Swift 侧 `SidecarEvent` **压根没声明** `t / s / v_target / v_actual / sigma_v / …`
- `SessionController.apply(_:)` 里 `if ev.event == "device"`，
  其余事件连看都不看

后果：会话进行中，GUI 的 HUD 只有一个「进度：开始推进…」，
已走距离、用时、瞬时速度、抖动**一个都没有**。
而 M4 要做的「比对预览 ETA 与实际用时」，在这种情况下只能靠翻日志文本。

**全程零报错。** 编译干净、Python 侧照发、Swift 侧 `Decodable` 照成功。

### 为什么两侧都沉默

因为 `SidecarEvent` 的字段全是**可选**（`let t: Double?` 这种）。
`Codable` 的约定是：**JSON 里没有的键，解出来就是 `nil`，不算错**。
于是「Python 发了一个字段」与「Swift 压根不知道有这个字段」这两件事，
在运行时**完全无法区分**。

顺带一提：当时还挂着一个 `@Published var tick: (t, lat, lon, s)?`，
**从来没被赋过值**，是个死字段 —— 它让人以为「tick 已经被处理过了」。

### 处置

新增 `tests/golden/session_events.json`：**真跑一小段会话**（假设备、不碰硬件）
抓下第一个 `tick` 与第一个实时 `progress`，两侧都对着它写：

| 侧 | 文件 | 验什么 |
| --- | --- | --- |
| Python | `tests/test_session_golden.py` | 载荷的**键集合**与基准严格相等 |
| Swift | `LiveMetricsTests.swift` | 真去**解码**那份 JSON，数值逐字相等 |

比对**键集合**而不是逐值相等：浮点会随 numpy 版本合法地漂一点，
而**键名对不上才是真正的契约破裂**。

变异验证（协议漂移会不会被抓到）：
给 `tick` 多塞一个 `elevation` 字段 → Python 侧红，报出「基准 vs 实际」的键差异；
把 Swift 的 `case sigmaV = "sigma_v"` 拼成 `"sigma_V"` → **3 条 Swift 测试红**。

### 三条可复用的纪律

**其一：`Decodable` 成功 ≠ 字段对上了。**
可选字段的解码永远不会失败，所以「解码成功」这个信号在协议一致性上**零信息量**。

**其二：跨语言协议必须有「**双方共用的真实载荷**」。**
注意是**真实**——不是照着头文件手抄一份。`PreviewMathTests` 那种
「把 golden 的数值转抄进 Swift 测试」的写法在这里会**重演同一个 bug**：
转抄本身就是一次人工搬运，而这次 bug 的本质就是搬运没发生
（压根没想到要搬）。所以 Swift 侧直接读那份 JSON。

**其三：注册一个协议 wart，而不是顺手改协议。**
`progress` 事件名被**重载**了：`run.prepare` / `run.start` / `run.stop`
的应答（会话摘要）与 1 Hz 实时指标同名，载荷形状却不同 ——
现在靠「`sigma_v` 在不在」区分。

拆成两个事件名显然更干净，但那是**改协议**，会影响 Python 侧与已有文档。
在没有实测依据、也没有第二个消费方的情况下改它，属于超出问题范围的改动 ——
所以这里只登记、只给消费侧加测试守住这个约定
（Python 侧 `test_progress确实被重载…`），把决策留给真正要动协议的时候。

⚠️ 消费侧必须显式写判别式。写成 `case "progress":` 就完蛋：
GUI 会把「会话刚开始，这是摘要」当成一条实时读数，
显示出一个不动的 0 km/h —— **比不显示更难查**。

---

## F15 ★ 压测工具本身也会骗人：「await 回来了」不等于「跑完了」

**涉及**：`src/vrun/soak.py`（新增）、`src/vrun/device.py`（新增 `InjectionStats`）、
`tests/test_soak.py`（新增）

### 背景

M2 短测只有 6 个点、每点停 3 s。它证明的是「**能注入**」，
不是「18 分钟不漂、不漏、不卡」—— 而 M4 的 3 km 全程恰恰是 18 分钟。

works.md §6 M2 那条「实测 1 Hz 连续注入 10 分钟，统计丢失点数与**延迟分布**」
当时只落了 ``dropped`` 计数器，延迟分布这一半**没有埋点**。

### 写压测工具时踩到的三件事

**其一：`strict=True` 意味着单点失败会让整个会话中断。**

`RunSession._on_sample_blocking` 用的是 ``_submit_coro(..., strict=True)``，
所以注入失败**不是**「跳过一个点继续跑」，而是当场把引擎打挂。

⇒ 这修正了一个此前的表述：`DeviceStatus.dropped` 的注释写「非零说明轨迹有洞」，
实际上它非零时**会话已经失败了**。两者都成立，但只有后者是真的行为。
于是压测里 ``dropped > 0`` 必然同时伴随三件事：会话提前结束、一条 ``error`` 事件、
覆盖时长不够。测试把这一条钉住了。

**其二：`RunSession._wait_task()` 最多只等 5 秒。**

它内部是 ``wait_for(shield(task), timeout=5.0)`` —— 引擎还在正常跑时，
它 5 秒后就返回了。

我的压测 harness 一开始写成「先 `_wait_task()`、再等到点、再清理」，
于是 5 秒后就进了清理，而**引擎线程仍在后台注入**。
真机上那表现为：手机最终停在**压测途中**的某个点而不是回到真实 GPS，
而报告还写着「清理已确认」。

⇒ **清理必须在引擎线程真的退出之后做。**
正确顺序是「先等到点触发 `stop()`（它内部会等线程）、再兜一次 `_wait_task()`」。

⚠️ 顺带修掉这个 wart 本身：新增 :meth:`RunSession.wait_until_finished(timeout)`。
原来的 ``_wait_task`` 是**会话内部的快路径** —— 调用它之前（``stop`` /
``cleanup``）已经先置了停止标志，引擎一两个采样周期内就会退出，所以 5 秒足够、
它**故意不等**。但外部要等「一次 18 分钟的运行自然跑完」时 5 秒显然不够。
新方法只做这一件事，不改 ``_wait_task`` 的语义（改了会波及 stop / cleanup）。

**其三：失败信号要从 ``error`` 事件接，不能靠 `try/except`，也不能事后查 `phase`。**

- ``try/except`` 没用：`_run_engine_blocking` 把引擎异常**吞掉**并转成 ``error`` 事件
  （这是有意的 —— 不让工作线程静默死掉，否则 GUI 会一直停在「运行中」），
  异常根本不会冒到调用方。
- 事后查 ``phase`` 也没用：它确实被置成了 ``FAILED``，
  但**紧接着的 `stop()` 又会改回 `STOPPED``** —— 查的时候已经看不到了。

⇒ 只有 ``emit`` 回调里的 ``error`` 事件是可靠的失败信号。

### 处置

- `device.py` 新增 `InjectionStats`：记录**每一次尝试**的耗时（含失败与超时）。
  只记成功的话，看到的分布会很漂亮，而真出问题的那次被抹掉了。
  **刻意不塞进 `DeviceStatus`** —— 那是每秒推给 GUI 的快照，
  塞进增长型列表等于每秒重传一遍历史。
- 新增 `vrun soak --minutes N`：无头运行（不走 GUI、不需要点按钮），
  产出「丢失点数 + 延迟分位数 + 覆盖时长 + 清理确认」四件套。
- 判定红线四条，缺一不可：**丢失点数 = 0、跑满计划时长、无报错、清理成功**。

### 为什么给压测工具写这么多测试

一份把「丢了 40 个点」显示成「✅ 通过」的报告，**比没有报告更坏** ——
它会让人以为验证过了。所以 ``report_lines`` 的每个判定分支都有用例，
而且做了 5 组变异验证（去掉任一条红线、去掉 panic-clear 提示、
不再接 error 事件、阈值形同虚设），确认测试真的在守。

⚠️ 变异⑤第一轮**没被抓到**：那时只有两条相关用例，
而其中一条的 ``error`` 非空会把阈值判断短路掉。
补了一条「**无错误、只是提前到终点**」的用例才抓住 ——
提醒：给判定逻辑写测试时，要特意造出「其他条件都正常、只这一条不满足」的情形。

### 顺带一条

``Route`` 压测路线必须留 2 倍余量，否则引擎会先到终点、压测提前结束，
而报告里的秒数失去意义。判据是 ``covered_s``（最后一个采样点的时刻）而不是
「墙钟等了多久」—— 后者只会告诉你 sleep 睡够了。

### 实测结果（同日，iPhone 18,1 / iOS 27.0.1 / USB，10.0 km/h @ 1 Hz）

| 项 | 5 分钟 | 10 分钟（正式） |
| --- | --- | --- |
| 注入成功 | 300 点 | **599 点** |
| **丢失点数** | **0** | **0** |
| 延迟 p50 / p90 | 4.9 / 6.3 ms | **5 / 7 ms** |
| 延迟 p99 / max | 26.5 / 75.6 ms | **21 / 51 ms** |
| 超时次数 | 0 | **0** |
| 清理 | 已确认 | **已确认** |

⇒ 合计 **899 次注入、0 丢失、0 超时**。最坏延迟 51 ms，
相对 1 Hz 的 1000 ms 预算有**三个数量级**余量。

这条结论值得单列，因为它**关掉了一个此前的未知风险**：
M2 的 6 点短测只证明了「能注入」，而 M4 的 3 km 全程是 **18 分钟**，
「长时间会不会漂、会不会漏」一直没有数据。现在有了，而且是干净的。

⚠️ 但它**只覆盖注入稳定性**。轨迹数学的正确性是 M1 用假时钟验的（平均速度
误差 0.74%、ETA 差 1.15 s），与本轮无关；校园 App 侧的计数与判定判定
仍需 M4 剩下的那几项目视确认 —— **本报告不能替代它们**。

---

## 汇总

| 编号 | 类型 | 影响 | 处置 |
| --- | --- | --- | --- |
| F1 | 文档精度纪律 | 安全带与 v_lo/v_hi 偏 0.04–0.14 | 实现保留精确 σ，容差覆盖 |
| F2 | **文档内部矛盾** | 判定色的黄/红边界 | 规则优先，分歧显式登记 |
| F3 | 文档数值异常 | 单行两个时间列 | 标注 + 跳过对照 + 单独锁定 |
| F4 | 建模固有性质 | v_actual 有 ~0.2 几何抖动 | 测试改用相对断言 |
| F5 | 统计估计量偏差 | σ 被低估最多 15% | 多种子 + 解析修正 |
| F6 | 实现遗漏 | η(t) 恒为 0 | 补实现，独立随机流 |
| F7 | **路径错误** | 侧车启动会失败 | ✅ 已修：`resolve_python.sh` 解析 prefix + 回归测试 |
| F8 | **文档理由不完整** | 「必须常驻进程」的真实理由被低估 | 决策不变，理由换成结构性的：CLI 无循环形态 |
| F9 | **架构取舍** | M2 的必要性被高估 | `play` 回放可覆盖完整会话；`device.py` 只为「中途改参数」 |
| F10 | 取整边界 | 闭式解 0.76 s 残差翻转显示秒数 | 秒级容差断言 + 回归锁定；GUI 行为不变 |
| F11 | ★ **静默失效** | 会话从不注入；停止要等跑完；保险泄漏 | 三个都靠单测抓（真机 18 分钟也未必看得出）；见 F11 |
| F12 | ★ **测试偷连真机** | 套件「随机」挂死（手机插着才复现） | fixture 层隔离硬件；见 F12 |
| F13 | ★ **误判崩溃真凶** | 点一下即 abort；26 条纯逻辑测试全绿也拦不住 | 改 `canShowCallout`；新增真喂 MapKit 的集成测试；见 F13 |
| F14 | ★ **跨语言协议静默断开** | 1 Hz 的 tick/progress 一直在发，GUI 一直收不到，两侧零报错 | 新增双方共用的真实载荷基准 `tests/golden/session_events.json`；见 F14 |
| F15 | ★ **压测工具本身会骗人** | 引擎仍在后台注入时就清理，手机停在途中而报告写「已确认」 | 修正等待顺序；失败信号改从 `error` 事件接；判定四红线 + 5 组变异验证；见 F15 |
