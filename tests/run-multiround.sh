#!/usr/bin/env bash
# tree-03 唯一验收入口①（桩驱动）：多轮回路 + 状态机 + 三道硬闸 + 本批次口径。
#   用法：bash ~/agent-harness/tests/run-multiround.sh      （cwd 无关、自包含、**不打模型**）
#   全绿 ⇒ rc=0 且最后一行打印 `GATES: <pass>/<total> PASS`
#   任一用例 FAIL ⇒ rc≠0；total < 19 视为不合格（下限写在 cases 里）
#   ★ 本脚本把 $TREE_ACT_CMD 指到 tests/stub-act.py ⇒ 一个 pi 进程都不许起。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
PY="${TREE_PY:-/usr/bin/python3}"

mkdir -p "${HERE}/tmp/tree03/runs"

export TREE_ROOT="${ROOT}"
export TREE_RUNS_DIR="${HERE}/tmp/tree03/runs"
export TREE_ACT_CMD="${HERE}/stub-act.py"
export PATH="${HOME}/node/bin:${PATH}"

exec "${PY}" "${HERE}/cases-multiround.py"
