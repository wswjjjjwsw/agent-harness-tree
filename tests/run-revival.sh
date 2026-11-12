#!/usr/bin/env bash
# tree-03 唯一验收入口②（真 pi）：会话复活 + 保真 + 无历史对照 + 恢复对账 + 串行自证。
#   用法：bash ~/agent-harness/tests/run-revival.sh      （cwd 无关、自包含）
#   全绿 ⇒ rc=0 且最后一行打印 `GATES: <pass>/<total> PASS`
#   ★ 真 pi 那几条是**唯一**允许打模型的：串行、单次超时 300s、总调用数 ≤ 12（脚本内自锁）。
#   ★ 配置面只在树内 runs/pi-agent/，绝不落 ~/.pi/agent（生产 llaat 目录）。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
PY="${TREE_PY:-/usr/bin/python3}"

mkdir -p "${HERE}/tmp/tree03/revival/runs"

export TREE_ROOT="${ROOT}"
export TREE_RUNS_DIR="${HERE}/tmp/tree03/revival/runs"
export TREE_PI_AGENT_DIR="${HERE}/tmp/tree03/revival/pi-agent"
export TREE_PI_AGENT_SESSION_DIR="${TREE_PI_AGENT_DIR}/sessions"
export TREE_PI_TIMEOUT=300
export PATH="${HOME}/node/bin:${PATH}"
unset TREE_ACT_CMD                       # 本入口不打桩（真跑那几条必须走真 pi）

exec "${PY}" "${HERE}/cases-revival.py" "$@"
