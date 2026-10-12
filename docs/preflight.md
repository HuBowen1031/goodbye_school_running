# preflight —— M0 设备前置检查实测记录

> **本文件是记录，不是计划。** 每一条都写明**实际执行的命令**与**真实输出**。
> 与 `works.md` 的区别：works.md 写「应该怎么做」，这里写「本机实际发生了什么」。
> 涉及个人隐私的字段（SIM 卡号、IMEI、序列号等）**一律不记录**。
>
> **脱敏说明**：Apple ID 邮箱与设备 UDID 已替换为占位符 —— 本仓库可能是公开的，
> 真实值不该进版本历史（Git 的历史一旦推送就很难彻底抹掉）。
> 团队 ID（`7GSYJXH7V8` / `Q69Z2T66R8`）**保留**：它们不是机密，
> 且 §4.2 的教训正是「团队 ID 不能从证书名里猜」，删掉反而讲不清。

---

## 0. 本轮环境快照（2026-10-12 20:02 起）

| 项 | 实测值 |
| --- | --- |
| 设备名 | 测试用 iPhone |
| 型号 | `iPhone18,1` |
| **iOS 版本** | **27.0.1**（Build `24A446`） |
| 连接方式 | USB（lockdown 已配对，`ActivationState: Activated`） |
| UDID | `<设备 UDID>` |
| 解释器 | `~/.conda/envs/vrun/bin/python`（由 `scripts/resolve_python.sh` 解析） |
| pymobiledevice3 | 11.27.2 |

> **iOS 27.0.1 ≥ 26.4 ⇒ DDI 以 cryptex 形式安装**，与 works.md §1.2 的预期一致。

---

## 1. 设备枚举 ✅

```bash
PY="$(scripts/resolve_python.sh)"
"$PY" -m pymobiledevice3 usbmux list
```

```json
[
    {
        "BuildVersion": "24A446",
        "ConnectionType": "USB",
        "DeviceClass": "iPhone",
        "DeviceName": "测试用 iPhone",
        "Identifier": "<设备 UDID>",
        "ProductType": "iPhone18,1",
        "ProductVersion": "27.0.1"
    }
]
```

> 注：`system_profiler SPUSBDataType` 在本轮**没有**列出这台设备。
> 结论：**以 `pymobiledevice3 usbmux list` 为准**，不要用 system_profiler 判断设备是否插好。

---

## 2. DDI 挂载 ✅（已存在，无需下载）

```bash
"$PY" -m pymobiledevice3 mounter auto-mount
```

```
WARNING Trying again over a no-root userspace tunnel since RSD is required for this command
        (override with PYMOBILEDEVICE3_DEFAULT_FALLBACK, or --tunnel/--userspace/--native)
INFO    DeveloperDiskImage already mounted; to replace it, remove it first with
        `cryptex uninstall com.apple.MobileAsset.DDI` ...
```

**结论**：

1. DDI **本来就在位**，不需要下载，**也不需要 Xcode** —— works.md §1.2 的判断成立。
2. 命令先走 RSD、失败后**自动回落到免 root 的 userspace 隧道**并成功 —— works.md §1.2「macOS 默认自动建立 in-process userspace 隧道，无需 root」成立。

```bash
"$PY" -m pymobiledevice3 cryptex list
```

```json
[ { "identifier": "com.apple.MobileAsset.DDI", "version": "27.1.266.1" } ]
```

> ⚠️ `~/.pymobiledevice3/` 目录在本轮**不存在**。
> 说明这台的 DDI 是随设备 / 系统侧已就位的，不是本机缓存出来的。
> **副作用**：`auto-mount` 在别的机器上会真的去下载，届时该目录才会出现。

---

## 3. ★ 生死验证 1：`simulate-location set` / `clear` ⚠️ 部分完成

### 3.1 命令签名（`--help` 实测）

```bash
"$PY" -m pymobiledevice3 developer dvt simulate-location --help
```

```
Commands:
  clear   Clear the currently simulated location on iOS 17 and newer.
  set     Set a simulated location through DVT on iOS 17 and newer.
  play    Replay a GPX route through DVT on iOS 17 and newer.

Usage: ... simulate-location set [OPTIONS] {latitude} {longitude}
Arguments:
  * latitude   <float>  [required]
  * longitude  <float>  [required]
```

