# Prime report：权威冲突示例

这个示例对应 B 验收：报告写 186，权威 oracle 是 168，受保护产物 `product/answer.txt` 是 168。

```
examples/prime-report/
├── input/prime-oracle.json     # 权威、只读
├── authority.json              # 只读/可写边界
├── product/answer.txt          # 受保护，168
├── blocks/prime-report.md      # 可写目标，错误地写 186
├── blocks/untouched-reference.md  # 受保护的干扰文件，不许改
├── expected/report.md          # 修复后的报告，168
├── stub-prime.py               # 确定性离线桩（不调模型）
└── run-offline.sh              # 离线一键脚本
```

## 机制

- `input/prime-oracle.json` 是 `authoritative`，冲突时以它为准。
- `blocks/prime-report.md` 是唯一的可写目标；修复叶只能改它。
- `product/answer.txt` 与 `blocks/untouched-reference.md` 在 `read_only` 里；sandbox 会拒绝写入。
- 终审、修复叶、复核叶都会收到事实表：`prime_count=168`。

## 期望

1. 审计/父节点即使声称 oracle=186，也会被事实表纠正。
2. 冲突 blocker 被改写为「报告 186 必须改为 168」。
3. 修复叶把报告改为 168。
4. `product/answer.txt`、`input/`、`blocks/untouched-reference.md` 字节不变。
5. 终审 0 blocker，root 完成。

## 跑法

### 离线（不需要模型，30 秒内）

```bash
pip install -e .
agent-harness-tree demo prime-report
# 或：bash examples/prime-report/run-offline.sh
# 期望末行：DEMO PASS: blocks/prime-report.md 186 -> 168, protected files unchanged
```

离线 demo 用 `stub-prime.py` 这个确定性桩，证明的是**驱动器 + 机械验收 + 修复闭环**，不是模型能力。
桩的终审判断读磁盘：报告已是 168 就不再报冲突（不会制造假阳性）。

### 真实运行（需要本地模型服务 + pi）

```bash
# 端点与模型 id 由 harness/session.py 的 DIRECT_BASE_URL / MODEL_ID 决定；
# TREE_USE_LLAAT=0 直连本地端点，=1 走队列适配器
TREE_USE_LLAAT=0 agent-harness-tree run \
  --input examples/prime-report/task.md \
  --fixture examples/prime-report \
  --run-id prime-$(date +%s)
agent-harness-tree verify --run-dir runs/prime-<id> --case selfheal
```

输出 `state.json` / `final_audit.jsonl` / `rounds.jsonl` / `evidence_ledger.jsonl`；
`verify` 用机械判据（文件字节、哈希、blocker 数）判定，不看模型自述。
夹具与冻结哈希在 `tests/fixtures/prime-selfheal/`，说明见 `docs/acceptance.md` 的「B 的公开复现」。
