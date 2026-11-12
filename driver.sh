#!/usr/bin/env bash
# tree-05 点火者（薄壳）。职责**只有四件**：建 run 目录、设环境变量、起进程、把每轮的推进读自 tree 状态。
# 它**不含任何动作决策** —— 挑哪个节点、推哪一步，一律由 harness/leafrun.py 的统一推进队列从 tree 状态推。
#
# 用法：
#   driver.sh --input <清单文件> [--框架 <大框架文件>] --run-id <名字> [--dry]
#
#   --dry：只做「建 run 目录 + 打印将要执行的命令行/将读的输入路径」就 exit 0
#          （一个 pi 进程都不起、不写心跳、不推进任何节点）。
#
# 真跑建议由调用方后台起（本脚本前台运行，正常退出）：
#   nohup bash driver.sh --input … --run-id … >> <run>/logs/driver.out 2>&1 &
set -u

PY="${TREE_PY:-/usr/bin/python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"

IN=""; FRAME=""; RUN_ID=""; DRY="no"; RESUME="no"; RUNNER="${TREE_RUNNER:-pi}"; RUNNER_EXPLICIT="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --input)   IN="${2:-}"; shift 2 ;;
    --框架)    FRAME="${2:-}"; shift 2 ;;
    --run-id)  RUN_ID="${2:-}"; shift 2 ;;
    --runner)  RUNNER="${2:-}"; RUNNER_EXPLICIT="yes"; shift 2 ;;
    --resume)  RESUME="yes"; shift ;;
    --dry)     DRY="yes"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[ -n "$RUN_ID" ] || { echo "缺 --run-id（它决定 run 目录名）" >&2; exit 2; }

export TREE_ROOT="${TREE_ROOT:-$HERE}"
export TREE_RUNS_DIR="${TREE_RUNS_DIR:-$TREE_ROOT/runs}"
RUN_DIR="$TREE_RUNS_DIR/$RUN_ID"
RUN_META="$RUN_DIR/run.json"
IN_RUN=""; FRAME_RUN=""

if [ "$RESUME" = "yes" ]; then
  [ -f "$RUN_DIR/state.json" ] || { echo "无法 resume：缺少 $RUN_DIR/state.json" >&2; exit 2; }
  [ -f "$RUN_META" ] || { [ -n "$IN" ] || { echo "缺 $RUN_META；旧 run 请显式给 --input" >&2; exit 2; } }
  if [ -f "$RUN_META" ]; then
    SAVED_RUNNER=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("runner", ""))' "$RUN_META") || exit 2
    SAVED_INPUT=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("input", ""))' "$RUN_META") || exit 2
    [ -n "$SAVED_RUNNER" ] && [ -n "$SAVED_INPUT" ] || { echo "无法 resume：$RUN_META 缺 runner/input" >&2; exit 2; }
    if [ "$RUNNER_EXPLICIT" = "yes" ] && [ "$RUNNER" != "$SAVED_RUNNER" ]; then
      echo "无法 resume：runner 与原 run 不一致（$SAVED_RUNNER）" >&2; exit 2
    fi
    RUNNER="$SAVED_RUNNER"
    if [ -n "$IN" ] && [ "$(basename "$IN")" != "$SAVED_INPUT" ]; then
      echo "无法 resume：--input 与原 run 保存的输入名不一致（$SAVED_INPUT）" >&2; exit 2
    fi
    IN_RUN="$RUN_DIR/input/$SAVED_INPUT"
  else
    IN_RUN="$RUN_DIR/input/$(basename "$IN")"
  fi
  [ -f "$IN_RUN" ] || { echo "无法 resume：run 内输入文件不存在：$IN_RUN" >&2; exit 2; }
