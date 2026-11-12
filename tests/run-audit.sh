#!/usr/bin/env bash
# tree-04 唯一验收入口：审计枝 + 全覆盖分批 + 终审触发 + 形状审查 D10（桩驱动）。
#   用法：bash ~/agent-harness/tests/run-audit.sh      （cwd 无关、自包含、**不打模型**）
#   全绿 ⇒ rc=0 且最后一行打印 `GATES: <pass>/<total> PASS`
#   任一用例 FAIL ⇒ rc≠0；total < 18 视为不合格（下限写在 cases 里）
#   ★ 本脚本把 $TREE_ACT_CMD 指到 tests/stub-audit.py ⇒ 一个 pi 进程都不许起（用例里采样自证）。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
PY="${TREE_PY:-/usr/bin/python3}"

mkdir -p "${HERE}/tmp/tree04/runs"

export TREE_ROOT="${ROOT}"
export TREE_RUNS_DIR="${HERE}/tmp/tree04/runs"
export TREE_ACT_CMD="${HERE}/stub-audit.py"
export PATH="${HOME}/node/bin:${PATH}"

exec "${PY}" "${HERE}/cases-audit.py"
