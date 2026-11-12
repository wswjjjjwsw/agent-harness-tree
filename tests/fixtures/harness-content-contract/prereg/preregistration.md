# 第四轮预登记（冻结版）

- 任务：bridge t-132692485362 第四轮第二阶段 live 验收（G1/G2）
- 冻结时间：2026-11-11 10:40 Asia/Shanghai（两例 live 重跑开始前写定；跑后不得改）
- 决策：GPT 已定——**选 C：有界、只读、纯复核型修复叶处理不可修非安全 blocker；不选 A/B 的自动丢弃 blocker**。本轮按 C 实现，不许擅自改选。
- 隔离代码：树候选 `~/agent-harness/runs/.candidate-49dec4ba` 分支
  `integration-acceptance-20261110` @ `16a601b`（第四轮第一阶段 1 个 commit：
  决策 C 纯复核叶 + 三缺陷修——终审检查点未决归一、merge 再派同义映射、
  审计叶引用不存在文件救援；含离线测试 cases-audit-repair 100/100、
  cases-evidence 23/23；其余套件与 93b152a 基线逐项一致，零回归）
- 隔离环境：mini；树隔离适配器 18905 → LLAAT 8903 → Sage 8092；
  翻译代理 18904 不用（G3/G4/G5 复验是下一阶段，不在本轮）
- 启动口径（两例完全相同，除 run id 与输入文件）：
  `TREE_LLAAT_ADAPTER_URL=http://127.0.0.1:18905/v1 TREE_LLAAT_URL=http://127.0.0.1:8903
  TREE_RUNS_DIR=/tmp/integ-accept4/runs TREE_PI_TIMEOUT=300 TREE_MAX_STEPS=40
  bash driver.sh --input <input.md> --run-id <id>`
  （适配器由 ensure_adapter 按 TREE_LLAAT_ADAPTER_URL 自动起；不设 TREE_ACT_CMD、
  不设 TREE_STATE_DIR；不许用 mock 代替真实 G1/G2）
- 机械核验口径：字数 = Python `len()`（与判据 chars_min 同口径）；答案值逐字节比对；
  JSON 产物逐键比对 oracle；叶判据机械核验结果取落盘 `disk_doc_check` 字段；
  验收判据以终审 ledger（final_audit.jsonl 的"已消除"原因行）+ env_retry.jsonl 台账
  + 修复叶 judge（chars_min）+ 核验脚本（sum_tokens、verify_q01/verify_agg）
  + answer/doc 的 `len()` 重算为准，不以模型自报为准。

## 本轮相对第三轮预登记的修订（冻结前写定）

- 修订 1（决策 C 断言）：复核叶只在"无可执行修复规格"时派发（有可修规格先走修复，
  不并行）；复核叶 role=审计（只读）、id 含 `.fix.review`、judge 为 json_keys、
  context 含被复核 blocker 原文 + 磁盘产物实测；复核消耗 1 轮修复预算
  （FINAL_AUDIT_REPAIR_MAX=2 共用计数），每父至多 1 轮复核
  （FINAL_AUDIT_REVIEW_MAX=1）；复核结论采纳须同时满足：复核叶完成、
  judge_ok、未决为空、reply 含"不阻断"实测结论、产物 sha256 与复核时一致；
  采纳的 blocker 仍记终审"已消除"（留痕，不抹除）；安全/结构性 blocker 永不进复核。
- 修订 2（缺陷修断言）：终审检查点 未决[].type 越出枚举时不得抛 ValueError
  （归一进 ledger"未决归一"原因行）；merge 处置/建议='再派' 不得显式失败
  （归一为 处置=解掉/建议=继续派，rounds 行记 audit_replies_normalized）；
  审计叶引用缩写/伪路径不得致证据门失败（省略号引用救援或跳过、工具名前缀救援，
  歧义与任意错目录仍拒收）。
- 修订 3（步数/轮次预算，到数修复回放）：TREE_MAX_STEPS=40（第三轮同口径）。
  到数修复/复核回放最多 +2 轮（修复预算 2，未改任何上限数值：
  FINAL_AUDIT_MAX_RETRY=2、FINAL_AUDIT_REPAIR_MAX=2 与 a784526 逐行一致；
  新增 FINAL_AUDIT_REVIEW_MAX=1 是复核轮次上限，不改已有上限数值）；
  父 round 恒 ≤ max_rounds+1，强制收束轮最多重放 2 次。轮次/步数断言按此放宽后口径判定。
- 修订 4（agg 数据放置，第二轮修订 A 的教训）：agg 数据必须拷进每次重跑的
  `input/agg-data/`（driver.sh 建 run 目录后、点火前预置；字节与 oracle 不变，
  每次拷贝后 MD5 核对）；任务文本用 run 内相对路径 `input/agg-data/`。

## 例 1：integ4-q01（Q01 原文；验证决策 C + 三缺陷修）

输入：`/tmp/integ-accept4/input/q01.md`（与第三轮 `/tmp/integ-accept3/input/q01.md`
逐字相同，MD5 `0ff718617362d1f3a5425378fe4f7fa8`）：
> 回答每一条时，必须在 reply 或 未决 的正文里带上它的 ID（形如 Q01）；未解决的条目要原样带 ID 进 未决[]。
>
> Q01 | 冒烟小任务：用 Python 计算 1 到 1000 之间素数的个数，并把结果写成可验证产物 |
> 派一个执行叶：先用 Bash 跑 python3 计算（不许心算），再用 Write 把结果数字单独一行写进
> run 内相对路径 product/answer.txt；doc 不少于 1500 字，写明计算方法、第二种独立算法
> 交叉复核的结果与产物路径。产物文件的数字必须与两种算法一致。