else
  [ ! -f "$RUN_DIR/state.json" ] || { echo "run 已有 state.json；请用 --resume，拒绝重建树" >&2; exit 2; }
  [ -n "$IN" ] || { echo "缺 --input（输入清单文件）" >&2; exit 2; }
  [ -f "$IN" ] || { echo "--input 指的文件不存在：$IN" >&2; exit 2; }
  [ -z "$FRAME" ] || [ -f "$FRAME" ] || { echo "--框架 指的文件不存在：$FRAME" >&2; exit 2; }

  mkdir -p "$RUN_DIR/logs" "$RUN_DIR/input"
  INPUT_NAME="$(basename "$IN")"
  if [ -e "$RUN_DIR/input/$INPUT_NAME" ]; then
    cmp -s "$IN" "$RUN_DIR/input/$INPUT_NAME" || {
      echo "run 内已有不同内容的输入文件；拒绝覆盖：$INPUT_NAME" >&2; exit 2;
    }
  else
    cp -p "$IN" "$RUN_DIR/input/"
  fi
  IN_RUN="$RUN_DIR/input/$INPUT_NAME"
  if [ -n "$FRAME" ]; then
    FRAME_NAME="$(basename "$FRAME")"
    if [ -e "$RUN_DIR/input/$FRAME_NAME" ]; then
      cmp -s "$FRAME" "$RUN_DIR/input/$FRAME_NAME" || {
        echo "run 内已有不同内容的框架文件；拒绝覆盖：$FRAME_NAME" >&2; exit 2;
      }
    else
      cp -p "$FRAME" "$RUN_DIR/input/"
    fi
    FRAME_RUN="$RUN_DIR/input/$FRAME_NAME"
  fi
  "$PY" -c 'import json,sys; json.dump({"runner": sys.argv[2], "input": sys.argv[3]}, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)' "$RUN_META" "$RUNNER" "$INPUT_NAME"
fi

# ★ pi 配置面必须落在 run 目录内（绝不落 ~/.pi/agent）
export TREE_PI_AGENT_DIR="$RUN_DIR/pi-agent"
export TREE_PI_TIMEOUT="${TREE_PI_TIMEOUT:-900}"
export TREE_MAX_STEPS="${TREE_MAX_STEPS:-40}"
export TREE_PI_AGENT_SESSION_DIR="$TREE_PI_AGENT_DIR/sessions"
# ★ 绝不设 TREE_ACT_CMD（那是桩开关；设了就不可能真跑）
unset TREE_ACT_CMD
# ★ 绝不设 TREE_STATE_DIR（会让心跳落到 run 目录之外）
unset TREE_STATE_DIR
# 非交互 ssh 的 PATH 不含 $HOME/node/bin
PATH="$HOME/node/bin:$PATH"; export PATH

ARGS=(--run-id "$RUN_ID" --run-dir "$RUN_DIR" --input "$IN_RUN" --runner "$RUNNER")
[ "$RESUME" = "yes" ] && ARGS+=(--resume)
[ -n "$FRAME_RUN" ] && ARGS+=(--框架 "$FRAME_RUN")

echo "== 点火（薄壳）=="
echo "TREE_ROOT=${TREE_ROOT}"
echo "TREE_RUNS_DIR=${TREE_RUNS_DIR}"
echo "RUN_DIR=${RUN_DIR}"
echo "TREE_PI_AGENT_DIR=${TREE_PI_AGENT_DIR}"
echo "TREE_PI_TIMEOUT=${TREE_PI_TIMEOUT}"
echo "TREE_MAX_STEPS=${TREE_MAX_STEPS}"
echo "TREE_ACT_CMD=${TREE_ACT_CMD:-<未设>}"
echo "TREE_STATE_DIR=${TREE_STATE_DIR:-<未设>}"
echo "将读的输入路径：$IN_RUN"
echo "将读的大框架路径：${FRAME_RUN:-<未给>}"
echo "将执行的命令行：$PY -m harness.leafrun ${ARGS[*]}"

if [ "$DRY" = "yes" ]; then
  echo "== --dry：到此为止（不起任何 pi 进程、不写心跳、不推进任何节点）=="
  exit 0
fi

cd "$TREE_ROOT" || exit 1
exec "$PY" -m harness.leafrun "${ARGS[@]}"
