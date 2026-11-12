# 权威边界：谁是权威，谁只读，谁可写

## 问题

B 验收里，审计叶声称 `input/prime-oracle.json` 是 186，真实值是 168。父节点信了，修复叶把受保护产物 `product/answer.txt` 改成了 186，终审还把只读 oracle 当成「要修的一方」。

根因不是模型不够聪明，而是运行时没有「权威来源」这个概念：

- 谁都可以声称某个文件是多少，程序不核对；
- 谁都可以改 `input/` 和 `product/`，sandbox 不拦；
- 冲突时由模型按文本自信度选边。

## authority.json

run 目录下可选：

```json
{
  "authoritative": ["input/prime-oracle.json"],
  "read_only": ["input/", "product/answer.txt", "blocks/untouched-reference.md"],
  "writable": ["blocks/prime-report.md", "doc/"]
}
```

- `authoritative`：权威来源，天然只读。冲突时以它为准。
- `read_only`：模型不可写，但可以作为证据。
- `writable`：允许模型写入的路径；可以是目录或单文件。
- 缺省策略（没有 `authority.json`）：`input/` 只读权威，`product/` / `blocks/` / `doc/` 可写。

路径必须是 run 内相对路径；绝对路径和 `..` 会被拒绝。

## 事实表

`authority.fact_sheet(run_dir)` 从磁盘读取权威文件，渲染成紧凑事实表：

```
## Harness 权威事实（程序读取，非模型陈述）
- input/prime-oracle.json（只读权威，sha256 07350ede954c，129 字节）：
  range_start=1；range_end=1000；inclusive=true；prime_count=168；method="independent trial division"

规则：以上是磁盘实际内容；任务描述、审计结论或父节点指令里的数值若与此不一致，
以此表为准。只允许修改标记为「可写目标」的文件；只读/权威文件不得修改。
```

它被注入到：

- 终审上下文（`final_audit.build_context`）；
- 修复叶上下文（`final_audit.build_repair_child`）；
- 复核叶上下文（`driver._dispatch_review_leaves`）；
- 普通子节点上下文中的被引用小文件（`driver.bound_fact_block`，最多 4 个，每个 ≤2KB）。

## 冲突改写

`authority.canonical_conflict(item, run_dir)`：

1. 从 blocker 文本里提取引用路径；
2. 找到唯一的 `authoritative` 文件和唯一的 `writable` 目标（优先 blocker 点名的，其次策略里唯一的非 `doc/` 文件）；
3. 读磁盘真实值；
4. 如果目标内容已经包含权威值 → 返回 `None`（已解决）；
5. 否则返回改写后的 blocker：
   「权威 `input/prime-oracle.json` 的实际值为 `prime_count=168`（只读）；可写目标 `blocks/prime-report.md` 当前内容为 186，必须改为 168。」

`conflict_resolved(item, run_dir)` 用于合并父节点未决时机械丢弃已经解决的冲突。

## sandbox 执行

`session._sandbox_profile_text(run_dir)` 生成 macOS sandbox profile：

```
(allow default)
(deny file-write* (subpath "~/agent-harness"))
(deny file-write* (subpath "~/mini"))
...
(deny file-write* (subpath "<run_dir>"))
(allow file-write* (literal "<run_dir>/blocks/prime-report.md"))
(allow file-write* (subpath "<run_dir>/doc"))
(allow file-write* (subpath "<session_dir>"))
(allow file-write* (subpath "<agent_dir>"))
```

- 先 deny 整个 run 目录：新增的 `input/` 文件、模型自造路径默认都不可写。
- 再只 allow `authority.writable` + pi 会话目录。
- macOS sandbox 规则「后匹配优先」，所以白名单必须写在 deny 之后。
- `TREE_PI_SANDBOX=0` 可关闭，仅供回归；非 Darwin 直接跳过。

## 一次 B 的实际效果

修复前：

- 审计叶：oracle=186（编造）；
- 父 merge：answer.txt 的 168 是错的，要改；
- 修复叶：用工具把 `product/answer.txt` 写成 186；
- 终审：oracle 仍为 168，与 answer 不一致 → 复核叶放行；
- 结果：root 完成，但产物错误。

修复后（`authority.json` + fact sheet + sandbox）：

- 审计叶读到真实 oracle=168；
- 冲突 blocker 被改写成「报告 186 必须改为 168」；
- 修复目标只可能是 `blocks/prime-report.md`；
- `product/answer.txt` 和 `input/` 物理上写不进去；
- 终审三份文件一致，root 完成。

## 不覆盖的情况

- 符号链接/硬链接绕过：`_resolve` 用 `resolve()` 做前缀检查，但 sandbox 规则按 canonical 路径；需要更多测试。
- Linux：sandbox profile 是 macOS 专属；Linux 需要 namespace / seccomp / bind mount。
- harness 进程本身不受 sandbox 限制；受控写入（managed doc）由 harness 执行，所以 target 必须在 `writable` 里。
- 多权威互斥：不自动改写，交给复核/人。
- 受保护产物如果是目录：`read_only` 用前缀匹配，`writable` 里的子路径仍可能被 allow；需要按最长前缀决定优先级（当前 `read_only` 优先）。
