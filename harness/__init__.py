"""tree-02 逻辑树驱动器骨架（role 制）的库。

模块分工（task-def §2 / §3）：
* role.py   —— role 表加载 + 覆盖边界（validate / effective / apply_overrides）
* gate.py   —— 五道硬闸 + 越权/截断的「记一笔」
* schema.py —— 四种输出契约（按 act 分型）+ 越权判定
* submit.py —— role 旋钮 → LLAAT 提交参数 + prompt 拼装（ctx 只在这里用）
* land.py   —— 文档落盘 doc/<node_id>.md（**lib 里只有它写 doc/**，模型无文件系统权限）
* node.py   —— 节点 = 逻辑位置：20 项字段表 + 状态机（多轮回路）+ 三条 assert（tree-03）
* driver.py —— 父的一轮循环：plan/spawn/merge 三 act + 三道硬闸 + 本批次口径（tree-03）
* session.py —— pi --session-id 外置保真历史：一节点一 id、保真读回、串行锁（tree-03）
* recover.py —— 启动 reconcile：只查一次 GET /task/<id>，父节点连 round/act/batch 一起核（tree-03）
* audit.py —— 审计枝：代码挂 / 扁平 / 分批抽检 / 承载 / 未审账（tree-04）
* final_audit.py —— 全量终审：机械触发（合并去向=收束）+ blocker 作废收束 + 回退一轮 + 到数兜底（tree-04）
* shape_review.py —— D10 形状审查（恢复）：越建议值 ⇒ 结构快照 + 只给建议、由父裁决（tree-04）
"""
