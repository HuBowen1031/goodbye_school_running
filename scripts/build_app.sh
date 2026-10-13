#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  build_app.sh —— 把 SwiftPM 构建产物组装成可双击的 VRun.app
#
#  为什么需要这个脚本
#    `swift build` 产出的是**裸可执行文件**。没有 app 包就没有
#    Info.plist、没有 bundle identifier，macOS 会因此：
#      · 不给它 Dock 图标
#      · 不给它正常的 activation policy（窗口无法正常置前，
#        AXRaise / set frontmost 全部静默失败）
#      · 不给它菜单栏身份
#    这些症状很容易被误判成「GUI 代码写坏了」，其实是**壳还没装**。
#
#  用法
#    scripts/build_app.sh                # 构建 + 组装 + 打开
#    scripts/build_app.sh --no-open      # 只构建，不打开
#    scripts/build_app.sh --debug        # debug 配置（默认 release）
#    scripts/build_app.sh --out ~/Desktop   # 指定输出位置
#
#  产物
#    $OUT/VRun.app
#      Contents/Info.plist
#      Contents/MacOS/VRunApp          ← SwiftPM 的可执行文件
#      Contents/Resources/repo -> 仓库根（符号链接）
#
#  ⚠️ **图标还是 macOS 的通用占位图**（Dock 里那个灰色方块）。
#     原因：Info.plist 没有 `CFBundleIconFile`，仓库里也没有 .icns。
#     想要真图标：把 `AppIcon.icns` 放到 `app/VRun/bundle/`，
#     然后在本脚本生成 Info.plist 的地方加上
#     `<key>CFBundleIconFile</key><string>AppIcon.icns</string>`，
#     并把 .icns 拷进 `Contents/Resources/`。属于 M5 的收尾项，不影响功能。
# ══════════════════════════════════════════════════════════════

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── 可调参数 ─────────────────────────────────────────────────
OPEN_APP=1
CONFIG="release"
# ⚠️ 默认输出到 /tmp：本项目位于 FileProvider 同步目录（~/Documents），
#    iCloud 会往产物里注入 com.apple.FinderInfo，导致 codesign 报
#    「resource fork, Finder information, or similar detritus not allowed」。
#    治标 xattr -cr 会被再次写回，正解是**别在同步目录里组装**。
OUT="/tmp/vrun-dist"
BUNDLE_ID="com.vrun.app"
VERSION="0.3.0"
BUILD_N="3"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-open) OPEN_APP=0; shift ;;
        --debug)   CONFIG="debug"; shift ;;
        --out)     OUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

# ── 环境 ────────────────────────────────────────────────────
# ⚠️ 解释器路径**绝不硬编码**（findings.md F7）：侧车自己会用
#    resolve_python.sh 解析，这里只需保证它可执行。
[[ -x "$REPO_ROOT/scripts/resolve_python.sh" ]] || {
    echo "✗ scripts/resolve_python.sh 不可执行" >&2; exit 1; }

# Xcode 不在默认位置时用 DEVELOPER_DIR，不碰 sudo xcode-select（works.md §6 M0）
if [[ -d /Applications/Xcode.app/Contents/Developer ]]; then
    export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
fi

SCRATCH="/tmp/vrun-swift"          # 同理：构建目录也要离开 ~/Documents
APP="$OUT/VRun.app"

# ── 1. 构建 ─────────────────────────────────────────────────
echo "▸ 构建（${CONFIG}）…"
swift build -c "$CONFIG" --scratch-path "$SCRATCH" \
    --package-path "$REPO_ROOT/app/VRun"

BIN="$(swift build -c "$CONFIG" --scratch-path "$SCRATCH" \
        --package-path "$REPO_ROOT/app/VRun" --show-bin-path)/VRunApp"
[[ -x "$BIN" ]] || { echo "✗ 找不到可执行文件：$BIN" >&2; exit 1; }

# ── 2. 组装 ─────────────────────────────────────────────────
echo "▸ 组装 $APP …"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$BIN" "$APP/Contents/MacOS/VRunApp"

# 仓库符号链接：MainView.locateRepoRoot() 的第 ① 条候选路径。
# 有了它，打包后的 app 不需要 VRUN_REPO_ROOT 环境变量。
ln -s "$REPO_ROOT" "$APP/Contents/Resources/repo"

# ── 3. Info.plist ───────────────────────────────────────────
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                 <string>VRun</string>
    <key>CFBundleDisplayName</key>          <string>VRun</string>
    <key>CFBundleExecutable</key>           <string>VRunApp</string>
    <key>CFBundleIdentifier</key>           <string>${BUNDLE_ID}</string>
    <key>CFBundlePackageType</key>          <string>APPL</string>
    <key>CFBundleShortVersionString</key>   <string>${VERSION}</string>
    <key>CFBundleVersion</key>              <string>${BUILD_N}</string>
    <key>LSMinimumSystemVersion</key>       <string>26.0</string>
    <key>NSHighResolutionCapable</key>      <true/>
    <key>NSHumanReadableCopyright</key>    <string>仅用于自有设备的定位测试</string>
    <!-- 不设 LSUIElement：有 Dock 图标、能正常置前，是我们要的行为 -->
</dict>
</plist>
PLIST

# ── 4. 签名 ─────────────────────────────────────────────────
# 免 sudo 的 ad-hoc 签名即可（works.md §6 M0：不需要开发者证书）
echo "▸ ad-hoc 签名…"
codesign --force --deep --sign - "$APP" 2>&1 | sed 's/^/  /'

# ── 5. 打开 ─────────────────────────────────────────────────
echo "✓ 完成：$APP"
if [[ $OPEN_APP -eq 1 ]]; then
    echo "▸ 启动…"
    open "$APP"
fi