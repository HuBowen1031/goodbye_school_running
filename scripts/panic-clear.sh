#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  panic-clear.sh —— 强制清除虚拟定位
#
#  什么时候用（works.md §2.3）
#    GUI 崩溃 / 被强杀 / 终端被关掉，三保险来不及跑干净时，
#    博文的 iPhone 定位会**一直卡在校园那个虚拟点上** ——
#    导航、天气、「查找」全部跟着偏，要到手机重连才恢复。
#    这条脚本是那条路上唯一能走的东西。
#
#  用法
#    scripts/panic-clear.sh
#
#  ⚠️ 它也是 safety.py **自动**走的最后一道保险
#    （主 event loop 已死、原 DTX 通道随之失效时的子进程路径）。
#    所以它不能只当「手动脚本」看待 —— 它失效等于保险失效。
# ══════════════════════════════════════════════════════════════

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── 解释器：唯一入口是 resolve_python.sh，绝不硬编码 ────────
# ⚠️ 这一段是 safety.py 子进程路径的**下游**。如果这里失败，
#    上游那条「最后一道保险」就断了 —— 所以错误必须显式暴露。
if [[ ! -x scripts/resolve_python.sh ]]; then
    echo "✗ scripts/resolve_python.sh 不可执行" >&2
    exit 1
fi
PY="$(scripts/resolve_python.sh)"
if [[ -z "${PY}" || ! -x "${PY}" ]]; then
    echo "✗ 解析不到可用的解释器（resolve_python.sh 输出：'${PY}'）" >&2
    exit 1
fi

echo "▸ 解释器：${PY}"
echo "▸ 正在清除虚拟定位…"

# `vrun panic-clear` 自己会处理幂等、隧道建立与失败重试。
# 不加 PYTHONPATH 的话，源码树下会直接 `No module named vrun` ——
# 而这恰恰是「崩溃之后才跑」的场景（safety.py 里已踩过一次）。
PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PY}" -m vrun panic-clear
RC=$?

if [[ ${RC} -eq 0 ]]; then
    echo "✓ 已清除。iPhone 定位已回到真实 GPS。"
else
    echo "✗ 清除失败（退出码 ${RC}）。" >&2
    echo "  排查顺序：" >&2
    echo "    1. USB 还插着吗 / 已信任此电脑吗" >&2
    echo "    2. iPhone 屏幕是否已解锁（锁屏时隧道可能建不起来）" >&2
    echo "    3. 重连一次手机即可清掉 —— 断开 USB 也会恢复真实定位" >&2
fi
exit ${RC}