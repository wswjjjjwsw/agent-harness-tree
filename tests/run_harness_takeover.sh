#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <worktree> <ref> <q01|agg2> <run-id> [--stage|--live]" >&2
  exit 2
}
[[ $# -ge 4 && $# -le 5 ]] || usage
WORKTREE="$(cd "$1" && pwd)"
REF="$2"
CASE="$3"
RUN_ID="$4"
MODE="${5:---stage}"
[[ "$CASE" == q01 || "$CASE" == agg2 ]] || usage
[[ "$MODE" == --stage || "$MODE" == --live ]] || usage
[[ "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$ ]] || usage

EVIDENCE="${HARNESS_EVIDENCE_DIR:-$HOME/harness-evidence/harness-takeover-20261111}"
RUNS="$EVIDENCE/runs"
RUN_DIR="$RUNS/$RUN_ID"
INPUT="$EVIDENCE/input/$CASE.md"
[[ -f "$INPUT" ]] || { echo "missing frozen input: $INPUT" >&2; exit 2; }
HEAD="$(git -C "$WORKTREE" rev-parse HEAD)"
REF_HEAD="$(git -C "$WORKTREE" rev-parse --verify "${REF}^{commit}")"
[[ "$HEAD" == "$REF_HEAD" ]] || { echo "worktree HEAD $HEAD != ref $REF_HEAD" >&2; exit 2; }
[[ -z "$(git -C "$WORKTREE" status --porcelain)" ]] || { echo "worktree must be clean" >&2; exit 2; }

check_md5() {
  local path="$1" expected="$2" actual
  actual="$(md5 -q "$path")"
  [[ "$actual" == "$expected" ]] || { echo "MD5 mismatch: $path $actual != $expected" >&2; exit 2; }
}

if [[ "$MODE" == --stage ]]; then
  [[ ! -e "$RUN_DIR" ]] || { echo "run-id already exists: $RUN_DIR" >&2; exit 2; }
  export TREE_ROOT="$WORKTREE" TREE_RUNS_DIR="$RUNS" TREE_PI_TIMEOUT=300 TREE_MAX_STEPS=40 TREE_RUNNER=pi
  unset TREE_ACT_CMD TREE_STATE_DIR
  bash "$WORKTREE/driver.sh" --input "$INPUT" --run-id "$RUN_ID" --runner pi --dry
  if [[ "$CASE" == agg2 ]]; then
    mkdir -p "$RUN_DIR/input/agg-data"
    cp -p "$EVIDENCE"/input/agg-data/*.txt "$RUN_DIR/input/agg-data/"
    check_md5 "$RUN_DIR/input/agg-data/month-01.txt" e947df0778daa06f88babbf1e072e022
    check_md5 "$RUN_DIR/input/agg-data/month-02.txt" 9c1165f07fbc9e932b1e6efe9dc9cbba
    check_md5 "$RUN_DIR/input/agg-data/month-03.txt" a16997b02577f080949c950bc68c9d81
    check_md5 "$RUN_DIR/input/agg-data/month-04.txt" 38ca65a97f29aa684d6e1eb35a7062a6
    check_md5 "$RUN_DIR/input/agg-data/month-05.txt" 35e04e10acb37d2559be92a6e477c5ac
    check_md5 "$RUN_DIR/input/agg-data/month-06.txt" 40fbd0b1ae8a7a42e86a5484f50380d2
  fi
  echo "STAGED_RUN=$RUN_DIR"
  echo "git_head=$HEAD ref=$REF"
  echo "Live launch is a separate explicit call with --live."
  exit 0
fi

[[ -d "$RUN_DIR" && -f "$RUN_DIR/input/$CASE.md" ]] || { echo "run is not staged: $RUN_DIR" >&2; exit 2; }
[[ ! -e "$RUN_DIR/state.json" && ! -e "$RUN_DIR/rounds.jsonl" ]] || { echo "run already contains state; refusing to replay: $RUN_DIR" >&2; exit 2; }
if [[ "$CASE" == agg2 ]]; then
  check_md5 "$RUN_DIR/input/agg-data/month-01.txt" e947df0778daa06f88babbf1e072e022
  check_md5 "$RUN_DIR/input/agg-data/month-02.txt" 9c1165f07fbc9e932b1e6efe9dc9cbba
  check_md5 "$RUN_DIR/input/agg-data/month-03.txt" a16997b02577f080949c950bc68c9d81
  check_md5 "$RUN_DIR/input/agg-data/month-04.txt" 38ca65a97f29aa684d6e1eb35a7062a6
  check_md5 "$RUN_DIR/input/agg-data/month-05.txt" 35e04e10acb37d2559be92a6e477c5ac
  check_md5 "$RUN_DIR/input/agg-data/month-06.txt" 40fbd0b1ae8a7a42e86a5484f50380d2
fi
export TREE_ROOT="$WORKTREE" TREE_RUNS_DIR="$RUNS" TREE_PI_TIMEOUT=300 TREE_MAX_STEPS=40 TREE_RUNNER=pi
export TREE_LLAAT_ADAPTER_URL=http://127.0.0.1:18905/v1 TREE_LLAAT_URL=http://127.0.0.1:8903
unset TREE_ACT_CMD TREE_STATE_DIR
bash "$WORKTREE/driver.sh" --input "$INPUT" --run-id "$RUN_ID" --runner pi 2>&1 | tee -a "$RUN_DIR/logs/driver.out"
