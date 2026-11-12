#!/usr/bin/env bash
# tree-02 唯一验收入口：一条命令跑全（自包含、可重复跑、不联网、不打 8903/8091）。
#   用法：bash ~/agent-harness/tests/run-gates.sh       （cwd 无关）
#   全绿 ⇒ rc=0 且最后一行打印 `GATES: <pass>/<total> PASS`
#   任一用例 FAIL ⇒ rc≠0
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
PY="${TREE_PY:-/usr/bin/python3}"

mkdir -p "${HERE}/tmp"

# TREE_ROOT 指到树根，lib 里的缺省路径（role.yaml / prompts / runs / doc）都随它走
TREE_ROOT="${ROOT}" TREE_GATES_TRACE="${TREE_GATES_TRACE:-}" exec "${PY}" "${HERE}/cases.py"