### 3.2 注入

**第一次尝试（20:02）**：目标点 江安校区 `30.5535, 103.9910`
—— ❌ **测试设计失误**：江安校区正是用户当前所在地，地图上看不出任何变化，无法验证。

**第二次（20:11，✅ 通过）**：改用**一眼可辨**的坐标 —— **北京天安门广场 `39.9087, 116.3975`**。

```bash
# 后台常驻运行（F8：set 会永久阻塞，必须靠 async with 保持 DVT 连接存活）
"$PY" -m pymobiledevice3 developer dvt simulate-location set -- 39.9087 116.3975
```

```
>>> 注入天安门广场 39.9087, 116.3975 —— 保持运行
（无报错输出，持续运行中）
```

**用户目视确认**：iPhone 地图的定位点**确实跳到了天安门广场** ✅

> 💡 **给后续测试的一条方法论**：注入点必须选一个**用户当前不在的地方**，
> 且要"一眼可辨"。设成用户脚下的坐标 = 无法区分"注入生效"与"什么都没发生"。

### 3.3 清理

```bash
"$PY" -m pymobiledevice3 developer dvt simulate-location clear
```

**结果：正常返回，无输出。**（与 `set` 不同，`clear` **不**调用 `wait_return()`。）

清理链路实测：

| 步骤 | 结果 |
| --- | --- |
| ① 用户确认注入生效后执行 `clear` | ✅ 正常返回 |
| ② 停掉后台常驻的 `set` 进程 | ✅ 已取消 |
| ③ 检查残留进程 | ✅ **无残留** |
| ④ 兜底再跑一次 `clear`（幂等验证） | ✅ 正常返回，无报错 |
| ⑤ 用户确认定位已恢复真实位置 | ✅ 已确认 |

> ✅ **`clear` 的幂等性实测通过**：本轮累计执行 4 次（含 1 次 trap 自动触发、
> 1 次超时前的手动、2 次本轮），全部成功，**未对已清除的设备报错**。
> 这正是 manifest §4.2.2 要求 `SafetyGuard` 具备的性质。

### 3.4 结论：生死验证 1 ✅ 通过

| 检查项 | 状态 |
| --- | --- |
| 设备枚举 / 配对 | ✅ |
| DDI（cryptex）已挂载 | ✅ |
| 注入命令无报错 | ✅ |
| **iPhone 地图确实跳到指定点** | ✅ **用户目视确认** |
| **地图已恢复真实定位** | ✅ **用户目视确认** |
| **清理幂等** | ✅ **实测 4 次全部成功** |
| 无残留进程 | ✅ |

---

## 4. ★ 生死验证 2：`isSimulatedBySoftware` —— **探针已编译，待签名侧载**

这是 works.md §8 R-1 的 **Gate**，也是全项目唯一可能致命的未知。

### 4.1 ✅ 探针已写好并**编译通过**（2026-10-12 20:16）

`probe/ProbeApp/` —— 一个只读的观测 App，显示 `isSimulatedBySoftware` / 坐标 /
速度 / 时间戳，并在顶部用一枚 `CLEAN`(绿) / `SIMULATED`(红) 胶囊给出**一眼可判**的结论。

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
cd probe
xcodebuild -project ProbeApp.xcodeproj -scheme ProbeApp \
  -sdk iphoneos -configuration Debug -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO build
```

```
** BUILD SUCCEEDED **
```

产物校验：

```
CFBundleIdentifier => "com.vrun.probe"
MinimumOSVersion   => "17.0"
NSLocationWhenInUseUsageDescription => "探针需要读取 CLLocation 的关键字段（works.md M0 生死验证 2）"
```

> **写探针时踩到的 SDK 事实**：`CLLocationSourceInformation` 在 iOS 27 SDK 上
> **只有 `isSimulatedBySoftware` 一个成员**，没有 `isSimulated` / `sourceInformationType`。
> 初版按旧印象写了后两个，编译直接报错。**Gate 只依赖那一个字段**，不影响结论。

### 4.2 ✅ 签名与安装完成 —— 只差「信任证书」

#### (a) 团队 ID **不能从证书名里猜**

第一次带签名构建用了证书括号里的团队 ID：

```
$ security find-identity -v -p codesigning
1) "Apple Development: <你的 Apple ID> (7GSYJXH7V8)"
```

```bash
DEVELOPMENT_TEAM=7GSYJXH7V8 xcodebuild ...    # ❌
```

```
error: No Account for Team "7GSYJXH7V8". Add a new account in Accounts settings ...
error: No profiles for 'com.vrun.probe' were found ...
```

Xcode 实际记录的是**另一个**团队：

```bash
$ defaults read com.apple.dt.Xcode | grep -A3 IDEProvisioningTeam
    IDEProvisioningTeamByIdentifier = {
                isFreeProvisioningTeam = 1;
                teamID = Q69Z2T66R8;
    }
