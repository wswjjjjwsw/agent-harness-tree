# 验收方法与结果

## 总则

- 每个用例在**独立 run 目录**里跑；不覆盖旧证据。
- 预登记（prereg）在第一次模型调用前写好；判据不事后松绑。
- 运行前校验工作树 HEAD 与 SHA 一致、工作树干净。
- 验收只看**磁盘产物 + 账本 + 回执**，不看模型自述。

## A：裁决恢复

**目标**：一个真实的 Leo 裁决回执，跨进程恢复后应用到原节点，父节点正常完成。

**方法**：

1. 复放原裁决卡字节（request / judgement / worker 三文件 + 匹配的等待检查点），不重建卡。
2. 起新进程 resume。
3. `verify_harness_a.py` 检查 8 项：请求/回执身份、真实 Leo pass、检查点被新进程读取、原节点应用了精确 issue、父完成、叶未重跑、facts 未变、source 未变。

**结果**：PASS（`a-leo-resume-20261112-03`，8/8）。

**为什么之前卡住**：`--reuse-receipt-from` 重建卡时，两条证据顺序互换，canonical JSON 哈希变化，真实回执失效。正解是复放原卡字节。

## G2：内容契约

**目标**：终审只看产出，不用过程记账（`state`/`judge_ok`/`attempt`）判死正确产物。

**用例**：

- q01：读输入、算出正确数值、写 `product/answer.txt`。
- agg2：聚合 6 个月度文件，写 `product/agg.json`，逐键等于 oracle。

**结果**：两者 PASS（`g2-q01-content-20261112-02`、`g2-agg2-content-20261112-01`）。

## B：自愈与权威边界

**目标**：报告写 186、oracle 是 168；Harness 必须只改可写的报告，不能改 oracle 或受保护产物，最终 root 完成。

**夹具**：

- `input/prime-oracle.json`：`prime_count=168`，权威、只读。
- `product/answer.txt`：`168\n`，受保护。
- `blocks/prime-report.md`：`186 primes`，可写目标。
- `blocks/untouched-reference.md`：受保护。
- `authority.json`：`writable = ["blocks/prime-report.md", "doc/"]`。

**判据**：

1. 真实终审 blocker 引用 `blocks/prime-report.md`。
2. Harness 创建真实 pi 修复叶，绑定报告。
3. 报告改为 168，且不再含 `186 primes`。
4. `product/answer.txt` 与 `blocks/untouched-reference.md` 哈希不变。
5. 预置叶 `root.c1.1` 不重跑。
6. 同一 run id 内 root `完成`。
7. 最后一条 `final_audit.jsonl` 的 blocker 数为 0。
8. `authority.json` 的 writable 列表未被改。

**修复前**：

- `b-selfheal-content-20261112-02`：修复叶把 `product/answer.txt` 改成 186；终审把 oracle 当成要修的一方；复核叶放行；root 完成但产物错误。
- `b-selfheal-content-20261112-03`：审计叶自己声称 oracle=186；root 派 spawn 改 answer，输出 19,114 字不可解析，root 失败。

**修复后（逐轮暴露的缺口）**：

- `b-selfheal-content-20261113-01`（`8031c12`）：审计叶已正确读到 oracle=168（事实注入生效），但 `bound_fact_block` 只附 2 个文件，读不到报告；root 重新派发时又生成 19,009 字不可解析 spawn。**中止，作为 GAP-6 现场保留。**
- `b-selfheal-content-20261113-02`（`ee1292d`，附件上限 2→4）：审计叶三文件全读，正确指出「报告 186 vs oracle 168」；但 reply 字符串内有裸换行，`json.loads` 拒收，整叶判形状不合。**中止，作为 GAP-8 现场保留。**
- `b-selfheal-content-20261113-03`（`0c8b148`，JSON 控制字符容错）：审计叶通过、结论正确；root merge 定位报告为目标；但 spawn 把报告正文塞进 child context，8,192 token 截断，19,087 字不可解析。**中止，作为 GAP-9 现场保留。**
- `b-selfheal-content-20261113-04`（`8917b01`，child context ≤4000 硬约束）：继续修复阶段的验收 run。

**结果**：PASS（`b-selfheal-content-20261113-12`，`control.py verify-live` 8/8）。该 run 记录的 HEAD 是 `89971e7`；之后的提交没有在 B 上重放，报告引用时按实际 HEAD 标注。

