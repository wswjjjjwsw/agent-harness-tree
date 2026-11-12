# 架构

## 目标与非目标

**目标**：在小参数本地模型上，把「长任务」变成「可验收的短任务集合」，并保证失败可定位、可恢复、可升级。

**非目标**：不是通用 Agent 框架，不追求支持所有工具/模型；不是把大模型能力蒸馏给小模型；不是可视化工作流编辑器。

## 1. 逻辑树

### 节点

每个节点 20 个字段，写入者固定：

| 字段 | 写入者 | 说明 |
|---|---|---|
| `id` / `parent` / `state` / `result` / `llaat_id` / `round` / `act` / `batch` | 代码 | 身份、状态、轮次、批次 |
| `context` / `judge` / `验收标准` / `merge` / `role` / `overrides` / `auth` | 父 | 任务、判据、验收、权限 |
| `role_prompt` | role 表 | 角色提示词 |
| `reply` / `未决` | 模型 | 产出与未决 |
| `judge_ok` / `judge_why` | LLAAT | 机械判据结论 |

### 状态机

```
待跑 ──→ 在跑 ──→ 完成
  ↑        │        ↑
  │        ├──→ 失败 │
  │        ├──→ 等子节点 ──→ 待跑
  │        └──→ 等裁决 ──→ 待跑 / 失败
```

- 完成 / 失败是封闭终态，不许再迁移。
- 等子节点时，该层写权限冻结（派生状态，不新增字段）。
- round 单调递增，强制收束只允许 `max_rounds+1` 那一轮。

### batch

父每次 spawn 产生一个 batch；只有当「本批次的子节点全部终态」时才允许 merge。更早批次的子节点、别人挂上来的节点不计入。这保证父不会在子任务还没结束时提前汇总。

## 2. Act 流水线

| act | 谁跑 | 输入 | 输出 |
|---|---|---|---|
| `plan` | 根 / 规划 / 决策 | 任务 + 历史 | 要点列表 |
| `spawn` | 同上 | 要点 + 历史 + 本批上下文 | `children[]`（role/context/judge/验收标准/auth） |
| `merge` | 同上 | `batch_view`（子节点 reply/未决/产物） | `去向`（收束/再派/切分）+ `reply` + `未决` + `uncovered` |

`merge` 不是裁判。它只能汇总、再派、或把未决上抛；不能丢弃子节点的硬未决。

## 3. 角色

| role | may_spawn | may_write | tools | 用途 |
|---|---|---|---|---|
| 根 | yes | no | none | 全局规划与收束 |
| 规划 | yes | no | none | 子任务展开 |
| 决策 | yes | no | none | 取舍与重规划 |
| 执行 | no | yes | read,bash,grep,find,ls,write | 产出文件 |
| 调研 | no | no | read,bash,grep,find,ls | 只读取证 |
| 审计 | no | no | none | 只读复核 |

每个角色的上下文/输出/思考预算、轮次上限都在 `role.yaml`；执行器只从表里读，不硬编码。

## 4. 独立验收

终审（`final_audit.on_converge`）只在 `merge.去向 == "收束"` 时触发：

1. 拼全量上下文：`audit.jsonl` 全部行 + 全树内容会计视图 + 权威事实表。
2. 跑一个审计检查点（role=审计）。
3. 解析 blocker；逐条做来源核对（引用路径存在吗）。
4. 机械再核验：结构断言、可度量项、权威事实改写。
5. 无 blocker → 允许完成；有 blocker → 本轮收束作废，父重新 merge。

作废达到上限后，进入 `_audit_cap_outcome`：

```
结构缺口 → 补审计
E_AUTH → 等用户
E_BUDGET / E_BROKEN → 明确失败
E_FACT → 有可写目标 → 修复叶；无目标 → 明确失败
E_POLICY → 只读复核 → Leo → 用户
```

## 5. 权威与写入边界

见 [authority.md](authority.md)。

## 6. 持久化与恢复

- `state.json`：全树快照 + scheduler 计数（每步原子写）。
- `rounds.jsonl`：每轮流水（act、schema、耗时、去向）。
- `audit.jsonl`：审计账本。
- `final_audit.jsonl`：终审检查点与 blocker 数。
- `recovery.jsonl`：策略切换。
- `escalation.jsonl`：原因分类与路由。
- `adjudication/<issue>/`：Leo 请求、worker 状态、回执。
- `recover.jsonl`：启动 reconcile。

resume 时加载 snapshot，已完成节点不重跑；在跑的节点通过 LLAAT `GET /task/<id>` 对账；裁决节点读回执，没有回执才重新提交。

## 7. Runtime

- `session.py`：拼 pi argv、上下文预算、SerialLock、sandbox profile。
- `submit.py` / `llaat_adapter.py`：提交 LLAAT 队列，适配 OpenAI chat completions。
- `cc_run.py` / `cc_agents.py`：Claude Code 路径（可选）。
- 测试用 stub runner：`TREE_ACT_CMD` 指向桩脚本，一个 pi 进程都不起。

## 8. 模块映射

| 概念 | 文件 |
|---|---|
| 节点 / 状态机 / 断言 | `harness/node.py`、`harness/gate.py`、`harness/fields.py` |
| 契约 | `harness/schema.py`、`harness/contract.py` |
| 角色 | `harness/role.py`、`role.yaml` |
| plan/spawn/merge | `harness/driver.py` |
| 调度循环 / 恢复 / 裁决等待 | `harness/leafrun.py`、`harness/recovery.py`、`harness/recover.py` |
| 审计 | `harness/audit.py` |
| 终审 / blocker / 修复 / 复核 | `harness/final_audit.py` |
| 原因分类 / 路由 | `harness/escalation.py` |
| 权威 / 事实表 / 冲突改写 | `harness/authority.py` |
| 持久回执 | `harness/adjudication.py` |
| 模型调用 / sandbox | `harness/session.py` |
| 队列 | `harness/submit.py`、`harness/llaat_adapter.py` |

### 后续拆分（stage 2）

```
harness/
  core/        node, gate, fields, role, schema, contract
  scheduler/   driver, leafrun, recovery, recover, progress, budget
  verify/      audit, final_audit, evidence, trust, shape_review
  escalate/    escalation, authority, arbitration, adjudication
  runtime/     session, submit, llaat_adapter, cc_run, cc_agents
  cli.py
```

`driver.py` / `leafrun.py` / `final_audit.py` 仍是单文件；先把行为用测试锁住，再按上表拆。不要在没有任何测试覆盖的情况下搬文件。

## 9. 设计取舍

- **为什么不用现成图编排？** 图的边是静态的，这里的树是模型每轮动态生成的；核心难点是验收与升级，不是编排原语。
- **为什么父不写文件？** 写权限集中在执行叶，父只做决策；这样修复目标、审计对象、权限边界都清晰。
- **为什么审计叶无工具？** 审计只看 context 里给的证据，避免它自己去「发现」新事实而引入新的幻觉；需要的文件由 Harness 用 fact sheet 附上。
- **为什么 merge 不是裁判？** 让模型在 merge 里「判定完成」等于自评；验收必须由独立检查点做。