```

改成 `DEVELOPER_TEAM=Q69Z2T66R8` 后**一次通过**，描述文件由 Xcode 自动创建：

```
Signing Identity:     "Apple Development: <你的 Apple ID> (7GSYJXH7V8)"
Provisioning Profile: "iOS Team Provisioning Profile: com.vrun.probe"
```

> 💡 **可用的自检命令**（下次不用猜）：
> ```bash
> defaults read com.apple.dt.Xcode | grep -A3 IDEProvisioningTeamByIdentifier
> ```

#### (b) ⚠️ 构建产物目录**不能放在 `~/Documents` 里**

签名步骤一度失败：

```
ProbeApp.app: resource fork, Finder information, or similar detritus not allowed
Command CodeSign failed with a nonzero exit code
```

根因**不是**代码或签名配置，而是文件属性：

```bash
$ xattr -l build/.../ProbeApp.app
com.apple.FinderInfo:
com.apple.fileprovider.fpfs#P:
com.apple.provenance:
```

`~/Documents` 是 **FileProvider 同步目录**（`com.apple.fileprovider.fpfs#P` 是标志）。
iCloud 会**不断重新注入** `com.apple.FinderInfo` —— `xattr -cr` 清完当下能过，稍后又被写回。
所以清属性是**治标不治本**。

**正解：把 derived data 放到同步目录之外**，一次通过：

```bash
xcodebuild ... -derivedDataPath /tmp/vrun-probe-dd     # ✅ BUILD SUCCEEDED
```

> ⚠️ **这条同样适用于 M5 的 `build_app.sh`** ——
> 否则每次打包都会在同一个地方失败，而且报错信息（"detritus"）与真实原因
> （同步目录注入属性）毫无关联，极难排查。

#### (c) 安装成功

```bash
xcrun devicectl device install app --device <设备 UDID> \
  /tmp/vrun-probe-dd/Build/Products/Debug-iphoneos/ProbeApp.app
```

```
App installed:
• bundleID: com.vrun.probe
• installationURL: file:///private/var/containers/Bundle/Application/F9964960-.../ProbeApp.app/
```

#### (d) ⛔ 启动被拒 —— 免费签名的**证书信任**尚未确认

```bash
xcrun devicectl device process launch --device <设备 UDID> com.vrun.probe
```

```
BSErrorCodeDescription = Security
The operation couldn't be completed. Unable to launch com.vrun.probe because it has
an invalid code signature, inadequate entitlements or **its profile has not been
explicitly trusted by the user**.
```

这是**免费 Apple ID 签名**的固有限制：证书装好了不等于系统信任它。

**需用户本人操作**（一次性）：

> **设置 → 通用 → VPN 与设备管理 → 开发者 App → `<你的 Apple ID>` → 信任**

若看不到「开发者 App」一栏，说明还没打开过探针 App —— 先点开它一次，
系统才会创建信任入口。

### 4.3 ★★★ GATE 判决（2026-10-12 20:24 – 20:26）

探针在**真实定位**与**注入定位**两种状态下的读数对比：

