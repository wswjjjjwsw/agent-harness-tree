# 上升机制：按原因路由，不按状态爬梯

## 问题

旧实现有三条半并行通道：

- 叶失败 → `_consult_arbitrator`（`live=False`，实际是死路径）；
- 终审 blocker → 修复叶 → 复核叶 → Leo → 用户；
- 终审作废上限 → 修复 / 明确失败；
- 节点「等裁决」→ 轮询回执。

它们都按「节点现在是什么状态」触发，而不是按「为什么不能本地继续」。结果是：

- 事实冲突（报告 186 vs 权威 oracle 168）被当成判断题，送去复核/Leo；
- 工具/会话故障被送进同一条语义通道；
- 方法失效（换两种策略仍失败）被当成「需要裁决」；
- 授权类 blocker 也可能先经过模型，再转给人。

B 验收把代价暴露得很清楚：审计叶自己声称 `input/prime-oracle.json` 是 186，真实值是 168；父节点信了，修复叶把受保护产物改成了 186；终审还把只读 oracle 当成「要修的一方」。

## 六个原因

| 代码 | 原因 | 判断依据 | 本地先做什么 | 升级到谁 |
|---|---|---|---|---|
| `E_FACT` | 事实冲突 | 权威文件与派生文件数值/内容不一致 | 重算、回读权威、改派生侧 | 不升级；权威本身互斥才转 `E_POLICY` |
| `E_EVIDENCE` | 证据缺口 | 引用不存在、文件读不到、缺关键约束 | 研究叶 / 父重规划补证据 | 仍缺 → 用户 |
| `E_POLICY` | 判断/取舍 | 多种合法做法、质量判断、无法机械化 | 只读复核 | Leo → 用户 |
| `E_AUTH` | 授权/红线 | 删除、发布、付款、权限、不可逆 | 不做 | 直接用户 |
| `E_BROKEN` | 方法失效 | 同一失败类换 2 种策略仍失败 | 父重规划（换分解/role/工具） | 预算耗尽 → 明确失败 |
| `E_BUDGET` | 预算耗尽 | 轮次/节点/token/时间到顶 | 停止派发，保留证据 | 明确失败 |

## 路由

```
E_FACT      → mechanical / rework → 回机械复验
E_EVIDENCE  → research → 回机械复验；仍缺 → user
E_POLICY    → review → leo → user
E_AUTH      → user
E_BROKEN    → research（父重规划）→ 回机械复验；耗尽 → fail
E_BUDGET    → fail
```

只有 `E_POLICY` 允许调用外部裁决。`needs_external_judge(reason)` 在代码里就是这一条。

## 什么不应该上升

1. schema / 字段缺失 / JSON 不可解析 → 换策略重试或修复叶。
2. 工具超时、会话丢失、队列故障 → 同策略有界重试。
3. 产物与权威输入不一致 → 重算 / 回读 / 改派生侧。
4. 假 blocker（引用不存在、数值与权威不符）→ 机械改写或降级为「未核验」。
5. merge 漏字段、漏未决、覆盖子发现 → 拒收该轮，重新 merge。
6. 同一 blocker 重复出现 → 去重，不重复派发。
7. 文档长度、文风、详略偏好 → NOTE，不是 blocker。
8. 子节点只说「不确定」但给不出具体证据缺口 → 重派更窄任务，不上升。
9. 复核叶已经用同一模型看过、没有新证据 → 不再派第二次复核。

## 自愈四层 + 终止

| 层 | 处理对象 | 动作 | 预算 | 账本 |
|---|---|---|---|---|
| H1 瞬时 | env / timeout / session | 同策略重试 | 2 次 | `env_retry.jsonl` |
| H2 策略 | tool / citation / schema | 换 role / 工具 / 查询焦点 | 2 种不同策略 | `recovery.jsonl` |
| H3 结构 | 分解错 / 覆盖缺 / 方法失效 | 父重规划（再派 / 切分） | `max_rounds` | `rounds.jsonl` |
| H4 事实 | 产物与权威不一致 | 重算 / 回读 / 改派生侧 | 2 轮 | `recovery.jsonl` |
| H5 终止 | 预算 / 授权 / 判断 | 上升或明确失败 | 路由预算 | `escalation.jsonl` |

## 六条不变量

1. 每次重试必须改变 strategy fingerprint；相同 fingerprint 直接拒。
2. 同一 `(父, 失败类)` 最多 2 种不同策略。
3. 修复目标必须在 `authority.json` 的 `writable` 白名单里。
4. 已完成的节点不重跑。
5. 修复后必须回机械复验。
6. 每个上升/自愈动作都要留一行账；没有账的动作视为没发生。

## 代码映射

| 层 | 代码 |
|---|---|
| 原因分类 | `harness/escalation.py`：`classify_blocker` / `classify_failure` / `route_for` / `record` |
| 权威事实 | `harness/authority.py`：`fact_sheet` / `canonical_conflict` / `conflict_resolved` |
| 终审路由 | `harness/final_audit.py`：`reverify_blockers` / `repair_specs` / `build_repair_child` |
| 调度路由 | `harness/driver.py`：`_blocker_reasons` / `_audit_cap_outcome` / `_refresh_adjudication_issue` |
| 用户通道 | `harness/leafrun.py`：`_begin_adjudication(stage=user)` / `_resume_adjudication(status=user)` |
| 自愈策略 | `harness/recovery.py`：`RecoveryController.choose` |

## 一个例子：168 vs 186

1. 终审输入附带事实表：`input/prime-oracle.json: prime_count=168`，`product/answer.txt: 168`，`blocks/prime-report.md: 186 primes`。
2. 审计/父节点即使声称 oracle=186，`reverify_blockers` 也会用磁盘事实改写 blocker：
   「权威 `input/prime-oracle.json` 的实际值为 `prime_count=168`（只读）；可写目标 `blocks/prime-report.md` 当前内容为 186，必须改为 168。」
3. 分类为 `E_FACT`，`repair_specs` 只选可写目标 `blocks/prime-report.md`。
4. 修复叶的 context 附事实表 + 写入边界；sandbox 只允许写报告和 `doc/`。
5. 修复后终审复验：三份文件一致，0 blocker，root 完成。
6. 如果权威本身互斥（两份 `input/` 文件冲突），不猜；分类为 `E_POLICY` 或 `E_BUDGET`，交给复核/人，或明确失败。

## 已知限制

- 原因分类是词表 + 引用 + 数值的机械启发式，不是语义理解；判不准时默认 `E_POLICY`，宁可多一次复核，不默认通过。
- 多权威互斥不会自动改写；这是有意的，避免程序替人决定「哪个权威是真的」。
- `E_POLICY` 的复核叶仍是同一模型，只解决上下文污染，不解决能力上限。
