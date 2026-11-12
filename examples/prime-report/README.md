# Prime report：权威冲突示例

这个示例对应 B 验收：报告写 186，权威 oracle 是 168，受保护产物 `product/answer.txt` 是 168。

```
examples/prime-report/
├── input/prime-oracle.json     # 权威、只读
├── authority.json              # 只读/可写边界
├── product/answer.txt          # 受保护，168
├── blocks/prime-report.md      # 可写目标，错误地写 186
└── expected/report.md          # 修复后的报告，168
```

## 机制

- `input/prime-oracle.json` 是 `authoritative`，冲突时以它为准。
- `blocks/prime-report.md` 是唯一的可写目标；修复叶只能改它。
- `product/answer.txt` 在 `read_only` 里；sandbox 会拒绝写入。
- 终审、修复叶、复核叶都会收到事实表：`prime_count=168`。

## 期望

1. 审计/父节点即使声称 oracle=186，也会被事实表纠正。
2. 冲突 blocker 被改写为「报告 186 必须改为 168」。
3. 修复叶把报告改为 168。
4. `product/answer.txt` 与 `input/` 字节不变。
5. 终审 0 blocker，root 完成。

## 跑法

真实运行需要本地模型服务和 pi；项目根目录的 `docs/acceptance.md` 有完整命令。离线测试只验证组件，不需要模型。