| 字段 | 基线（真实 GPS） | 注入后（DVT） | 判读 |
| --- | --- | --- | --- |
| **`isSimulatedBySoftware`** | **`false`** | **`true`** | 🔴 **被标记为软件模拟** |
| 顶部胶囊 | `CLEAN` | `SIMULATED` | 🔴 翻转 |
| latitude | 30.5553767 | 39.9087000 | ✅ 注入生效 |
| longitude | 103.9906565 | 116.3975000 | ✅ 注入生效 |
| **altitude** | **491.21 m** | **0.00 m** | ⚠️ **破绽信号**：成都实际海拔约 490 m，注入恒为 0 |
| **horizontalAccuracy** | **16.9 m** | **5.0 m** | ⚠️ **破绽信号**：恰好是整数值，且比真实 GPS「更准」 |
| speed | −1.000 m/s | −1.000 m/s | 静止，iOS 的无效值约定 |
| course | −1.0° | −1.0° | 同上 |
| timestamp | 20:24 | **20:26** | ✅ 新鲜读数，注入确在生效 |

#### 判决

works.md §6 M0 的 Gate 判据是**两个条件同时成立**才判死：

> 「若确认被标记为软件模拟，**且**校园 App 会检测该字段，
> 则整个方案的路线选择无效，需在此之前停下来重新讨论。」

| 条件 | 状态 |
| --- | --- |
| ① 注入被标记为「软件模拟」 | ✅ **已确认**（works.md §8 R-1 从「可能」变成「确实」） |
| ② 校园 App 会检测该字段 | ❌ **已排除**（见 §4.4） |

### 🟢 GATE 判决：**通过**（2026-10-12 20:35）

**条件②不成立 ⇒ 方案路线成立，M2 / M3 可以开工。**

---

### 4.4 条件②的排除过程（真机实测）

#### 第一次：定点注入

注入 `30.5555, 103.9907`（距真实位置约 15 m）。**App 接受并开始计数**，无任何告警。

#### 第二次：轨迹回放 —— 166 m @ 9–10 km/h

不只测「能不能启动」，而是**完整跑一段真实运动**：

```bash
# ① 用 M1 的引擎离线算出带时间戳的 GPX（以用户真实位置为中心的小环）
vrun gpx --route logs/near_me.json --speed 10 --noise off --out logs/run400.gpx
#    → 165.6 m / 66 个采样点 / 总时长 66.0 s / 平均 9.031 km/h

# ② 回放到真机
pymobiledevice3 developer dvt simulate-location play -- logs/run400.gpx
```

**结果**：

| 检查项 | 结果 |
| --- | --- |
| App 是否报警 | ✅ **全程无任何告警** |
| 距离是否正确 | ✅ **用户确认：距离正确** |
| 清理 | ✅ `clear` 已执行，无残留进程 |

**结论：校园 App 不检测 `isSimulatedBySoftware`，也不因模拟来源而改变计数行为。**

> 💡 **这一步比"定点不报警"强得多**：一段 166 m、9–10 km/h、66 秒的连续运动
> 如果被判为作弊，通常会在**速度**或**距离累计**上露馅。两项都正常，
> 说明 App 的判定逻辑确实只看「GPS 位置差 + 时间」，
> 与 works.md §8 R-2 里用户口述的一致。

---

### 4.5 ★ 副产品：`GPX 回放` 是一条被低估的注入路径

今天意外打通了一条**不写 `device.py` 也能注入轨迹**的路：

```python
# pymobiledevice3 源码：location_simulation_base.py
async def play_gpx_file(self, filename, disable_sleep=False, ...):
    for point in points:
        duration = (point.time - last_time).total_seconds()
        await asyncio.sleep(duration)        # ← 按 GPX 的时间戳差值
        await self.set(point.latitude, point.longitude)
```

一份**带时间戳的 GPX** 就是一条可直接回放的轨迹。
而 GPX 正是 M1 `recorder.py` 的产物 —— 于是：

```
M1 的纯计算引擎 ──► GPX ──► 真机注入
       （离线、秒级、可复现）      （无需重连、无需维护 async 隧道）
```

**优点**（实测）：零隧道代码、零连接管理、轨迹与注入**逐字节一致**、天然可暂停（杀进程+clear）。
**局限**：`play` 回放完同样调 `OSUTILS.wait_return()` 永久阻塞，**中途无法改速度**。

> 这直接影响 M2 的取舍 —— 见 `findings.md` **F9**。

#### 附带确认的三件事

1. **§2.1「时间不可伪造」再次坐实** —— 注入点的 `timestamp` 就是注入那一刻的系统时间（20:26），
   不携带任何外部时间戳。
