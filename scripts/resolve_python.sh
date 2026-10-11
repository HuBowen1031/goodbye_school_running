#!/bin/zsh
# ══════════════════════════════════════════════════════════════════
#  resolve_python.sh —— 解析 vrun 环境的 Python 解释器【绝对路径】
#
#  ── 为什么不能硬编码 ──────────────────────────────────────────
#   ① conda 不在非交互 shell 的 PATH 上（works.md §1.3 实测），
#      所以脚本里不能只写 `python`；
#   ② 环境的实际位置由 conda 的 envs_dirs 决定 —— 本机 vrun 装在
#      ~/.conda/envs/vrun，**不是** miniconda3/envs/vrun
#      （findings.md F7 实测：写死会直接让侧车启动失败）。
#
#  ── 解析顺序（先命中者胜）────────────────────────────────────
#   1. $VRUN_PYTHON                     显式覆盖，跳过全部探测
#   2. conda config --show envs_dirs    → <dir>/<env>/bin/python
#   3. conda env list --json            → <prefix>/bin/python
#   4. ~/miniconda3/envs/<env>/bin/...  约定位置兜底
#
#   每个候选都要过三关：存在、可执行、是 Python 3（防止指到半残环境）。
#
#  ── 环境变量 ─────────────────────────────────────────────────
#   VRUN_ENV_NAME         环境名，默认 vrun
#   VRUN_PYTHON           直接指定解释器，默认未设置
#   VRUN_RESOLVE_VERBOSE  非空时把命中的规则打到 stderr（排查用）
#
#  ── 用法 ─────────────────────────────────────────────────────
#   PY="$(scripts/resolve_python.sh)" || exit 1
#   PYTHONPATH=src "$PY" -m vrun estimate --speed 10 --distance 3000
#
#   成功 → stdout 恰好一行绝对路径，退出码 0
#   失败 → stderr 说明原因与补救办法，退出码 1
#
#   ⚠️ 这是 Swift 侧车、build_app.sh、preflight.sh 共用的**唯一**入口。
#      任何地方都不许再出现解释器的硬编码路径。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

ENV_NAME="${VRUN_ENV_NAME:-vrun}"

note() {
  if [[ -n "${VRUN_RESOLVE_VERBOSE:-}" ]]; then
    printf 'resolve_python: %s\n' "$1" >&2
  fi
}

die() {
  printf 'resolve_python: %s\n' "$1" >&2
  exit 1
}

# 候选必须同时满足：非空、存在、可执行、能被当作 Python 3 跑起来。
# 最后一条是为了挡住「目录还在但环境已损坏」和「指到了非 Python 的
# 可执行文件」两种情况 —— 否则失败会推迟到侧车启动时才暴露。
is_usable_python() {
  local p="$1"
  [[ -n "$p" && -x "$p" ]] || return 1
  "$p" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1
}

# accept <路径> <规则名> —— 可用则打印并退出，否则返回 1 让调用方继续。
accept() {
  if is_usable_python "$1"; then
    note "命中规则：$2  →  $1"
    printf '%s\n' "$1"
    exit 0
  fi
  note "跳过（不是可用的 Python 3）：$1"
  return 1
}

# ── 规则 1：显式覆盖 ──────────────────────────────────────────────
if [[ -n "${VRUN_PYTHON:-}" ]]; then
  accept "$VRUN_PYTHON" 'VRUN_PYTHON' \
    || die "VRUN_PYTHON=${VRUN_PYTHON} 不可用（不存在 / 不可执行 / 不是 Python 3）"
fi

# ── 定位 conda 本体（它同样不在 PATH 上）────────────────────────
find_conda() {
  local c
  for c in "${CONDA_EXE:-}" \
           "$HOME/miniconda3/bin/conda" \
           "$HOME/anaconda3/bin/conda" \
           "$HOME/miniforge3/bin/conda" \
           "$HOME/miniconda/bin/conda" \
           /opt/homebrew/Caskroom/miniconda/base/bin/conda \
           /usr/local/Caskroom/miniconda/base/bin/conda \
           /opt/miniconda3/bin/conda; do
    if [[ -n "$c" && -x "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

CONDA_BIN=""
if CONDA_BIN="$(find_conda)"; then
  note "conda 本体：${CONDA_BIN}"
else
  note '未找到 conda 本体，跳过规则 2 / 3'
fi

# ── 规则 2：envs_dirs 逐个探测（最快，且不必解析 JSON）──────────
if [[ -n "$CONDA_BIN" ]]; then
  dirs="$("$CONDA_BIN" config --show envs_dirs 2>/dev/null \
          | sed -n 's/^[[:space:]]*-[[:space:]]*//p' || true)"
  if [[ -n "$dirs" ]]; then
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      d="${d/#\~/$HOME}"                       # conda 有可能回显 ~ 前缀
      accept "${d%/}/${ENV_NAME}/bin/python" "envs_dirs: ${d}" || true
    done <<< "$dirs"
  fi
fi

# ── 规则 3：conda env list --json（规则 2 失效时的通用解）───────
if [[ -n "$CONDA_BIN" ]]; then
  json="$("$CONDA_BIN" env list --json 2>/dev/null || true)"
  if [[ -n "$json" ]]; then
    prefix="$(printf '%s\n' "$json" \
              | grep -oE "\"[^\"]*/${ENV_NAME}\"" \
              | head -n 1 | tr -d '"' || true)"
    if [[ -n "$prefix" ]]; then
      accept "${prefix}/bin/python" 'conda env list --json' || true
    fi
  fi
fi

# ── 规则 4：约定位置兜底（conda 完全不可用时的最后一条路）───────
accept "$HOME/miniconda3/envs/${ENV_NAME}/bin/python" '约定位置兜底' || true

# ── 全部未命中 ───────────────────────────────────────────────────
die "找不到环境 '${ENV_NAME}' 的 Python 解释器。
  已尝试：\$VRUN_PYTHON → envs_dirs 探测 → conda env list --json → ~/miniconda3/envs
  排查：
    conda env list                    # 环境是否存在、prefix 在哪
    conda env list --json             # 看 vrun 的完整路径
  重建环境：
    scripts/bootstrap_env.sh
  临时指定（不写进仓库）：
    VRUN_PYTHON=/path/to/python scripts/resolve_python.sh"
