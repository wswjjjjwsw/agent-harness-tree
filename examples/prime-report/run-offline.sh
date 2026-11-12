#!/usr/bin/env bash
# 离线一键：确定性桩跑完整棵树（不需要模型、不需要网络）。
# 末行必须是：DEMO PASS: blocks/prime-report.md 186 -> 168, protected files unchanged
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
RUN="$(mktemp -d)/prime-run"

cd "${ROOT}"
PYTHONPATH="${ROOT}" "${TREE_PY:-python3}" -m harness.cli demo prime-report --run-dir "${RUN}"