2. **§1.2「`set()` 不携带速度/朝向/海拔/精度」得到补充证据** —— 实测 `altitude` 恒为 `0.00`、
   `horizontalAccuracy` 恒为 `5.0`，**DVT 接口根本无法指定它们**。
   这意味着工具**无法伪造一个"像真的"海拔与精度**，这本身是一个结构性限制。
3. **`isSimulatedBySoftware` 无已知规避手段**（works.md §8 R-1 已预判）。
   本轮实测未发现任何可关闭该标记的开关 —— DVT 协议层面就没有这个能力。

---

## 5. ★ Xcode 构建链路 —— 已验证，且**不需要 sudo**

works.md M0 原本要求：

```bash
sudo xcode-select -s /Applications/Xcode.app && sudo xcodebuild -runFirstLaunch
```

**实测发现不必如此。** 设置环境变量即可，且完全不碰系统状态：

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcodebuild -version                  # → Xcode 27.0 (27A266a)
xcrun --sdk iphoneos --show-sdk-path # → .../iPhoneOS27.0.sdk
```

| 项 | 实测 |
| --- | --- |
| CLT 里有 iOS SDK 吗 | ❌ **没有**。只有 `MacOSX*.sdk`（26 / 26.5 / 27 / 27.0） |
| 完整 Xcode 里有 iOS SDK 吗 | ✅ `iPhoneOS.sdk`、`iPhoneOS27.0.sdk` |
| `DEVELOPER_DIR` 能否免 sudo 指向完整 Xcode | ✅ **可以** |
| iOS SDK 版本 vs 设备 iOS | 27.0 vs 27.0.1 ✅ 完全匹配 |

> 💡 **这个发现修正了 works.md 的一个隐含假设**：
> works.md §1.3 / R-5 说「CLT 的 `MacOSX27.0.sdk` 已足够构建 SwiftUI + MapKit」。
> 这句话对 **M3 的 macOS GUI** 成立（确实够），
> 但对 **M0 探针（iOS App）** **不成立** —— CLT 里**根本没有 iOS SDK**。
> 若当初直接信了「CLT 够用」而跳过 Xcode 验证，Gate 会卡在一个
> 「不知道自己在等什么」的状态。

**给脚本的约定**：`build_app.sh`（M5）与探针构建都应显式设置
`DEVELOPER_DIR`，而不是要求用户改全局 `xcode-select`。

> ⚠️ **Gate 判据**：若该字段为 `true` **且** 校园 App 会检测它，
> 则整个方案的路线选择失效 —— **在此之前不要投入 M2 / M3**（works.md §6 M0 的 ⚠️ Gate）。

---

## 6. 本轮未涉及 / 未验证

| 项 | 说明 |
| --- | --- |
| Xcode 构建链路 | ✅ **已验证**（见 §5，`DEVELOPER_DIR` 免 sudo） |
| 探针侧载 | ⛔ 卡在 Xcode 未登录 Apple ID（见 §4.2，**需用户本人操作**） |
| `isSimulatedBySoftware` 实测 | ⏳ 待侧载后读取 —— **这是 Gate** |
| SwiftPM + SwiftUI + MapKit（M3 用） | 未验证。works.md R-5 的兜底路径**仍是纸面结论**，本轮未触及 |
| 1 Hz 长时间注入稳定性 / 延迟分布 | 未测（needs M2 的常驻进程） |
| 校园 App 的速度算法是否滤波 | 未知（works.md R-9） |

---

## 7. 给下一轮的五条硬结论

1. **DDI 与隧道都已就绪**，M0 的环境侧不再有阻碍 —— M2 可以直接开工（前提是 Gate 过了）。
2. **`set` 永久阻塞**（F8）⇒ 逐点注入**只能**用常驻进程的 Python API，
   `simulate-location set` CLI 不能用于循环注入。这比 works.md §4.1 给出的理由更强。
3. **`clear` 是幂等且立即返回的**，可作为安全红线的兜底动作；
   但**仍需人工确认一次**（§3.4），不能只信退出码。
4. **Xcode 不需要 sudo**：`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` 即可。
   但 **CLT 里没有 iOS SDK** —— 任何 iOS 产物都必须用完整 Xcode。
5. **钥匙串里有证书 ≠ Xcode 里登录了账号**。自动签名还需要后者（见 §4.2），
   这是本轮唯一需要用户亲自操作的环节。