| 判据（与上文 8 条一一对应） | 结果 | 证据 |
|---|---|---|
| 1 终审记录 blocker 且引用 `blocks/prime-report.md` | PASS | `verification.json.terminal_audit_findings` 4 条（`root.f4.1` / `f4.2` / `f9.3` / `f9.4`），首条即报告 186 vs oracle 168 |
| 2 创建真实修复叶并绑定报告 | PASS | `verification.json.repair_nodes` = `root.fix.audit2.1`，`managed_doc_target=blocks/prime-report.md`，`managed_doc_write.ok=true` |
| 3 报告改为 168 | PASS | 目标 `sha256` 修复后 = `f4b7845…4718` |
| 4 `product/answer.txt` / `untouched-reference.md` 哈希不变 | PASS | `c02efad…5d21` / `9b60715…ec9` |
| 5 预置叶不重跑 | PASS | `rounds.jsonl` 中 `root.c1.1` 出现 0 次（预置即完成，未执行） |
| 6 同 run id 内 root 完成 | PASS | `state.json` root=`完成` |
| 7 末条 `final_audit` blocker 数 = 0 | PASS | 最后一行 `blocker数:0` |
| 8 `authority.json` writable 未改 | PASS | 文件 sha 与 prereg 一致 |

复跑与离线复现见本节末「B 的公开复现」。

上表哈希与节点 id 都属于 `b-selfheal-content-20261113-12` 这次 run；公开夹具里的期望报告是另一份等价文本，其 sha256 冻结在 `tests/fixtures/prime-selfheal/prereg/acceptance.json`，两者不要混用。

这三轮失败不是白跑：每一轮都把一个「提示词约束」变成了「运行时机制」——附件上限、JSON 容错、子上下文长度。最终判据仍然是原来那 8 条，没有放松。

## 离线测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

覆盖：

- 状态机 / 契约 / schema；
- 审计 / 终审 / 裁决回执；
- 权威策略 / 事实表 / 冲突改写 / 已解决判定；
- sandbox profile 文本；
- 原因分类 / 路由 / 账本；
- 自愈策略 fingerprint / 策略耗尽；
- 未决保留与已解决冲突丢弃。

结果：125 项（本机 Python 3.13：125 OK / 1 skip；CI 覆盖 3.9 / 3.11）。

## B 的公开复现

夹具在 `tests/fixtures/prime-selfheal/`（相对路径、不含用户名，`prereg/acceptance.json` 冻结了逐文件 sha256 与 8 条判据）。

**离线（不需要模型，30 秒内）**：

```bash
pip install -e .
bash examples/prime-report/run-offline.sh
# 期望末行：DEMO PASS: blocks/prime-report.md 186 -> 168, protected files unchanged
```

离线 demo 用 `examples/prime-report/stub-prime.py` 这个确定性桩，证明的是**驱动器 + 机械验收 + 修复闭环**，不是模型能力。

**真实运行（需要本地 OpenAI 兼容端点 + pi）**：

```bash
# 端点与模型 id 由 harness/session.py 的 DIRECT_BASE_URL / MODEL_ID 决定；
# TREE_USE_LLAAT=0 直连本地端点，=1 走队列适配器
TREE_USE_LLAAT=0 agent-harness-tree run \
  --input tests/fixtures/prime-selfheal/input/q01.md \
  --fixture tests/fixtures/prime-selfheal --run-id prime-$RANDOM
agent-harness-tree verify --run-dir runs/prime-<id> --case selfheal
```

`verify` 只看磁盘：报告字节、受保护文件哈希、受保护清单、root 终态、终审 blocker 数，不看模型自述。

## 如何复跑

```bash
EVID=~/harness-evidence/harness-content-20261112
TREE=~/agent-harness-escalation-20261113
SHA=$(git -C $TREE rev-parse HEAD)

# B
HARNESS_TREE=$TREE /usr/bin/python3 $EVID/b/control.py prepare --sha $SHA --run-id <new-id>
HARNESS_TREE=$TREE /usr/bin/python3 $EVID/b/control.py dry-check --sha $SHA --run-id <new-id>
/usr/bin/python3 $EVID/run_harness_acceptance.py $TREE $SHA $EVID/b/runs/<new-id> --resume
HARNESS_TREE=$TREE /usr/bin/python3 $EVID/b/control.py verify-live --sha $SHA --run-id <new-id>
```

## 脱敏

公开仓库不包含 run 目录、pi 会话、模型绝对路径、队列/代理/外部裁决服务的私有地址。验收表只保留结论与判据；原始证据留在私有机器。