固定机械 oracle（不变）：
- `product/answer.txt` 内容（去空白）= `168`
- 叶 doc（`doc/*.md`，判据所指文件）`len() >= 1500`，且含 `168`
- root 终态 = 完成；全部叶 judge_ok=True；无失败叶；终审账无兜底行（无
  `兜底=次数上限/到数`，无 `终审_blocker_明确失败`）；叶 `disk_doc_check` 全部通过

## 例 2：integ4-agg2（多文件聚合 + 结构化 JSON 产物；验证决策 C——装饰性 blocker 的复核出路）

输入：`/tmp/integ-accept4/input/agg2.md`（与第三轮 agg2.md 逐字相同，
MD5 `03deec48b331dd8d4e102b05734ecf13`）：
> 回答每一条时，必须在 reply 或 未决 的正文里带上它的 ID（形如 AGG1）；未解决的条目要原样带 ID 进 未决[]。
>
> AGG1 | 多文件聚合任务：汇总 run 内相对路径 input/agg-data/ 下 6 个月度数据文件 |
> 派执行叶：用 Bash + Python 读全部 6 个文件（month-01.txt 至 month-06.txt，
> 均在 run 内 input/agg-data/ 下），每文件每行一个整数；计算①每个文件的行数与和；
> ②六个文件总行数与总和；③全部数字的最大值与最小值。把结果写成两份产物：
> product/agg.json（JSON，键 files{文件名:{lines,sum}}、total_lines、total_sum、max、min）与
> doc/agg_report.md（不少于 1500 字，写明方法、逐文件数据、第二种独立算法或
> 逐行复核的结果与产物路径）。JSON 数值必须与文件真实内容一致。

输入数据（`/tmp/integ-accept2/input/agg-data/` 原件，MD5 见下；重跑前原样拷入
`/tmp/integ-accept4/runs/integ4-agg2/input/agg-data/`，拷贝后逐文件 MD5 核对）：
- month-01.txt `e947df0778daa06f88babbf1e072e022`
- month-02.txt `9c1165f07fbc9e932b1e6efe9dc9cbba`
- month-03.txt `a16997b02577f080949c950bc68c9d81`
- month-04.txt `38ca65a97f29aa684d6e1eb35a7062a6`
- month-05.txt `35e04e10acb37d2559be92a6e477c5ac`
- month-06.txt `40fbd0b1ae8a7a42e86a5484f50380d2`

固定机械 oracle（不变，见 `/tmp/integ-accept4/prereg/agg-oracle.json`，
MD5 `647c9c3b91452aad89d40cd4584afaed`）：
- files：month-01 {lines 25, sum 11292}；month-02 {22, 9788}；month-03 {22, 12218}；
  month-04 {30, 14930}；month-05 {21, 9944}；month-06 {28, 13097}
- total_lines = 148；total_sum = 71269；max = 988；min = 18
- `doc/agg_report.md` `len() >= 1500`，且含 71269 与 148
- `product/agg.json` 逐键等于 oracle（lines/sum/total_lines/total_sum/max/min 全等）
- root 终态 = 完成；全部叶 judge_ok=True；无失败叶；终审账无兜底行；叶 `disk_doc_check` 全部通过

## 跑序

q01 → agg2。先验证 Q01 的干净闭环，再验证 agg2（第三轮 agg2 式装饰性 blocker
是决策 C 的主战场：若本轮复现同类 blocker，须走复核叶出路，不得自动丢弃、
不得直接明确失败）。

## G2 通过标准（两例全部满足才算 PASS）

1. 两例 root 均完成，且各自 oracle 全部机械通过（独立复核命令见核验脚本）；
2. 任一例出现：答案/JSON 值不符、doc < 1500 字、叶失败、终审兜底/明确失败、
   disk_doc_check 失败 —— 该例即 FAIL，G2 仍为 PARTIAL，不得以"根完成"冒充通过；
3. 单任务 prompt+completion 统计：每例用 sum_tokens4.py 汇总，任一任务 > 32K
   必须有明确机制解释或拦截记录，否则记为 D 未闭合。
4. 每跑诚实记录，不伪造成功；若一跑诚实失败，记录根因后按跑序继续，不要在同一
   失败模式上反复重跑烧时间；若 q01 与 agg2 均诚实失败且根因是新设计问题，停止
   live 循环、如实 PARTIAL 收尾（第二轮 04:40 就是这么停的）。

## G1 同步记账

- 适配器日志提交数：以**本轮自己的适配器日志实数**为准。
- LLAAT 任务文件增量（跑前/跑后 `~/.llaat/tasks` 计数）、单任务最大 token
  （0 超 32K 为目标）、三方 token 求和（适配器日志提交数 × LLAAT 任务 usage 求和
  交叉核对）。

## 停下事项

- 无。本阶段只做 live 验收；G3/G4/G5 复验与报告更新是下一阶段，不在本轮做。
- 生产不动：不重启生产服务，不改生产 queue.py；跑完按 pid 杀掉隔离适配器 18905，
  确认端口空闲、生产服务未重启。
