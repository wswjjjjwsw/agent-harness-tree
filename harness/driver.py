"""驱动器：父的一轮循环（76 §4.5，task-def §2.2）。

职责：
  · 决定父这一轮跑哪个 act（plan / spawn / merge）—— **不写死任何 role 名字或 thinking 值**，
    形状一律 role.effective(role, act) 取（tree-02 已落地）。
  · 三道硬闸：① spawn 必配 merge（不许连续两轮 spawn）② 轮次到数强制 merge 收束 ③ 收束必带 uncovered
    （③ 由 schema.validate 管，驱动器不绕）。
  · 每轮往 runs/<run_id>/rounds.jsonl 追加一行流水。
  · act_runner 可注入：缺省走 harness/session.py 的真 pi；环境变量 TREE_ACT_CMD 指到一个桩脚本时
    **一个 pi 进程都不许起**（由 harness/session.py 保证不会跑到那条路）。

状态机与 batch 口径：见 harness/node.py 的文件头与 76 §3.2。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from harness import audit as audit_mod, escalation, final_audit, gate, land, node as node_mod, role as role_mod, schema, submit

ACTS = role_mod.ACTS                      # ("plan", "spawn", "merge")
UP = {"再派": "spawn", "切分": "spawn"}
# 第二轮 D：merge 会话轮换周期——每 N 轮 merge 换一个 pi 会话（见 step()）。
# 校准：integ-q01 同一 merge 会话内 prompt 估算逐轮 +~10K（26,079 → 57,057），
# N=2 时单会话历史封顶约 2 轮 ≈ 25K 估算，留给适配器 32K 预算守卫余量。
MERGE_SESSION_ROUNDS = 2


# ---------------------------------------------------------------- 输出解析
def _escape_json_controls(text: str) -> str:
    """把 JSON 字符串内部的裸换行/回车/制表符转义。

    小模型经常输出 `{"reply": "第一行
    第二行"}` 这种非法 JSON；json.loads 直接拒。只在字符串内部转义，
    不碰结构字符，不猜内容。
    """
    out = []
    in_str, esc = False, False
    for ch in str(text or ""):
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def parse_act_output(text: str, act: str) -> dict:
    """从模型正文里取出契约 JSON。取不到 ⇒ raise ValueError（响亮，不静默猜）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("act=%s 的产出是空的（pi 没回任何正文）" % act)
    repaired = _escape_json_controls(raw)
    for candidate in (raw, repaired):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:                               # noqa: BLE001
            pass
    raw = repaired
    # 正文里夹了 JSON（pi --mode text 会带前后解释）⇒ 扫第一个配平的 {...}
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(raw[start:i + 1])
                except Exception:                       # noqa: BLE001
                    start = None
                    continue
                if isinstance(obj, dict):
                    return obj
                start = None
    raise ValueError("act=%s 的产出里找不到一个 JSON 对象（原文 %d 字）：%s"
                     % (act, len(raw), raw[:200]))


_BOUND_FACT_MAX_BYTES = 2048
_BOUND_FACT_MAX_FILES = 4
# child context 是任务说明，不是文件转储。超过这个长度会把 spawn 的 JSON
# 顶到 max_tokens 后截断（B-03：19,087 字、finish_reason=length）。
_SPAWN_CONTEXT_MAX = 4000


def bound_fact_block(context: str, run_dir: str) -> str:
    """Attach the real bytes of the small run files this context cites.

    A parent can state a number about a read-only input that the file does not
    contain; the child then inherits it as fact. Showing the actual content
    keeps the decision with the tree while removing the fabrication.
    """
    text = str(context or "")
    root = Path(str(run_dir or ""))
    if not text or not root.is_dir():
        return context
    try:
        from harness import evidence as _ev_mod
        cites = _ev_mod.seen_citations(text)
    except Exception:                                            # noqa: BLE001
        return context
    picked = []
    seen = set()
    for cite in cites:
        if cite in seen:
            continue
        seen.add(cite)
        try:
            path = (root / cite).resolve()
            path.relative_to(root.resolve())
            if not path.is_file():
                continue
            size = path.stat().st_size
        except (OSError, ValueError):
            continue
        if size <= 0 or size > _BOUND_FACT_MAX_BYTES:
            continue
        picked.append((cite, path, size))
        if len(picked) >= _BOUND_FACT_MAX_FILES:
            break
    if not picked:
        return context
    lines = ["", "", "## Harness 附：本 run 内被引用文件的真实内容（只读）"]
    for cite, path, size in picked:
        try:
            body = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        except (OSError, UnicodeDecodeError):
            continue
        lines.append("- %s（sha256 %s，%d 字节）：" % (cite, digest, size))
        lines.append(body.rstrip())
    if len(lines) <= 3:
        return context
    lines.append("任务描述里若出现这些文件的具体数值，以上面实际内容为准；"
                 "不一致时按实际内容执行，并把不一致写进 未决[]。")
    return text + "\n".join(lines)


_HARD_UNDECIDED = ("冲突", "授权", "判不了")


def _merge_unresolved(previous: list, current: list, run_dir: str = None) -> list:
    """合并父节点未决：新产出为准，但上一轮的硬未绝不会被静默吞掉。

    旧实现 `p["未决"] = obj["未决"]` 是整体覆盖；merge 模型只要不重复写，
    子节点报上来的冲突/授权/判不了就消失了。这里保留上一轮的硬项，按
    (type, what) 去重；真正的解决必须由机械事实或裁决回执显式给出，
    不能靠「这次没提」。

    给了 run_dir 时，冲突项先过 authority.conflict_resolved：
    派生侧已经等于权威值 ⇒ 机械解决，不再保留。
    """
    out = [dict(x) for x in (current or []) if isinstance(x, dict)]
    seen = {(str(x.get("type") or ""), str(x.get("what") or "")) for x in out}
    for item in previous or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in _HARD_UNDECIDED:
            continue
        key = (str(item.get("type") or ""), str(item.get("what") or ""))
        if key in seen:
            continue
        if (run_dir and str(item.get("type") or "") == "冲突"):
            try:
                from harness import authority as _auth
                probe = "%s %s" % (item.get("what") or "", item.get("why") or "")
                if _auth.conflict_resolved(probe, run_dir):
                    continue
            except Exception:                                    # noqa: BLE001
                pass
        out.append(dict(item))
        seen.add(key)
    return out


def _blocker_reasons(blockers: list, run_dir: str) -> list:
    """终审 blocker 逐条归类；失败时按 E_POLICY 保守处理。"""
    rows = []
    for b in blockers or []:
        try:
            row = escalation.classify_blocker(str((b or {}).get("item") or ""), run_dir)
        except Exception:                                        # noqa: BLE001
            row = {"reason": escalation.E_POLICY, "route": escalation.R_REVIEW}
        row["blocker_id"] = str((b or {}).get("id") or "")
        rows.append(row)
    return rows


def _blocker_covered_by_specs(blocker: dict, specs: list) -> bool:
    """这条 blocker 是否有可执行的修复规格（同 id，或空 id 时按 item 前缀）。"""
    bid = str((blocker or {}).get("id") or "")
    item = str((blocker or {}).get("item") or "")
    for spec in specs or []:
        sid = str((spec or {}).get("blocker_id") or "")
        if bid and sid == bid:
            return True
        if not bid and item:
            spec_item = str((spec or {}).get("item") or "")
            if item[:60] and item[:60] in spec_item:
                return True
    return False


def applied_adjudication_view(node: dict):
    """Project only a persisted Leo pass that the driver actually applied."""
    meta = (node or {}).get("_adjudication") or {}
    checkpoint = meta.get("receipt_checkpoint") or {}
    receipt = checkpoint.get("receipt") or {}
    if (meta.get("phase") != "applied" or meta.get("applied") is not True
            or meta.get("last_source") != "leo" or meta.get("last_status") != "accepted"
            or checkpoint.get("source") != "leo" or receipt.get("verdict") != "pass"):
        return None
    issue = meta.get("issue") or {}
    blockers = [
        {key: blocker.get(key) for key in ("id", "item", "level")}
        for blocker in (issue.get("blockers") or []) if isinstance(blocker, dict)
    ]
    return {
        "source": "leo",
        "verdict": "pass",
        "note": str(receipt.get("note") or ""),
        "binding": {
            "issue_id": checkpoint.get("issue_id"),
            "candidate_sha256": checkpoint.get("candidate_sha256"),
            "card_sha256": checkpoint.get("card_sha256"),
            "blockers": blockers,
        },
    }


# ---------------------------------------------------------------- 桩 / 真跑
def act_runner_default(node: dict, act: str, knobs: dict, prompt: str,
                       session_id: str, run_dir: str, timeout: float = None) -> str:
    """缺省实现：走 harness/session.py 的真 pi（真跑路径）。"""
    from harness import session                                 # 局部导入，避免无谓依赖
    return session.run_act(node, act, knobs, prompt, session_id=session_id,
                           run_dir=run_dir, timeout=timeout)


def make_stub_runner(cmd: str):
    """把 $TREE_ACT_CMD 包成 act_runner：`<cmd> <act> <node_id> <round>`，prompt 走 stdin，正文走 stdout。"""
    def _run(node, act, knobs, prompt, session_id, run_dir):   # noqa: ANN001
        proc = subprocess.run([cmd, str(act), str(node.get("id") or ""), str(node.get("round") or 0)],
                              input=prompt or "", capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError("桩 act 脚本 rc=%d：%s" % (proc.returncode, proc.stderr[-400:]))
        return proc.stdout
    return _run


def resolve_act_runner(runner=None):
    """注入优先：参数 > $TREE_ACT_CMD 桩 > 真 pi。"""
    if runner is not None:
        return runner, "injected"
    cmd = os.environ.get("TREE_ACT_CMD")
    if cmd:
        return make_stub_runner(cmd), "stub:%s" % cmd
    return act_runner_default, "pi"


# ---------------------------------------------------------------- 硬闸①
def request_act(parent: dict, want: str, sink: str = None) -> tuple:
    """硬闸①：spawn 必配 merge，不许连续两轮 spawn。结构性拒绝（不是告警）。

    父上一轮 act == spawn ⇒ 下一个 act 必须是 merge；此时再请求 spawn ⇒ 拒 + 记一笔 + 该轮不算数。
    """
    if want == "spawn" and (parent or {}).get("act") == "spawn":
        why = ("上一轮 act=spawn（round=%s）而本轮又请求 spawn ⇒ 拒（spawn 之后必须进「等子节点」、"
               "下一个 act 必须是 merge；76 §4.5.5 硬闸①）" % (parent or {}).get("round"))
        gate.log_violation(sink, kind="驱动_连续spawn", node_id=str((parent or {}).get("id") or ""),
                           detail=why)
        return False, why
    return True, ""


# ---------------------------------------------------------------- 提示词
def next_act(parent: dict) -> str:
    """按父的 (round, act, merge.去向) 推出本轮该跑哪个 act（None = 已收束）。"""
    p = parent or {}
    r = int(p.get("round") or 0)
    last = p.get("act") or ""
    if r == 0:
        return "plan"
    if last == "plan":
        return "spawn"
    if last == "spawn":
        return "merge"
    if last == "merge":
        return UP.get((p.get("merge") or {}).get("去向"))
    return None


def batch_view(parent: dict, tree: dict) -> str:
    """父 merge 轮拿到的「这一批子的会计视图」：reply 原文 + 未决[] + judge 结论（76 §4.3）。

    V2.1 P1（t-5df0e8dd3660）：每子另附 doc 正文摘录（带来源定位）与可信产物清单，
    总量有硬预算（全批 6000 字，每子 2000 字），超了截断并标注出处，不静默丢；
    merge 仍是短决策轮（76 §4.5.1），摘录是证据、不是让模型重写正文。
    """
    run_dir = (parent or {}).get("_run_dir") or ""
    total_budget, per_child_budget, spent = 6000, 2000, 0
    trust = {}
    if run_dir:
        try:
            from harness import trust as _tr
            trust = _tr.read_trust(run_dir)
        except Exception:                                        # noqa: BLE001
            trust = {}
    rows = []
    for nid in sorted(node_mod.batch_members(parent, tree)):
        c = tree.get(nid) or {}
        row = {
            "id": nid, "role": c.get("role"), "state": c.get("state"),
            "judge_ok": c.get("judge_ok"), "judge_why": c.get("judge_why"),
            "reply": c.get("reply") or "", "未决": c.get("未决") or [],
            "result": c.get("result") or "",
        }
        adjudication = applied_adjudication_view(c)
        if adjudication:
            row["已应用裁决"] = adjudication
        excerpt = ""
        if run_dir:
            doc_path = Path(run_dir) / "doc" / ("%s.md" % nid)
            if doc_path.is_file():
                try:
                    text = doc_path.read_text(encoding="utf-8")
                    budget = min(per_child_budget, max(0, total_budget - spent))
                    if budget > 0 and text.strip():
                        if len(text) > budget:
                            excerpt = (text[:budget] + "\n…（截断，共 %d 字，见 doc/%s.md）"
                                       % (len(text), nid))
                        else:
                            excerpt = text
                        spent += len(excerpt)
                except OSError:
                    pass
        row["doc摘录"] = excerpt
        prods = (trust.get(nid) or {}).get("products") or []
        row["产物"] = ["%s:%s（%d 字节，%s）" % (pr.get("kind"), pr.get("path") or "reply",
                                                pr.get("bytes", 0), pr.get("source") or "")
                       for pr in prods]
        rows.append(row)
    return json.dumps({"本批次": (parent or {}).get("batch"), "子节点": rows},
                      ensure_ascii=False, indent=1)


def _history_block(parent: dict) -> str:
    """V1（路线 B 缺口 2）：全树历史叶清单（机械事实，不只看本批次）。

    batch_view 只给本批次（batch == 父.batch）的子；merge 再派后新一批为空，
    根会误判“从未派叶”（V0 终审原话）。这里把父子树下全部非根节点列出来，
    供 merge 与 spawn 共用：id / role / state / judge_ok / judge_why / reply 摘录。
    """
    tree = parent.get("_tree") or {}
    pid = str(parent.get("id") or "")
    rows = []
    for nid in sorted(node_mod.subtree_ids(pid, tree)):
        if nid == pid:
            continue
        n = tree.get(nid) or {}
        # V2: 产物 + 机械验收进清单（旧值只列节点状态，磁盘文件不可见）
        _art = n.get("artifacts") or []
        _ver = n.get("verify") or {}
        _extra = ""
        if _art:
            _extra += " | artifacts=" + ",".join(
                "%s(%dB)" % (str(a.get("path") or ""), int(a.get("bytes") or 0))
                for a in _art[:6])
        if _ver:
            _extra += " | verify=%s/%s" % (_ver.get("passed"), _ver.get("total"))
            if _ver.get("compile_ok") is True:
                _extra += " 编译通过"
        if n.get("verified"):
            _extra += " | 已验证"
        rows.append("%s | role=%s | state=%s | judge_ok=%s | why=%s | reply=%s%s" % (
            nid, n.get("role"), n.get("state"), n.get("judge_ok"),
            str(n.get("judge_why") or "")[:120].replace("\n", " "),
            str(n.get("reply") or "")[:150].replace("\n", " "), _extra))
    if not rows:
        return "\n## 历史叶清单（机械事实，全树）：尚无任何子节点被派生过。"
    _inv = _disk_inventory(parent)
    return "\n## 历史叶清单（机械事实，全树，不只看本批次）：\n" + "\n".join(rows) + _inv


def _disk_inventory(parent: dict) -> str:
    """run 目录下 project/ 的磁盘实况（merge/spawn 可见的另一路机械事实）。

    只读、不写；目录不存在或读失败时返回空串（不阻塞 prompt 组装）。
    """
    try:
        from pathlib import Path as _P
        _rd = (parent or {}).get("_run_dir") or ""
        if not _rd:
            return ""
        _proj = _P(str(_rd)) / "project"
        if not _proj.is_dir():
            return ""
        _fs = sorted((f.name, f.stat().st_size) for f in _proj.iterdir()
            if f.is_file() and f.suffix == ".py")
        if not _fs:
            return "\n## 磁盘实况（project/）：尚无 .py 文件。"
        return ("\n## 磁盘实况（project/，机械扫描，非模型自报）："
            + ", ".join("%s(%dB)" % (nm, sz) for nm, sz in _fs))
    except Exception: # noqa: BLE001
        return ""


def build_prompt(parent: dict, act: str, knobs: dict, table: dict, first_round: bool,
                 stateless: bool = False) -> str:
    """本轮输入。

    ★ ctx_budget 口径（task-def §2.3.5）：历史由 pi 外置会话承载，**不重复注入 L0 契约**——
      首轮（round=1）用 tree-02 的 submit.assemble_prompt（role_prompt 全文 + context + 契约），
      后续轮只给【本轮增量】，L0 由会话里的原文提供（压缩靠格式不靠删内容）。

    ★ stateless=True（CC runner，走 ccswitch 15721 → Sage）：CC 没有 pi 外置会话，
      每轮都是全新请求，历史不存在。首轮照常（assemble_prompt，但 include_role_prompt=False，
      role_prompt 已在 agent 文件的 system prompt 里）；非首轮 = 首轮 base（含契约）
      + 本轮增量（batch_view/要点/被拒原因）——否则 merge 永远看不到子结果、spawn 拿不到
      plan 要点（pi 的“首轮注入一次”假定会话存在，CC 上不成立）。pi 路径
      （stateless=False）一字不动。

    ★ 第二轮 D：换会话的 merge 轮也是"首轮"（fresh_session），但只给契约不给 batch_view
      会让 merge 看不到子结果 ⇒ 首轮 merge 也拼 base + 本轮增量（见函数尾部）。
    """
    # 第二轮 D：首轮判定后移到底部——换会话的 merge 轮也是"首轮"（fresh_session），
    # 但只给契约不给 batch_view 会让 merge 看不到子结果（旧代码的首轮早返分支
    # 正是这个缺口：会话缺失时 merge 拿不到 batch_view）。本轮增量体先算出来，
    # 首轮 merge 再拼成 base + 增量。
    if stateless or first_round:
        # CC 无会话 / 换会话首轮：头里的“会话里”说法不成立，改说契约在上文 base 段、增量在下文。
        head = ("## 本轮输入（act=%s / round=%s；契约与任务见上文，"
                "本轮增量见下文）" % (act, parent.get("round")))
    else:
        head = "## 本轮输入（act=%s / round=%s；L0 契约与历史在你的会话里，不重复注入）" % (
            act, parent.get("round"))
    tail = "只输出一个 JSON 对象（act=%s 的契约形状），不要额外解释。" % act
    if act == "merge":
        body = batch_view(parent, parent.get("_tree") or {})
        body += _history_block(parent)
        _tree = parent.get("_tree") or {}
        _notes = []
        for _mid in sorted(node_mod.batch_members(parent, _tree)):
            if ".fix." in _mid:
                _m = _tree.get(_mid) or {}
                if node_mod.is_terminal(_m) and str(_m.get("reply") or "").strip():
                    _notes.append("[修复节点结论] %s: %s" % (_mid, _m.get("reply")))
                elif node_mod.is_terminal(_m):
                    # 修复节点失败: 父只能消费完成兄弟结果, 该字段必须留在 uncovered,
                    # 不许凭猜测填值, 也不许假装该字段已解决。
                    _notes.append("[修复节点失败] %s 未给出结论, 其对应缺失字段仍未知, "
                                  "必须留在 uncovered, 不许猜填" % _mid)
        if _notes:
            body += "\n" + "\n".join(_notes)
        # 证据台账（机械事实）+ 恢复提示（此前被拒必须换策略）。
        try:
            from harness import contract as _ct
            _rd = parent.get("_run_dir") or ""
            if _rd:
                _led = _ct.read_ledger(_rd)
                _ls = []
                for _mid in sorted(node_mod.batch_members(parent, _tree)):
                    _e = _led.get(_mid)
                    if _e:
                        _ls.append("- 台账 %s：结果类型=%s，工具成功 %d 条，partial=%s，未决 %d 条" % (
                            _mid, _e.get("result_type"), (_e.get("tools") or {}).get("successes", 0),
                            (_e.get("session_path") or "").replace(_rd + "/", ""),
                            len(_e.get("unresolved") or [])))
                if _ls:
                    body += "\n## 证据台账（机械事实，非模型自述）\n" + "\n".join(_ls)
        except Exception:                                        # noqa: BLE001
            pass
        _rn = parent.get("_merge_reject_notes") or []
        if _rn:
            body += ("\n【恢复提示】此前 merge 被拒 %d 次，最近原因：%s。本次必须改变策略"
                     "（不同去向/要点/uncovered），不得逐字重复上次被拒输出。" % (len(_rn), _rn[-1][:200]))
        # V2.1 P2: phantom citation strategy switch + premise falsification replan.
        try:
            _ph = []
            for _mid in sorted(node_mod.batch_members(parent, _tree)):
                if str((_tree.get(_mid) or {}).get("failure_class") or "") == "phantom_citation":
                    _ph.append(_mid)
            if _ph:
                body += (chr(10) + "子节点 %s 因幻影引用失败（0 工具调用却引用不存在文件）。"
                    "再派该章节必须换任务方案：给 1-2 条具体只读命令、要求逐字粘贴工具输出；"
                    "禁止原任务重派（驱动器会机械拒收相同任务）。若已换过 1 次仍同错，"
                    "记 uncovered 收束，不再重派。" % "、".join(_ph))
            _rp = parent.get("_replan_request") or {}
            if _rp.get("premise_id"):
                body += (chr(10) + "前提 %s 已被证伪（证据：%s），受影响节点：%s。"
                    "本次 merge 必须用 再派/切分 表达新计划，替换受影响子树；"
                    "被证伪的结论已标 suspect/invalidated，不得再作为确证引用。"
                    % (_rp.get("premise_id"), str(_rp.get("evidence") or "")[:200],
                    "、".join(_rp.get("affected") or [])))
        except Exception:
            pass
    elif act == "spawn":
        # 移植自生产 (2026-10-15 修)：首轮 spawn 时还没有 merge，直接取 merge 会喂全 null
        # 字段（实测 135 字空 prompt），与「spawn = 照 plan/merge 已想好的要点只做书写」矛盾。
        # 现在：没有 merge ⇒ 回落到 plan 轮存下的 result。
        mg = parent.get("merge") or {}
        yaodian = mg.get("下一批要点")
        if yaodian is None:
            raw = parent.get("result")
            try:
                yaodian = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                yaodian = raw or None
        body = json.dumps({
            "要点": yaodian,
            "切分目标": mg.get("切分目标"),
            "上一轮去向": mg.get("去向"),
        }, ensure_ascii=False, indent=1)
        # V1（路线 B 缺口 1）：重派时给根看全树历史（失败叶详情 + 从未派生的模块），
        # 否则要点退化成“证据台账 unresolved”一句空话时 spawn 无从下手（V0 第 4 轮）。
        body += _history_block(parent)
        body += ("\n【重派规则】同 role 已有 2 个及以上失败叶时，驱动器会拒收同 role 子节点"
                 "（须换 role 或记未决）；失败叶重派必须换写法（直接用工具读规约、直接写文件），"
                 "不许原样重派；每个 child 必须带非空 context、judge、验收标准、auth，缺一即整轮拒收。"
                 "例外（V2 修复叶）：overrides.repair_of 指向上轮失败叶、且 context 点名磁盘上"
                 "已存在的文件时，同类预算豁免（同一失败叶最多修 2 次），可接着失败处继续干。")
        body += ("\n【child context 硬约束】每个 children[i].context ≤ 4000 字；"
                 "只写任务、目标文件路径、验收标准；禁止把文件正文/长报告粘贴进 context——"
                 "执行叶有 read 工具，自己读文件。超长会被 schema 拒收。")
        # V2.1 P1（E16 的一半）：spawn prompt 带 plan 共同约束——要点条数即子节点数上限，
        # 不许超出要点范围另开新题（干跑 Round 1 实测：模型无视“只派一个”派了 6 个）。
        if isinstance(yaodian, list) and yaodian:
            body += ("\nplan 要点共 %d 条；每个要点最多派一个子节点（本轮最多 %d 个），"
                     "不得超出要点范围另开新题。" % (len(yaodian), len(yaodian)))
    else:
        body = json.dumps({"要点": parent.get("result") or ""}, ensure_ascii=False, indent=1)
    # 移植自生产 (R2)：非首轮 spawn 也必须带合法 role 取值域。
    # 现场失败（prompt_chars=135）就是非首轮；只在首轮注入 = 治不到病。
    # role_set_note 本身在候选 harness/submit.py 已存在且与生产同款，此处只补调用点。
    role_note = submit.role_set_note() if act == "spawn" else ""
    parts = [head, body]
    if role_note:
        parts.append(role_note)
    # spawn 契约形状示例：children[i].role 只能从合法 role 里选，附最小示例防自造。
    if act == "spawn":
        parts.append(
            "输出形状示例（只示范结构，不规定内容）："
            '{"children": [{"role": "调研", "context": "…", "judge": "…", '
            '"验收标准": "…", "auth": "…"}]}'
            "—— role 必须是上面合法 role 取值里的一个，不许自造（如 spawn/merge 不是 role）。"
        )
    parts.append("每个 child 必须带非空 context（可执行目标）与 验收标准（可机械核验的判据）"
                 "或 judge，否则计划校验会拒绝该子节点（砍空叶）。")
    # V2.1 P1（E16 拒因回馈，t-f6f9d7325d93 §6）：把上一轮被拒原因喂回模型，
    # 不许盲重试同一输出；成功轮由 step() 清空 _last_reject。
    _lr = parent.get("_last_reject") or {}
    reject_note = ""
    if _lr.get("detail"):
        reject_note = ("【上一轮被拒】act=%s：%s。本次必须先修正该问题再重出，"
                       "不得逐字重复被拒的输出。"
                       % (_lr.get("act"), str(_lr.get("detail"))[:400]))
        parts.append(reject_note)
    parts.append(tail)
    if first_round:
        # 首轮：base（契约+任务）+ 本轮增量。merge 首轮（换会话/会话缺失）必须带增量；
        # plan/spawn 首轮返 base；若这是被拒后的首轮重试，也带上具体拒因。
        base = submit.assemble_prompt(parent, knobs, act,
                                      include_role_prompt=not stateless)
        if act == "merge":
            return base + "\n" + "\n".join(parts)
        if reject_note:
            return base + "\n" + reject_note
        return base
    if stateless:
        # CC 无会话：非首轮也必须把 base（契约+任务）带上，否则模型只有增量、没有契约。
        base = submit.assemble_prompt(parent, knobs, act, include_role_prompt=False)
        return base + "\n" + "\n".join(parts)
    return "\n".join(parts)


def build_repair_context(failed_node: dict, run_dir: str, root_hint: str = "") -> str:
    """Harness 模板生成修复叶 context（t-dc62df942275 项 3）。

    V2-1d c4.1 的教训：根自由撰写的修复叶 context 让叶三次跑错测试路径
    （`cd <run_dir> && python3 tests/test_acceptance.py`——run 目录下根本没有
    tests/）。455d626 只修了叶 prompt 的 run-dir 锚点，修复叶 context 仍由根
    自由撰写。本函数把路径/失败清单改由 Harness 按失败叶的机械记录生成，
    根的 context 只作“修什么”补充（root_hint），附在模板末尾。
    模板内容：失败叶 id + 失败测试名（verify items）+ 工作区/project 绝对路径
    + 具体要修的文件（失败叶已登记产物 → 绝对路径）+ 复测命令原文。
    @returns: 模板文本；失败叶为空时返回 ""（调用方保留原 context，不编造）。
    """
    failed = failed_node or {}
    if not failed:
        return ""
    ver = failed.get("verify") or {}
    if not isinstance(ver, dict):
        ver = {}
    items = ver.get("items") or []
    failed_tests = [str(it.get("name") or "") for it in items
                    if isinstance(it, dict) and not it.get("ok")
                    and str(it.get("name") or "").strip()]
    arts = failed.get("artifacts") or []
    # 第三轮 P1④：任务文本里的 run 目录用真实路径（realpath）；只改文本写法，
    # 不改调用方存的 run_dir。
    rd = os.path.realpath(str(run_dir or "")) if run_dir else ""
    proj_abs = os.path.join(rd, "project") if rd else ""
    lines = [
        "【修复任务（Harness 模板生成，根只填“修什么”）】",
        "失败叶：%s（role=%s，state=%s，failure_class=%s）" % (
            failed.get("id"), failed.get("role"), failed.get("state"),
            failed.get("failure_class") or "（未分类）"),
    ]
    if failed_tests:
        lines.append("失败测试：%s" % "、".join(failed_tests))
    elif ver:
        lines.append("失败测试：verify 记录 %s/%s 通过，未逐项列出失败名（见复测命令输出）"
                     % (ver.get("passed"), ver.get("total")))
    else:
        lines.append("失败测试：（该失败叶无 verify 记录；以根补充的失败现象为准）")
    lines.append("工作区绝对路径（run 目录）：%s" % rd)
    lines.append("项目目录绝对路径：%s" % proj_abs)
    if arts:
        lines.append("要修的文件（失败叶已登记产物，均已落盘，先 Read 再 Edit）：")
        for a in arts[:8]:
            rel = str((a or {}).get("path") or "") if isinstance(a, dict) else ""
            if rel:
                lines.append("- %s" % os.path.join(proj_abs, rel))
    else:
        lines.append("要修的文件：（失败叶未登记产物；以根补充为准，先 Read 磁盘实况再动手）")
    if str(ver.get("command") or "").strip():
        lines.append("复测命令（原文照抄，绝对路径，不许改写成相对路径）：%s"
                     % str(ver["command"]).strip())
        lines.append("禁止：不要 cd 到 run 目录后跑 `python3 tests/test_acceptance.py`——"
                     "测试文件不在 run 目录下，该相对路径不存在；复测只用上面给出的命令原文。")
    lines.append("先 Read 上面列出的文件定位失败点，再 Edit 修复；不要凭空编新路径。")
    if str(root_hint or "").strip():
        lines.append("根补充（修什么）：%s" % str(root_hint).strip())
    return "\n".join(lines)


def _existing_artifact_paths(ctx: str, run_dir: str) -> list:
    """从 context 文本里抽出已存在于 run_dir/project 下的 .py 文件名（V2 修复叶豁免用）。
    只认 run_dir/project 下的 .py（防路径穿越：..、绝对路径、非 .py 一律不要）。"""
    import os as _os
    import re as _re
    out = []
    base = _os.path.join(str(run_dir or ""), "project")
    for _tok in _re.findall(r"[A-Za-z0-9_.]+", str(ctx or "")):
        if not _tok.endswith(".py"):
            continue
        if "/" in _tok or _tok.startswith("."):
            continue
        _p = _os.path.join(base, _tok)
        if _os.path.isfile(_p) and _tok not in out:
            out.append(_tok)
    return out


def _norm_context(text: str) -> str:
    """任务 context 归一化（去全部空白 + 小写，前 500 字）：同错重派守卫用。"""
    import re as _re
    s = _re.sub(r"\s+", "", str(text or "")).lower()
    return s[:500]


# ---------------------------------------------------------------- 驱动器
def _plan_points(parent: dict) -> list:
    """取父节点的 plan 要点（list[str]）。来源：merge["下一批要点"]，无则 plan result 的 "要点"。
    形态防御：要点可能是 str 列表，也可能是 dict 列表（含 要点/title/text 键），或 plan result 本身就是列表。"""
    pts = None
    mg = (parent or {}).get("merge") or {}
    if isinstance(mg, dict):
        pts = mg.get("下一批要点")
    if pts is None:
        raw = (parent or {}).get("result")
        parsed = None
        try:
            parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            pts = parsed.get("要点")
        elif isinstance(parsed, list):
            pts = parsed
    out = []
    if isinstance(pts, list):
        for x in pts:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                s = str(x.get("要点") or x.get("title") or x.get("text") or "").strip()
                if s:
                    out.append(s)
    return out


def _investigate_role(table: dict) -> str:
    """从 role 表派生“调查 role”：有工具（能取证）且 may_write=false（不产 doc）。

    V2.2：恢复策略里建议的换 role 不许写死 role 名（multiround no_literals_in_driver
    扫 driver.py 的 AST，role 名字面量即失败）。当前表里唯一满足的是 调研；表改了，
    跟随表走。查不到 ⇒ 返回 ""（调用方不得再写死一个默认 role 名）。
    """
    for _rn, _row in (((table or {}).get("roles") or {}).items()):
        if not _row.get("may_write") and str(_row.get("tools") or ""):
            return str(_rn)
    return ""


class Driver:
    """一个父节点的一轮循环。

    @param parent 父节点（dict，20 项字段）
    @param tree   树（{node_id: node}），驱动器就地增删节点
    @param act_seq 可选：脚本文档化的 act 序列（每项一个 act 名），用来测「连续两轮 spawn 被拒」这种
                   **非法请求**；缺省 None = 按 next_act 自己推。
    """

    def __init__(self, parent: dict, tree: dict = None, table: dict = None, run_id: str = None,
                 run_dir: str = None, act_runner=None, act_seq=None, l0: str = ""):
        self.parent = parent
        self.tree = tree if tree is not None else {}
        self.tree.setdefault(parent["id"], parent)
        self.table = table or role_mod.load_roles()
        self.run_id = run_id or gate.DEFAULT_RUN_ID
        self.run_dir = Path(run_dir or node_mod.default_run_dir(self.run_id))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.sink = str(self.run_dir / "violations.jsonl")
        self.runner, self.runner_kind = resolve_act_runner(act_runner)
        self.act_seq = list(act_seq) if act_seq else None
        self.rounds_log = self.run_dir / "rounds.jsonl"
        self.driver_log = self.run_dir / "driver.log"
        self.l0 = l0 or ""
        self.recovery = None # 由 Scheduler._driver 注入（恢复控制器；无注入时守卫仍执行，只不记台账）
        self._adjudication_issue = None
        self._audit_backfill = []

    # ---- 落盘（受 assert② 管） --------------------------------------------
    def write_doc(self, node_id: str, text: str) -> tuple:
        """该层落盘：先过 assert②（冻结期一律拒），再由 land 落 doc/<node_id>.md。"""
        ok, why = node_mod.check_write(node_id, self.tree, self.sink)
        if not ok:
            return False, why
        return True, land.land_doc(str(self.run_dir / "doc"), node_id, text)


    def write_managed_doc(self, node_id: str, target: str, text: str) -> tuple:
        """Write only the exact run-relative target bound to this repair leaf."""
        node = self.tree.get(str(node_id)) or {}
        binding = node.get("_managed_doc")
        if not isinstance(binding, dict) or binding.get("target") != target:
            return False, "managed document target does not match leaf binding"
        ok, why = node_mod.check_write(node_id, self.tree, self.sink)
        if not ok:
            return False, why
        return True, land.land_run_file(str(self.run_dir), target, text)

    def _log_round(self, row: dict) -> None:
        with open(self.rounds_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _log_forced(self, rnd: int, limit, want: str) -> str:
        line = ("强制收束 round=%s max_rounds=%s（轮次到数 ⇒ 本轮只允许 merge：请求的 act=%s 被改写为 "
                "merge，去向被强制为收束）" % (rnd, limit, want))
        with open(self.driver_log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return line

    # ---- 主循环 ------------------------------------------------------------
    def step(self) -> dict:
        """跑一轮；返回该轮流水行（state=waiting/closed 的行**不算轮次**、不占 round）。"""
        p = self.parent
        if node_mod.is_terminal(p):
            return {"skip": "closed", "node_id": p["id"], "state": p["state"], "round": p["round"]}

        want = self.act_seq.pop(0) if self.act_seq else next_act(p)
        if want is None:
            ok, why = node_mod.change_state(p, "完成", self.sink)
            return {"skip": "next_act=None 已无 act 可跑", "node_id": p["id"], "state": p["state"],
                    "round": p["round"], "note": why}

        knobs = dict(role_mod.effective(p["role"], want if want in ACTS else None, table=self.table))
        limit = knobs.get("max_rounds") or 0
        next_round = int(p.get("round") or 0) + 1

        # 硬闸②（上半段）：轮次到数 ⇒ 本轮只许 merge（模型想 spawn 也改成 merge）
        forced, forced_line = False, ""
        if limit and next_round > int(limit) and want != "merge":
            forced = True
            forced_line = self._log_forced(next_round, limit, want)
            want = "merge"
            knobs = dict(role_mod.effective(p["role"], "merge", table=self.table))

        # 硬闸①：spawn 必配 merge
        ok, why = request_act(p, want, self.sink)
        if not ok:
            return {"act": want, "node_id": p["id"], "round": p["round"], "rejected": why,
                    "rounds_advanced": False}

        # 等子节点：本批次的子还没全终结 ⇒ 不许跑任何 act（merge 也要等）
        if p.get("state") == node_mod.WAIT_CHILDREN:
            if node_mod.batch_members(p, self.tree):
                ready, rwhy = node_mod.ready_to_merge(p, self.tree)
                if not ready:
                    return {"skip": "waiting", "act": want, "node_id": p["id"],
                            "round": p["round"], "note": rwhy, "rounds_advanced": False}
            # 本批次已全终结（或还没拉过子）⇒ 解冻、回「在跑」再跑本轮
            node_mod.change_state(p, "待跑", self.sink)
            node_mod.change_state(p, "在跑", self.sink)

        # 硬闸②（下半段）：到数轮 ⇒ 记可 grep 日志 + 走既有 gate.check_round（只许那一轮）
        if want == "merge" and limit and next_round > int(limit):
            forced = True
            if not forced_line:
                forced_line = self._log_forced(next_round, limit, "merge")
            gate.check_round(next_round, knobs, self.sink, str(p.get("id") or ""))

        # assert③
        adv_ok, _adv_round, adv_why = node_mod.advance_round(p, knobs, self.sink, forced=forced)
        if not adv_ok:
            return {"act": want, "node_id": p["id"], "round": p.get("round"),
                    "rejected": adv_why, "rounds_advanced": False}

        # F4：merge 前排空本批次的审计（叶终结后才审；发现进本轮 merge 的待回应清单）。
        # 排空失败绝不阻塞 merge；每批只审一次（drain_pending 内判重）。
        if want == "merge":
            try:
                audit_mod.drain_pending(self, p)
            except Exception:                                    # noqa: BLE001
                pass
            hard_docs = final_audit.hard_doc_specs(
                self.tree, str(self.run_dir), parent=p)
            if hard_docs:
                p["_audit_cap_blockers"] = [
                    {"id": "[disk-doc:%s]" % str(spec.get("repair_of") or ""),
                     "item": str(spec.get("item") or ""), "级别": "blocker"}
                    for spec in hard_docs]
                p["_audit_cap_reason"] = "合并前硬文档机械核验未达标"
                p["_audit_cap_cid"] = ""
                detail = self._audit_cap_outcome(
                    p, {}, knobs, precomputed_hard_docs=hard_docs)
                now = time.time()
                row = {
                    "act": want, "node_id": str(p["id"]),
                    "round": int(p.get("round") or 0),
                    "start": round(now, 3), "end": round(now, 3),
                    "seconds": 0.0, "rounds_advanced": True,
                    "runner": "mechanical", "prompt_chars": 0,
                    "session_id": "", "chars": 0, "schema_ok": None,
                    "hard_doc_preempt": [str(spec.get("target") or "")
                                         for spec in hard_docs],
                    "why": detail,
                }
                self._log_round(row)
                return row

        first_round = (next_round == 1)
        p["_tree"] = self.tree
        p["_run_dir"] = str(self.run_dir)
        from harness import session as session_mod
        sid = session_mod.session_id(self.run_id, p["id"])
        stateless = bool(getattr(self.runner, "is_cc", False))
        # 第二轮 D：merge 会话轮换（集成验收 t-132692485362）。
        # 实测（integ-q01）：同一 root merge 会话内 prompt 估算逐轮 +~10K，
        # 26,079 → 36,278 → 46,668 → 57,057，终致 2 个任务单会话超 32K
        # （实际 prompt+completion 44,237 / 36,236）。根因是 pi 外置会话把每轮
        # prompt+输出全量累积进历史。merge 的全部必需上下文都是机械重建的
        # （batch_view、_history_block、证据台账、拒因提示），不依赖会话历史 ⇒
        # 每 MERGE_SESSION_ROUNDS 轮 merge 换一个会话（新 sid ⇒ fresh_session ⇒
        # 首轮全量注入，见 build_prompt），单会话历史封顶约 2 轮。叶/plan/spawn
        # 会话不动；CC stateless 路径本来就无会话，不轮换。
        if want == "merge" and not stateless:
            _mrounds = int(p.get("_merge_rounds") or 0) + 1
            p["_merge_rounds"] = _mrounds
            _epoch = (_mrounds - 1) // MERGE_SESSION_ROUNDS
            if _epoch:
                sid = "%s-m%d" % (sid, _epoch)
        # ★ 首轮注入 L0 的口径（task-def §2.3.5「首轮注入一次」）：
        #   「首轮」= 这个节点的**会话文件还不存在**那一轮（round==1，或重启/中途起的新会话）。
        #   否则会话里根本没有 L0 与历史 ⇒ 模型拿不到契约（实测：真跑一轮 merge 因缺契约而输出非契约 JSON）。
        fresh_session = not session_mod.find_session_file(
            sid, os.environ.get("TREE_PI_AGENT_SESSION_DIR"))
        # CC 无会话文件：fresh_session 恒 True，不能用它当“首轮”判据（否则 merge 永远
        # 拿不到 batch_view、spawn 拿不到 plan 要点）。stateless 时首轮 = next_round==1。
        prompt = build_prompt(p, want, knobs, self.table,
                              first_round if stateless else (first_round or fresh_session),
                              stateless=stateless)
        # ── ③ tree-04 插入点：merge 轮的「待回应清单 + blocker 段」注入（§2.4 ③）──
        #   knobs 是 step 的局部 dict，schema.validate(want, obj, knobs) 在它之后调用 ⇒ 注入生效。
        if want == "merge":
            prompt += final_audit.prepare_merge(p, knobs, self)
            # V2.1 P1：记下本轮是否有审计发现——有则终审重试流程说了算，
            # 机械 merge 不插手（final_audit_retry_capped 用例：3 次作废后修复派发/明确失败，第二轮 B）。
            p["_merge_audit_active"] = bool((knobs or {}).get(schema.AUDIT_KEY))
        t0 = time.time()
        err = ""
        # P1（V2.1）：merge 模型重复输出 2 次 ⇒ 本轮不调模型，改走机械 merge。
        # 审计在场时除外：终审重试流程说了算（final_audit_retry_capped），
        # 机械 merge 不抢第三次作废后的修复派发/明确失败。
        _mech_obj, _mech_fail = None, ""
        if want == "merge" and p.get("_merge_mechanical_next"):
            if p.get("_merge_audit_active"):
                p.pop("_merge_mechanical_next", None)
            else:
                _mech_obj, _mech_fail = self._mechanical_merge(p, knobs)
                p.pop("_merge_mechanical_next", None)
        try:
            if _mech_obj is not None:
                raw = json.dumps(_mech_obj, ensure_ascii=False)
            elif _mech_fail:
                raise RuntimeError("merge 机械兜底失败：" + _mech_fail)
            else:
                raw = self.runner(p, want, knobs, prompt, sid, str(self.run_dir))
        except Exception as exc:                            # noqa: BLE001
            raw, err = "", "%s: %s" % (type(exc).__name__, exc)
        t1 = time.time()

        row = {"act": want, "node_id": p["id"], "round": int(p["round"]),
               "start": round(t0, 3), "end": round(t1, 3), "seconds": round(t1 - t0, 3),
               "rounds_advanced": True,
               "runner": ("mechanical" if (_mech_obj is not None or _mech_fail)
                          else self.runner_kind),
               "prompt_chars": len(prompt), "session_id": sid}
        if err:
            row.update({"chars": 0, "schema_ok": False, "why": "act_runner 失败：" + err,
                        "forced": forced_line})
            p["act"], p["state"] = want, "失败"
            self._reject(p, row, "act_runner 失败（环境侧）", kind=None)
            return row

        try:
            obj = parse_act_output(raw, want)
        except ValueError as exc:
            # 2026-11-13：spawn 被截断/不可解析，但 merge 已点名可写目标 ⇒ 机械派修复叶。
            if want == "spawn":
                cid = self._mechanical_repair_from_merge(p, raw)
                if cid:
                    row.update({"chars": len(raw), "schema_ok": False,
                                "why": "spawn JSON 不可解析 ⇒ 机械派发修复叶 %s" % cid,
                                "rounds_advanced": True, "runner": "mechanical",
                                "forced": forced_line})
                    self._log_round(row)
                    return row
            row.update({"chars": len(raw), "schema_ok": False, "why": str(exc),
                        "forced": forced_line})
            self._reject(p, row, "act=%s 的产出里没有可取用的 JSON：%s" % (want, exc))
            return row

        # merge 指纹（t-64e27d836932）：同一 merge 输出重复被拒 ⇒ 下次必须换策略。
        if want == "merge":
            import hashlib as _hl
            _fp_src = json.dumps({k: obj.get(k) for k in ("去向", "下一批要点", "切分目标", "uncovered")},
                                 ensure_ascii=False, sort_keys=True)
            row["merge_fingerprint"] = _hl.sha256(_fp_src.encode("utf-8")).hexdigest()[:12]
            row["merge_repeat"] = row["merge_fingerprint"] in list(p.get("_merge_fp") or [])
        # T-FIX-TREE 续修：模型产出的未决 type 机械归一（"未决"→"判不了"等），再进校验。
        # 实测：merge 轮 "未决[2].type"="未决" 被形状拒收，整轮作废。
        if isinstance(obj.get("未决"), list):
            _und, _unote = schema.normalize_model_undecided(obj["未决"])
            if _unote:
                obj["未决"] = _und
                row["undecided_normalized"] = _unote
        # T-FIX-TREE 集成验收：merge 轮 uncovered / 对审计发现 机械归一（与未决归一同一模式），再进校验。
        # 无映射/无法归一 ⇒ 显式失败（不静默丢弃、不编造处置）。Run D 实测形状拒收的直接原因。
        if want == "merge" and "uncovered" in obj:
            _unc, _unote = schema.normalize_model_uncovered(obj["uncovered"])
            if _unc is None:
                self._reject(p, row, "merge 轮 uncovered 无法归一：%s" % _unote,
                             extra={"chars": len(raw), "schema_ok": False, "why": _unote,
                                    "forced": forced_line})
                return row
            if _unote:
                obj["uncovered"] = _unc
                row["uncovered_normalized"] = _unote
        if want == "merge" and schema.AUDIT_KEY in obj:
            _aud, _anote = schema.normalize_model_audit_replies(obj[schema.AUDIT_KEY])
            if _aud is None:
                self._reject(p, row, "merge 轮 对审计发现 无法归一：%s" % _anote,
                             extra={"chars": len(raw), "schema_ok": False, "why": _anote,
                                    "forced": forced_line})
                return row
            if _anote:
                obj[schema.AUDIT_KEY] = _aud
                row["audit_replies_normalized"] = _anote
        _clean, perm_why = schema.check_permission(want, obj, knobs, self.sink)
        # 机械补全（V2.1 P1，t-d301b69799d2 的 uncovered 兜底推广到多字段）：
        # 缺的确定性字段只从可验证来源补（fix.* 结构化结论 / 证据台账未决），
        # 补不上 ⇒ 走正常校验（拒绝），绝不默认填 []、不编造语义。
        if want == "merge":
            _rep = self._repair_merge_fields(p, obj)
            if _rep:
                row["merge_repaired"] = _rep
                if "uncovered" in _rep:
                    row["uncovered_source"] = _rep["uncovered"]
                    row["uncovered_fallback"] = True
        # V2.1 P2 hard/soft fields (after mechanical repair: what is still missing is a real hard error).
        try:
            from harness import fields as _fields_mod
            _hard, _filled = _fields_mod.classify_and_fill(want, obj, knobs)
        except Exception:
            _hard, _filled = [], []
        if _filled:
            row["soft_defaults"] = _filled
        if _hard:
            row["error_code"] = _hard[0]["code"]
            self._reject(p, row, "硬字段错误：%s" % "；".join(e["message"] for e in _hard),
                extra={"chars": len(raw), "schema_ok": False, "forced": forced_line})
            return row

        ok_schema, schema_why = schema.validate(want, obj, knobs)

        # 硬闸②（收官）：到数轮的产出**必须**被改写成收束（task-def §2.5.2「被改写成收束」），
        #   改写本身记一笔（响亮，不静默覆盖模型的去向）。
        if forced and ok_schema and obj.get("去向") != "收束":
            gate.log_violation(self.sink, kind="驱动_产出形状", node_id=str(p["id"]),
                               detail=("轮次到数（round=%s > max_rounds=%s）：产出去向=%r ⇒ 驱动器"
                                       "改写成收束（76 §3.2「强制以 merge 收束」/ §4.5.5）"
                                       % (p["round"], limit, obj.get("去向"))))
            obj["去向"] = "收束"
            obj.setdefault("uncovered", [{
                "what": "轮次到数（round=%s > max_rounds=%s）仍未覆盖的部分" % (p["round"], limit),
                "why": "驱动器强制收束，不许再派（未决由父上冒）"}])
            ok_schema, schema_why = schema.validate(want, obj, knobs)
            row["rewritten_to"] = "收束"
            row["from_去向"] = "再派/切分"

        # 收束覆盖检查（t-64e27d836932）：partial/failed 分支必须进 uncovered，否则拒收。
        if want == "merge" and ok_schema and obj.get("去向") == "收束":
            _cov_ok, _cov_why = self._check_partial_coverage(p, obj)
            if not _cov_ok:
                return self._reject(p, row, _cov_why, kind="驱动_部分证据未覆盖",
                                    extra={"chars": len(raw), "schema_ok": True,
                                           "去向": obj.get("去向"), "forced": forced_line})
        if not ok_schema:
            self._reject(p, row, "act=%s 的产出不契约：%s" % (want, schema_why),
                         extra={"chars": len(raw), "schema_ok": False, "why": schema_why,
                                "perm": perm_why, "forced": forced_line})
            return row

        # ── ④a tree-04 插入点（G1 修正）：「对审计发现[] 的形状」自检 ──
        #   ★ 注入键是契约装载键 `对审计发现`（§3.6 更正）⇒ `schema.py:148` 的审计分支**不再触发**
        #     ⇒ `item/处置/说明` 必填 + `处置/建议` 枚举**没有任何执行者**（owner 实测证实）。
        #     `schema.py` 属禁改文件 ⇒ 本批在新代码里把这段形状补回来（不另加插入点，挂在 ④ 同处）。
        if want == "merge" and ok_schema:
            shape_bad = final_audit.reply_shape_problems(obj)
            if shape_bad:
                return self._reject(p, row, "对审计发现 的形状不合规：%s" % "；".join(shape_bad),
                                    kind="驱动_审计未回应", extra={"schema_ok": False})
        # ── ④b tree-04 插入点：「id 串逐条出现」校验（§2.4 ④ / §3.6）──
        #   ★ 只查 id 串（机械可检），不查模型对 `item` 正文的措辞（措辞是语义，机械检不了）。
        if want == "merge" and ok_schema:
            miss = final_audit.missing_replies(obj, p, self)
            if miss:
                return self._reject(p, row, "审计发现未逐条回应：缺 %s（id 串必须逐字出现在 "
                                     "对审计发现[].item 里）" % miss, kind="驱动_审计未回应")

        # 拉孙的落点必须是【树里已有的子】：切分目标不存在 ⇒ 该轮拒（不许凭空造一个节点）
        if want == "merge" and obj.get("去向") == "切分":
            miss = [str(t) for t in (obj.get("切分目标") or []) if str(t) not in self.tree]
            if miss:
                self._reject(p, row, "切分目标不在树里：%s（拉孙必须挂在已有的子上）" % miss,
                             extra={"chars": len(raw), "schema_ok": True, "去向": obj.get("去向"),
                                    "forced": forced_line})
                return row

        row.update({"chars": len(raw), "schema_ok": True,
                    "why": "", "perm": perm_why, "forced": forced_line,
                    "去向": obj.get("去向") if want == "merge" else None})
        # ── ⑤ tree-04 插入点（step 尾部）：apply_output 的返回值 = 「待拒原因」或 None（§2.4 ⑤）──
        self._adjudication_issue = None
        self._audit_backfill = []
        detail = self.apply_output(p, want, obj, knobs)       # 既有调用，只多接返回值
        if self._adjudication_issue:
            row["adjudication_issue"] = self._adjudication_issue
        if self._audit_backfill:
            row["audit_backfill"] = list(self._audit_backfill)
        if detail:
            # 第二轮 B 的哨兵：修复派发 / 明确失败都不是"该轮不算数"——本轮已算数
            # （终审跑了、修复叶派了 / 父已进失败终态），不许走 _reject 回退 round。
            # 第四轮决策 C：复核派发同为哨兵（终审跑了、复核叶派了，本轮已算数）。
            if (detail.startswith("【修复派发】") or detail.startswith("【明确失败】")
                    or detail.startswith("【复核派发】") or detail.startswith("【裁决派发】")
                    or detail.startswith("【审计补派】")):
                row.update({"chars": len(raw), "schema_ok": True, "why": detail,
                            "去向": obj.get("去向") if want == "merge" else None})
                self._log_round(row)
                return row
            return self._reject(p, row, detail, kind="驱动_收束作废")   # ← 唯一写流水处
        # 成功轮清空拒因回馈（E16）：下一轮不再重复注入旧拒因。
        p.pop("_last_reject", None)
        self._log_round(row)
        return row

    def _uncovered_missing_coverage(self, parent: dict, obj: dict) -> list:
        """收束时未被 uncovered 覆盖的 partial/failed 分支 [(node_id, result_type)]。

        覆盖判定与 _check_partial_coverage 同一口径（节点 id 进 what/why，或未决
        what 前 15 字进 what/why）。只读台账 + 树状态，不改 obj，供
        _repair_merge_fields（机械补齐）与 _check_partial_coverage（终检）共用。
        """
        from harness import contract as _ct
        led = _ct.read_ledger(str(self.run_dir))
        missing = []
        for nid in sorted(node_mod.batch_members(parent, self.tree)):
            e = led.get(nid)
            if not e or e.get("result_type") not in ("partial", "failed"):
                continue
            unc = obj.get("uncovered") or []
            covered = False
            for u in unc:
                if not isinstance(u, dict):
                    continue
                hay = str(u.get("what") or "") + " " + str(u.get("why") or "")
                if nid in hay:
                    covered = True
                    break
                for ob in (e.get("unresolved") or []):
                    w = str((ob or {}).get("what") or "")
                    if w and w[:15] in hay:
                        covered = True
                        break
                if covered:
                    break
            if not covered:
                missing.append((nid, e.get("result_type")))
        return missing

    def _check_partial_coverage(self, parent: dict, obj: dict) -> tuple:
        """收束时 partial/failed 分支必须被 uncovered 覆盖（t-64e27d836932）。
        覆盖判定见 _uncovered_missing_coverage。@returns: (ok, why)。"""
        bad = self._uncovered_missing_coverage(parent, obj)
        if bad:
            return False, "收束时部分/失败分支未覆盖：%s（须进 uncovered，注明节点 id 或未决要点）" % "；".join(
                "%s（%s）未被 uncovered 覆盖" % (nid, rt) for nid, rt in bad)
        return True, ""

    def _repair_uncovered_fallback(self, parent: dict):
        """merge 缺 uncovered 时的机械兜底（t-d301b69799d2）。

        仅当同一父的成功修复子节点提供了 schema-valid 的 uncovered 数组时返回它；
        否则返回 None（调用方走正常拒绝流程，绝不默认填 []）。

        规则：
        - 修复子节点必须已终结且 state == "完成"
        - 必须有 repair_uncovered 字段且为 [{what,why}] 列表
        - 多个修复子节点值不一致 → 冲突，返回 None
        - 父已有显式 uncovered 时本函数不被调用（只在缺失时调用），故不覆盖
        """
        pid = str(parent.get("id") or "")
        prefix = pid + ".fix."
        candidates = []
        for nid, node in (self.tree or {}).items():
            if not str(nid).startswith(prefix):
                continue
            if not node_mod.is_terminal(node):
                continue
            if node.get("state") != "完成":
                continue
            val = node.get("repair_uncovered")
            if val is None:
                continue
            if not isinstance(val, list):
                continue
            if not all(isinstance(u, dict) and str(u.get("what") or "").strip()
                       and str(u.get("why") or "").strip() for u in val):
                continue
            candidates.append((str(nid), val))
        if not candidates:
            return None
        # 多个修复子节点值不一致 → 冲突，不填
        first_val = candidates[0][1]
        for _nid, val in candidates[1:]:
            if val != first_val:
                return None
        return {"value": first_val, "source": candidates[0][0]}

    def _repair_merge_fields(self, parent: dict, obj: dict) -> dict:
        """V2.1 P1：merge 缺确定性字段时的机械补全（只补可验证来源，绝不编造语义）。

        - uncovered（去向=收束）：沿用 _repair_uncovered_fallback（fix.* 结构化结论）。
        - 下一批要点（去向=再派/切分）：取本批未决（证据台账 unresolved，逐条带节点 id），上限 5 条。
        - 切分目标：无法机械推导（必须是已存在节点 id），不补。
        @returns: {field: source} 已补的字段（空 dict = 无可补）。
        """
        repaired = {}
        if obj.get("去向") == "收束" and "uncovered" not in obj:
            _fb = self._repair_uncovered_fallback(parent)
            if _fb is not None:
                obj["uncovered"] = _fb["value"]
                repaired["uncovered"] = _fb["source"]
        # 集成验收补丁 2（integ-q01 实测）：uncovered 已在但未覆盖失败分支时，
        # 机械补记（与 _mechanical_merge 的补齐同口径、同文案）。只用已验证事实：
        # 台账 result_type + 节点 id；不编造处置、不碰审计回应。补后仍过同一终检。
        if obj.get("去向") == "收束" and isinstance(obj.get("uncovered"), list):
            _missing = self._uncovered_missing_coverage(parent, obj)
            for _nid, _rt in _missing:
                obj["uncovered"].append(
                    {"what": "%s（%s）未被覆盖" % (_nid, _rt or "失败"),
                     "why": "机械收束补记：该分支未完成，见未决"})
            if _missing:
                repaired["uncovered_coverage_topup"] = "机械补记 %d 个未覆盖分支：%s" % (
                    len(_missing), "、".join(_nid for _nid, _ in _missing))
        if obj.get("去向") in ("再派", "切分") and not obj.get("下一批要点"):
            from harness import contract as _ct
            led = _ct.read_ledger(str(self.run_dir))
            pts = []
            for nid in sorted(node_mod.batch_members(parent, self.tree)):
                e = led.get(nid) or {}
                for ob in (e.get("unresolved") or []):
                    if isinstance(ob, dict) and str(ob.get("what") or "").strip():
                        pts.append("%s：%s" % (nid, ob.get("what")))
                    elif isinstance(ob, str) and str(ob).strip():
                        pts.append("%s：%s" % (nid, ob))
                if len(pts) >= 5:
                    break
            if pts:
                obj["下一批要点"] = pts[:5]
                repaired["下一批要点"] = "证据台账 unresolved（%d 条）" % len(pts)
        return repaired

    def _mechanical_merge(self, parent: dict, knobs: dict) -> tuple:
        """V2.1 P1：merge 模型重复输出后的机械 merge（不调模型）。

        只用已验证的机械事实：证据台账（结果类型/未决）、fix.* 结构化结论、
        批次终结状态。@returns: (obj or None, fail_reason)。
        """
        if (knobs or {}).get(schema.AUDIT_KEY):
            return None, "本轮有审计发现，机械 merge 无法逐条回应（不编造处置）"
        from harness import contract as _ct
        led = _ct.read_ledger(str(self.run_dir))
        members = sorted(node_mod.batch_members(parent, self.tree))
        if not members:
            return None, "本批无子节点"
        unresolved, n_complete, n_partial, n_failed = [], 0, 0, 0
        for nid in members:
            e = led.get(nid) or {}
            rt = e.get("result_type")
            if rt == "complete":
                n_complete += 1
            elif rt == "partial":
                n_partial += 1
            else:
                n_failed += 1
            for ob in (e.get("unresolved") or []):
                if isinstance(ob, dict) and str(ob.get("what") or "").strip():
                    unresolved.append({"type": str(ob.get("type") or ""),
                                       "what": "%s：%s" % (nid, ob.get("what")),
                                       "why": str(ob.get("why") or "叶未决")})
                elif isinstance(ob, str) and str(ob).strip():
                    unresolved.append({"type": "",
                                       "what": "%s：%s" % (nid, ob), "why": "叶未决"})
        # V2.2 自愈：机械产物必须 100% 过同一 schema 校验——未决条目补合法 type
        #（台账有 type 则保留，无则归一化为缺信息；"BLOCKER 缺信息"类取子串枚举）。
        # P5 tier1-2 真 bug：兜底自身产出的未决缺 type，RuntimeError 计一次被拒、根落失败。
        unresolved = schema.normalize_undecided(unresolved)
        all_terminal = all(node_mod.is_terminal(self.tree.get(nid) or {}) for nid in members)
        if all_terminal:
            unc = []
            _fb = self._repair_uncovered_fallback(parent)
            if _fb is not None:
                unc = list(_fb["value"])
            else:
                unc = [{"what": u["what"], "why": u["why"]} for u in unresolved[:10]]
            # V2.2 自愈：收束覆盖检查要求每个部分/失败分支都被 uncovered 提到；
            # 机械兜底必须自己补齐，不许留给模型再猜一次（否则覆盖检查拒收、根落失败）。
            # 覆盖口径与 _check_partial_coverage 一致（节点 id 或未决 what 前 15 字），
            # 另加节点 state=失败作兜底（台账缺失时仍覆盖）。
            for nid in members:
                e = led.get(nid) or {}
                _needs = e.get("result_type") in ("partial", "failed") or \
                    str((self.tree.get(nid) or {}).get("state") or "") == "失败"
                if not _needs:
                    continue
                _covered = False
                for u in unc:
                    if not isinstance(u, dict):
                        continue
                    _hay = str(u.get("what") or "") + " " + str(u.get("why") or "")
                    if nid in _hay:
                        _covered = True
                        break
                    for ob in (e.get("unresolved") or []):
                        _w = str((ob or {}).get("what") or "")
                        if _w and _w[:15] in _hay:
                            _covered = True
                            break
                    if _covered:
                        break
                if not _covered:
                    unc.append({"what": "%s（%s）未被覆盖" % (nid, e.get("result_type") or "失败"),
                                "why": "机械收束补记：该分支未完成，见未决"})
            obj = {"去向": "收束",
                   "reply": "【机械生成】merge 模型重复输出，改走机械收束：本批 %d 叶，%d 完成，%d 部分，%d 失败；未决见 uncovered。"
                            % (len(members), n_complete, n_partial, n_failed),
                   "未决": unresolved[:10],
                   "uncovered": unc}
        else:
            pts = ["%s：%s" % (u.get("what"), u.get("why")) for u in unresolved[:5]]
            if not pts:
                return None, "本批未全终结且无未决要点可派"
            obj = {"去向": "再派",
                   "reply": "【机械生成】merge 模型重复输出，改走机械再派：按未决缩小重查。",
                   "未决": unresolved[:10],
                   "下一批要点": pts}
        ok, why = schema.validate("merge", obj, knobs)
        if not ok:
            return None, "机械 merge 不契约：" + why
        return obj, ""

    # ---- 产出落进节点 ------------------------------------------------------
    def _reject(self, p: dict, row: dict, detail: str, kind: str = "驱动_产出形状",
                extra: dict = None) -> dict:
        """该轮不算数（round 回退）+ 记一笔（kind=None ⇒ 不记，环境侧失败）。"""
        if kind:
            gate.log_violation(self.sink, kind=kind, node_id=str(p["id"]),
                               detail=detail + " ⇒ 该轮不算数（round 退回）")
        # V2.1 P1（E16）：所有 act 的拒因都记 _last_reject，供下一轮 prompt 回馈；
        # 成功轮由 step() 清空。
        p["_last_reject"] = {"act": row.get("act"), "detail": str(detail)[:400]}
        if row.get("act") == "merge":
            if row.get("merge_fingerprint"):
                _fps = list(p.get("_merge_fp") or [])
                if row["merge_fingerprint"] in _fps and not p.get("_merge_audit_active"):
                    # 同一 merge 输出第二次被拒 ⇒ 下一轮不调模型，改走机械 merge
                    #（“不允许相同错误反复 3 次烧模型”：模型最多为同一错误调用 2 次）。
                    # 审计在场时除外（终审重试兜底流程说了算）。
                    p["_merge_mechanical_next"] = True
                    gate.log_violation(self.sink, kind="驱动_merge重复输出",
                                       node_id=str(p["id"]),
                                       detail="merge 输出指纹 %s 第二次被拒：%s ⇒ 下一轮改走机械 merge，不再调模型"
                                       % (row["merge_fingerprint"], detail[:200]))
                if row["merge_fingerprint"] not in _fps:
                    _fps.append(row["merge_fingerprint"])
                p["_merge_fp"] = _fps[-10:]
            _notes = list(p.get("_merge_reject_notes") or [])
            _notes.append(detail[:200])
            p["_merge_reject_notes"] = _notes[-5:]
        p["round"] = int(p["round"]) - 1
        row.update({"rounds_advanced": False, "rejected": detail})
        row.update(extra or {})
        self._log_round(row)
        return row

    def apply_output(self, p: dict, act: str, obj: dict, knobs: dict) -> None:
        """把这一轮的产出落到父节点（20 项字段里的 reply / 未决 / merge / result）+ 状态机推进。"""
        p["act"] = act
        if str(obj.get("reply") or "").strip():
            p["reply"] = str(obj["reply"])
        if isinstance(obj.get("未决"), list):
            p["未决"] = _merge_unresolved(p.get("未决") or [], obj["未决"],
                                          str(self.run_dir))
        # 修复叶结构化输出（t-d301b69799d2）：叶若返回 uncovered 结构化数组，存入节点，
        # 供 merge 机械兜底读取（不经过 reply 自然语言）。
        if act == "leaf" and isinstance(obj.get("uncovered"), list):
            p["repair_uncovered"] = obj["uncovered"]
        if obj.get("judge_ok") is not None:
            p["judge_ok"] = bool(obj["judge_ok"])
            p["judge_why"] = str(obj.get("judge_why") or "")
        if act == "plan":
            p["result"] = json.dumps(obj.get("要点"), ensure_ascii=False)
            node_mod.change_state(p, "在跑", self.sink)
            return
        if act == "spawn":
            _spawned = self.spawn_children(p, obj, knobs)
            if not _spawned and (obj.get("children") or []):
                _d = p.get("_spawn_drops") or {}
                _parts = []
                if _d.get("empty"):
                    _parts.append("空叶 %d 条（context/验收标准/judge 缺失）" % _d["empty"])
                if _d.get("same_error"):
                    _parts.append("同错重派被拒 %d 条（context 与失败叶相同，须换任务方案）" % _d["same_error"])
                if _d.get("same_kind"):
                    _parts.append("同类重派被拒 %d 条（role 已连续失败，须换 role、记未决或派修复叶）" % _d["same_kind"])
                if _d.get("nodes_max"):
                    _parts.append("超量 %d 条（节点数已达上限）" % _d["nodes_max"])
                _why = "；".join(_parts) if _parts else "未记录拒因（见 violations.jsonl）"
                return ("spawn 无有效子节点：%d 条 children 全被拒（%s），该轮不算数，父须按拒因重 plan"
                    % (len(obj.get("children") or []), _why))
            p["batch"] = int(p["round"])                 # 父自己在等的那一批 = 本 spawn 轮
            node_mod.change_state(p, "等子节点", self.sink)
            # ── ② tree-04 插入点：落子之后挂审计枝（§2.4 ②）──
            #   内序：抽检触发 → 形状审查触发（audit.on_spawn 里定死）。
            #   审计/检查点节点创建之前也要过 check_nodes_max（在 make_checkpoint 里）。
            audit_mod.on_spawn(self, p)
            return None
        if act == "merge":
            p["merge"] = obj
            go = obj.get("去向")
            if go == "收束":
                # ── ⑤ tree-04 插入点：收束判定的落点（§2.4 ⑤ / §3.8）──
                #   ★ 父不许先置「完成」再回退（完成/失败 是封闭终态，assert①）⇒ 先判定、后置完成。
                ok, why = final_audit.on_converge(self, p, obj)   # 只判定 + 记账，不写流水
                if ok:
                    node_mod.change_state(p, "完成", self.sink)
                    p["_replan_request"] = None  # F5：merge 已消费证伪重规划请求
                    return None                                   # ← 收束成立
                # 第二轮 B：作废已达上限的收束不许再作废重跑——自动有限修复派发 / 明确失败。
                # on_converge 在 p 上留了 _audit_cap_blockers（不再兜底通过）。
                if p.get("_audit_cap_blockers"):
                    return self._audit_cap_outcome(p, obj, knobs)
                # ★ 作废：把这轮 merge 的效果完全撤掉，把父放回「这轮还没发生」的位置。
                p["act"] = "spawn"    # 最后真实发生过的 act（理由见 §2.4 ⑤★★：不显式改回，
                                      #   next_act 会按「上一轮是 merge」去查 merge.去向 ⇒ 算错 act）
                p["merge"] = None     # 本轮那份去向作废（下一轮仍是 merge ⇒ 不看它）
                return "终审出 blocker ⇒ 该轮收束作废：%s" % why  # 交给 step 走既有 _reject
            else:                                        # 再派 / 切分 ⇒ 新一批（下一轮是 spawn）
                p["batch"] = int(p["round"]) + 1
                node_mod.change_state(p, "等子节点", self.sink)
                p["_replan_request"] = None  # F5：merge 已消费证伪重规划请求
                return None

    def _audit_cap_outcome(self, p: dict, obj: dict, knobs: dict,
                           precomputed_hard_docs: list = None) -> str:
        """Resolve cap blockers mechanically, then queue Sage or a bound Leo issue."""
        blockers = list(p.pop("_audit_cap_blockers") or [])
        # 2026-11-13：cap blocker 可能来自不同入口；这里再过一次机械核验/权威改写，
        # 保证进入路由的 blocker 已经绑定真实磁盘事实。
        try:
            blockers, _cap_dropped = final_audit.reverify_blockers(
                blockers, self, str(p.get("id") or ""))
        except Exception:                                        # noqa: BLE001
            pass
        reason = str(p.pop("_audit_cap_reason") or "次数上限")
        cid = str(p.pop("_audit_cap_cid") or "")
        rnd = int(p.get("round") or 0)
        limit = int(knobs.get("max_rounds") or 0)
        repairs = int(p.get("_audit_repair_rounds") or 0)
        at_limit = bool(limit and rnd > limit)
        pid = str(p.get("id") or "")
        hard_checks = (
            final_audit.hard_convergence_failures(self, p, blockers)
            if precomputed_hard_docs is None else
            [{"kind": "disk_doc", "item": str(spec.get("item") or ""),
              "target": str(spec.get("target") or ""),
              "chars_min": spec.get("chars_min")}
             for spec in precomputed_hard_docs]
        )

        if any(row.get("kind") == "structural" for row in hard_checks):
            missing = final_audit.missing_incremental_audits(self)
            if missing:
                issue = final_audit.make_adjudication_issue(
                    p, p.get("merge") or obj, blockers, self.tree, str(self.run_dir),
                    {"reason": reason, "repair_rounds": repairs,
                     "review_rounds": int(p.get("_audit_review_rounds") or 0)},
                    {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX - repairs),
                     "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                         - int(p.get("_audit_review_rounds") or 0))},
                    hard_checks=hard_checks, stage="after_audit")
                issue["audit_backfill"] = missing
                self._set_merge_continuation(p, issue, cid, "after_audit")
                self._audit_backfill = missing
                self._adjudication_issue = issue
                return ("【审计补派】结构 blocker 指向缺失的直接叶审计，"
                        "先补审计再对原 merge 续判：%s" % "、".join(missing))

        # 2026-11-13：先按「为什么不能本地继续」归类，再决定去向。
        # 授权直接找人；预算/方法失效直接失败；事实冲突必须有可写目标；
        # 只有判断题才允许进入复核/Leo。
        reason_rows = _blocker_reasons(blockers, str(self.run_dir))
        reason_set = {str(row.get("reason") or "") for row in reason_rows}
        for row in reason_rows:
            escalation.record(str(self.run_dir), dict(
                row, node_id=pid,
                budget={"repairs_left": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX - repairs),
                        "reviews_left": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                            - int(p.get("_audit_review_rounds") or 0))}))

        if escalation.E_AUTH in reason_set:
            issue = final_audit.make_adjudication_issue(
                p, p.get("merge") or obj, blockers, self.tree, str(self.run_dir),
                {"reason": reason, "repair_rounds": repairs,
                 "review_rounds": int(p.get("_audit_review_rounds") or 0)},
                {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX - repairs),
                 "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                     - int(p.get("_audit_review_rounds") or 0))},
                hard_checks=[], stage="user")
            self._set_merge_continuation(p, issue, cid, "user")
            self._adjudication_issue = issue
            return "【人工】终审 blocker 涉及授权/红线，直接等待用户决定"

        if escalation.E_BUDGET in reason_set or escalation.E_BROKEN in reason_set:
            detail = "；".join(str(row.get("why") or row.get("reason"))
                              for row in reason_rows
                              if row.get("reason") in (escalation.E_BUDGET,
                                                       escalation.E_BROKEN))
            p["reply"] = "终审 blocker 属于预算耗尽/方法失效，不能靠复核或裁决豁免"
            p["未决"] = list(p.get("未决") or []) + [
                {"type": "判不了", "what": "%s %s" % (audit_mod.BLOCKER_WORD, detail),
                 "why": "本地预算/方法已耗尽；按明确失败处理，不进入 Leo"}]
            final_audit.ledger(self, rnd, cid, len(blockers), False, 兜底="预算/方法耗尽")
            node_mod.change_state(p, "失败", self.sink)
            return "【明确失败】预算/方法耗尽，不进入复核/裁决"

        specs = (
            final_audit.repair_specs(
                blockers, self.tree, str(self.run_dir), parent=p)
            if precomputed_hard_docs is None else list(precomputed_hard_docs)
        )
        dispatchable, skipped = [], []
        for spec in specs:
            # 2026-11-12：不再以字数为派发门槛，只要求有可写目标。
            if str(spec.get("target_abs") or ""):
                dispatchable.append(spec)
            else:
                skipped.append(spec)
        if skipped:
            gate.log_violation(
                self.sink, kind="终审_修复规格跳过", node_id=pid,
                detail=("终审 blocker %d 条的修复规格里 %d 个无可执行目标"
                        "（缺目标文件，不派空修复叶）：%s"
                        % (len(blockers), len(skipped),
                           "；".join(str(x.get("item") or "")[:120] for x in skipped))))

        if dispatchable and repairs < final_audit.FINAL_AUDIT_REPAIR_MAX:
            created = self._dispatch_repair_leaves(p, dispatchable, cycle=repairs + 1)
            if created:
                p["_audit_repair_rounds"] = repairs + 1
                gate.log_violation(
                    self.sink, kind="终审_修复派发", node_id=pid,
                    detail=("终审 blocker %d 条未消除（%s）⇒ 自动派发 %d 个修复叶：%s"
                            "（第 %d/%d 轮修复）"
                            % (len(blockers), reason, len(created), "、".join(created),
                               repairs + 1, final_audit.FINAL_AUDIT_REPAIR_MAX)))
                p["act"] = "spawn"
                p["merge"] = None
                p["batch"] = int(p.get("round") or 0)
                node_mod.change_state(p, "等子节点", self.sink)
                if at_limit:
                    p["round"] = int(p.get("round") or 0) - 1
                    with open(self.driver_log, "a", encoding="utf-8") as fh:
                        fh.write("到数修复派发 父=%s 轮号=%s 回退 1（第 %d/%d 轮修复）\n"
                                 % (pid, rnd, repairs + 1,
                                    final_audit.FINAL_AUDIT_REPAIR_MAX))
                return ("【修复派发】终审 blocker %d 条未消除（%s）⇒ 已派发 %d 个修复叶：%s"
                        % (len(blockers), reason, len(created), "、".join(created)))

        # 事实冲突必须有可执行的派生侧修复；没有目标或修复预算耗尽 ⇒ 明确失败。
        # 不能让复核叶/Leo 用语义投票决定 168 还是 186。
        unresolved_fact = []
        for blocker, row in zip(blockers, reason_rows):
            if row.get("reason") != escalation.E_FACT:
                continue
            if (not _blocker_covered_by_specs(blocker, dispatchable)
                    or repairs >= final_audit.FINAL_AUDIT_REPAIR_MAX):
                unresolved_fact.append(blocker)
        if unresolved_fact:
            detail = "；".join(str((b or {}).get("item") or "")[:180]
                              for b in unresolved_fact)
            p["reply"] = "终审事实冲突没有可执行的派生侧修复目标，父明确失败"
            p["未决"] = list(p.get("未决") or []) + [
                {"type": "冲突", "what": "%s %s" % (audit_mod.BLOCKER_WORD, detail),
                 "why": "事实冲突只能重算/回读权威并改派生侧；不允许复核/Leo 投票选边"}]
            final_audit.ledger(self, rnd, cid, len(blockers), False, 兜底="事实冲突无可写目标")
            node_mod.change_state(p, "失败", self.sink)
            return "【明确失败】事实冲突没有可写派生目标，不进入复核/裁决"

        if hard_checks:
            detail = "；".join(str(row.get("detail") or row.get("item") or row.get("kind"))
                              for row in hard_checks)
            p["reply"] = "终审存在未解决的硬性机械缺陷，不能提交语义裁决"
            p["未决"] = list(p.get("未决") or []) + [
                {"type": "超范围", "what": "%s %s" % (audit_mod.BLOCKER_WORD, detail),
                 "why": "当前磁盘/结构检查未通过，修复预算已用尽或无可执行修复"}]
            gate.log_violation(self.sink, kind="终审_blocker_明确失败", node_id=pid,
                               detail="硬性机械检查失败，禁止 Sage/Leo 豁免：%s" % detail)
            final_audit.ledger(self, rnd, cid, len(blockers), False, 兜底="硬检查失败")
            node_mod.change_state(p, "失败", self.sink)
            return "【明确失败】硬性机械检查失败，不允许语义裁决豁免"

        reviewable, unreviewable = [], []
        allow_executable = repairs >= final_audit.FINAL_AUDIT_REPAIR_MAX
        for blocker in blockers:
            ok, why = final_audit.is_reviewable_blocker(
                blocker, self.tree, str(self.run_dir), parent=p,
                allow_executable=allow_executable)
            if ok:
                reviewable.append(blocker)
            else:
                unreviewable.append((blocker, why))
        review_rounds = int(p.get("_audit_review_rounds") or 0)
        if reviewable and not unreviewable and review_rounds < final_audit.FINAL_AUDIT_REVIEW_MAX:
            issue = final_audit.make_adjudication_issue(
                p, p.get("merge") or obj, reviewable, self.tree, str(self.run_dir),
                {"reason": reason, "repair_rounds": repairs,
                 "review_rounds": review_rounds},
                {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX - repairs),
                 "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX - review_rounds)},
                hard_checks=[])
            self._set_merge_continuation(p, issue, cid, "sage")
            self._adjudication_issue = issue
            return ("【裁决派发】本地修复无可执行余量，"
                    "将对原 merge 争议派发一次结构化 Sage 复核")

        if unreviewable:
            details = "；".join("%s（%s）" % (b.get("item") or "", why)
                                for b, why in unreviewable)
            p["reply"] = "终审 blocker 不符合只读 Sage 复核边界，父明确失败"
            p["未决"] = list(p.get("未决") or []) + [
                {"type": "判不了", "what": "%s %s" % (audit_mod.BLOCKER_WORD, details),
                 "why": "未审批的硬/敏感 blocker 不得由无关语义复核或自由文本自动放行"}]
            final_audit.ledger(self, rnd, cid, len(blockers), False, 兜底="复核边界失败")
            node_mod.change_state(p, "失败", self.sink)
            return "【明确失败】blocker 不符合 Sage 只读复核边界"

        # Sage was already used or unavailable; only its normalized verdicts may accompany Leo.
        reviews = self._structured_review_rows(p)
        if not reviews:
            p["reply"] = "终审 blocker 未能获得有效的结构化 Sage 结论，父明确失败"
            p["未决"] = list(p.get("未决") or []) + [
                {"type": "判不了", "what": "%s Sage 复核未完成" % audit_mod.BLOCKER_WORD,
                 "why": "没有绑定 blocker 和输入证据的结构化 Sage verdict"}]
            final_audit.ledger(self, rnd, cid, len(blockers), False, 兜底="Sage 未完成")
            node_mod.change_state(p, "失败", self.sink)
            return "【明确失败】没有可供 Leo 复核的结构化 Sage 结论"

        issue = final_audit.make_adjudication_issue(
            p, p.get("merge") or obj, blockers, self.tree, str(self.run_dir),
            {"reason": reason, "repair_rounds": repairs,
             "review_rounds": review_rounds},
            {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX - repairs),
             "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX - review_rounds)},
            sage_reviews=reviews)
        self._set_merge_continuation(p, issue, cid, "leo")
        self._adjudication_issue = issue
        return "【裁决派发】Sage 结论仍未消除争议，等待绑定的结构化 Leo 裁决"

    def _set_merge_continuation(self, p: dict, issue: dict, audit_cid: str,
                                stage: str) -> None:
        candidate = p.get("merge") or {}
        candidate_sha = final_audit._canonical_sha256(candidate)
        p["_merge_continuation"] = {
            "round": int(p.get("round") or 0),
            "origin_audit_cid": str(audit_cid or ""),
            "issue_id": str(issue.get("id") or ""),
            "candidate_sha256": candidate_sha,
            "stage": str(stage or ""),
        }
        issue["candidate_sha256"] = candidate_sha

    def dispatch_sage_review(self, owner: dict, issue: dict) -> list:
        """Create/reuse bounded read-only Sage leaves for the saved merge issue."""
        self.parent = owner
        self.tree[str(owner.get("id") or "")] = owner
        issue_id = str(issue.get("id") or "")
        continuation = owner.get("_merge_continuation") or {}
        candidate = owner.get("merge") or {}
        expected_sha = str(continuation.get("candidate_sha256")
                           or issue.get("candidate_sha256") or "")
        if (not issue_id or not expected_sha
                or final_audit._canonical_sha256(candidate) != expected_sha):
            raise ValueError("Sage issue is not bound to the original merge candidate")

        meta = owner.get("_adjudication") or {}
        existing_ids = [str(x) for x in (meta.get("review_node_ids") or [])
                        if str(x) in self.tree]
        if existing_ids:
            return existing_ids
        blockers = [{"id": str(row.get("id") or ""),
                     "item": str(row.get("item") or ""),
                     "级别": str(row.get("level") or "blocker")}
                    for row in (issue.get("blockers") or [])]
        cycle = int(owner.get("_audit_review_rounds") or 0) + 1
        if cycle > final_audit.FINAL_AUDIT_REVIEW_MAX:
            return []
        state = str(owner.get("state") or "")
        if state == node_mod.WAIT_ADJUDICATION:
            ok, why = node_mod.change_state(owner, "待跑", self.sink)
            if not ok:
                raise ValueError("cannot resume owner before Sage review: %s" % why)
            state = "待跑"
        if state == "待跑":
            ok, why = node_mod.change_state(owner, "在跑", self.sink)
            if not ok:
                raise ValueError("cannot start owner before Sage review: %s" % why)
            state = "在跑"
        if state not in ("在跑", node_mod.WAIT_CHILDREN):
            raise ValueError("owner is not schedulable for Sage review: %s" % state)
        review_ids = self._dispatch_review_leaves(
            owner, blockers, cycle=cycle, issue_id=issue_id)
        owner["_audit_review_rounds"] = cycle
        continuation["stage"] = "sage"
        if review_ids and owner.get("state") == "在跑":
            node_mod.change_state(owner, node_mod.WAIT_CHILDREN, self.sink)
        return review_ids

    def _adjudication_checks(self, owner: dict, issue: dict,
                             require_snapshot: bool = True) -> tuple:
        candidate = owner.get("merge")
        if not isinstance(candidate, dict):
            return False, "original merge candidate is missing", []
        binding = issue.get("binding") or {}
        expected_sha = str(binding.get("candidate_sha256")
                           or issue.get("candidate_sha256") or "")
        if not expected_sha or final_audit._canonical_sha256(candidate) != expected_sha:
            return False, "original merge candidate hash changed", []
        if require_snapshot:
            expected_snapshot = binding.get("artifact_snapshot") or {}
            if not final_audit.snapshot_matches(expected_snapshot, str(self.run_dir)):
                return False, "bound product/doc snapshot changed", []
        hard = final_audit.hard_convergence_failures(
            self, owner, [{"id": str(row.get("id") or ""),
                           "item": str(row.get("item") or ""),
                           "级别": str(row.get("level") or "blocker")}
                          for row in (issue.get("blockers") or [])])
        knobs = role_mod.effective(str(owner.get("role") or ""), "merge", table=self.table)
        ok, why = schema.validate("merge", candidate, knobs)
        if not ok:
            hard.append({"kind": "merge_shape", "detail": why})
        covered, coverage_why = self._check_partial_coverage(owner, candidate)
        if not covered:
            hard.append({"kind": "merge_coverage", "detail": coverage_why})
        return not hard, "", hard

    def _accept_merge_continuation(self, owner: dict, issue: dict,
                                   require_snapshot: bool = True) -> dict:
        ok, why, hard = self._adjudication_checks(
            owner, issue, require_snapshot=require_snapshot)
        if not ok:
            return {"status": "failed", "reason": why or
                    "unwaivable mechanical checks failed: %s" % hard}
        candidate = owner["merge"]
        owner["act"] = "merge"
        owner["reply"] = str(candidate.get("reply") or owner.get("reply") or "")
        owner["未决"] = _merge_unresolved(owner.get("未决") or [],
                                          candidate.get("未决") or [],
                                          str(self.run_dir))
        meta = owner.setdefault("_adjudication", {})
        meta.update({"phase": "applied", "applied": True,
                     "applied_issue_id": str(issue.get("id") or ""),
                     "applied_candidate_sha256": str(
                         (issue.get("binding") or {}).get("candidate_sha256") or "")})
        # 2026-11-13：复核/Leo 把 cap blocker 消掉后，补一条 0-blocker 的终审账。
        # 否则 final_audit.jsonl 最后一行仍是 cap 的「1 blocker」，外部判据会误读成
        # 最后一次终审没通过；实际是复核 pass 覆盖了它。
        try:
            cid = str((owner.get("_merge_continuation") or {}).get("origin_audit_cid") or "")
            if cid and (issue.get("blockers") or []):
                final_audit.ledger(
                    self, int(owner.get("round") or 0), cid, 0, False,
                    原因="复核/裁决 pass 覆盖原 blocker ⇒ 终审 0 blocker")
        except Exception:                                        # noqa: BLE001
            pass
        continuation = owner.setdefault("_merge_continuation", {})
        continuation["stage"] = "applied"
        if owner.get("state") in (node_mod.WAIT_ADJUDICATION, node_mod.WAIT_CHILDREN):
            ok, why = node_mod.change_state(owner, "待跑", self.sink)
            if not ok:
                return {"status": "failed", "reason": "owner cannot resume: %s" % why}
        if owner.get("state") == "待跑":
            ok, why = node_mod.change_state(owner, "在跑", self.sink)
            if not ok:
                return {"status": "failed", "reason": "owner cannot run: %s" % why}
        if owner.get("state") != "在跑":
            return {"status": "failed", "reason": "owner is not running at merge continuation"}
        ok, why = node_mod.change_state(owner, "完成", self.sink)
        if not ok:
            return {"status": "failed", "reason": "owner cannot complete: %s" % why}
        owner["_replan_request"] = None
        return {"status": "accepted"}

    def _adjudication_specs(self, owner: dict, blockers: list) -> list:
        if int(owner.get("_audit_repair_rounds") or 0) >= final_audit.FINAL_AUDIT_REPAIR_MAX:
            return []
        specs = final_audit.repair_specs(
            blockers, self.tree, str(self.run_dir), parent=owner)
        out, seen = [], set()
        for spec in specs:
            target = str(spec.get("target_abs") or "")
            if (target and target not in seen):  # 2026-11-12：不再要求 chars_min
                seen.add(target)
                out.append(spec)
        return out

    def _refresh_adjudication_issue(self, owner: dict, issue: dict,
                                    source: str) -> dict:
        candidate = owner.get("merge") or {}
        ok_audit, why_audit = final_audit.on_converge(self, owner, candidate)
        fresh = owner.pop("_audit_cap_blockers", None)
        if fresh is None:
            fresh = owner.pop("_audit_blockers", None)
        if ok_audit and not why_audit and not fresh:
            accepted = self._accept_merge_continuation(
                owner, issue, require_snapshot=(source != "repair"))
            if accepted["status"] == "accepted":
                return accepted

        if not fresh:
            old_blockers = [
                {"id": str(row.get("id") or ""),
                 "item": str(row.get("item") or ""),
                 "级别": str(row.get("level") or "blocker")}
                for row in (issue.get("blockers") or [])]
            fresh, _dropped = final_audit.reverify_blockers(
                old_blockers, self, str(owner.get("id") or ""))
            fresh, _reviewed = final_audit.apply_reviewed_drops(
                fresh, self, str(owner.get("id") or ""))

        hard = final_audit.hard_convergence_failures(self, owner, fresh)
        if hard:
            specs = self._adjudication_specs(owner, fresh)
            if specs:
                return {"status": "rework", "specs": specs}
            return {"status": "failed",
                    "reason": "mechanical audit still has unwaivable failures: %s" % hard}
        if not fresh:
            return self._accept_merge_continuation(
                owner, issue, require_snapshot=(source != "repair"))

        # 2026-11-13：按原因路由，不让事实冲突进入复核/Leo。
        audit_cid = str((owner.get("_merge_continuation") or {}).get(
            "origin_audit_cid") or "")
        reason_rows = _blocker_reasons(fresh, str(self.run_dir))
        reason_set = {str(row.get("reason") or "") for row in reason_rows}
        if escalation.E_AUTH in reason_set:
            updated = final_audit.make_adjudication_issue(
                owner, candidate, fresh, self.tree, str(self.run_dir),
                {"source": source,
                 "repair_rounds": int(owner.get("_audit_repair_rounds") or 0),
                 "review_rounds": int(owner.get("_audit_review_rounds") or 0)},
                {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX
                                - int(owner.get("_audit_repair_rounds") or 0)),
                 "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                     - int(owner.get("_audit_review_rounds") or 0))},
                hard_checks=[], stage="user")
            self._set_merge_continuation(owner, updated, audit_cid, "user")
            meta = owner.setdefault("_adjudication", {})
            meta.update({"issue": updated, "issue_id": updated["id"],
                         "candidate_sha256": updated["candidate_sha256"],
                         "applied": False, "phase": "user"})
            return {"status": "user", "issue": updated,
                    "reason": str(updated.get("question") or "需要用户决定")}
        if escalation.E_BUDGET in reason_set or escalation.E_BROKEN in reason_set:
            return {"status": "failed",
                    "reason": "blocker 属于预算耗尽/方法失效，不能由复核/Leo 豁免"}
        specs = self._adjudication_specs(owner, fresh)
        unresolved_fact = [
            b for b, row in zip(fresh, reason_rows)
            if row.get("reason") == escalation.E_FACT
            and not _blocker_covered_by_specs(b, specs)
        ]
        if unresolved_fact:
            return {"status": "failed",
                    "reason": "事实冲突没有可写派生目标，不能由复核/Leo 投票选边"}
        if specs and any(row.get("reason") == escalation.E_FACT for row in reason_rows):
            return {"status": "rework", "specs": specs}

        reviews = list(issue.get("sage_reviews") or [])
        use_sage_history = source == "repair" and bool(reviews)
        updated = final_audit.make_adjudication_issue(
            owner, candidate, fresh, self.tree, str(self.run_dir),
            {"source": source,
             "repair_rounds": int(owner.get("_audit_repair_rounds") or 0),
             "review_rounds": int(owner.get("_audit_review_rounds") or 0)},
            {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX
                            - int(owner.get("_audit_repair_rounds") or 0)),
             "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                - int(owner.get("_audit_review_rounds") or 0))},
            sage_reviews=reviews if use_sage_history else None,
            hard_checks=[])
        audit_cid = str((owner.get("_merge_continuation") or {}).get(
            "origin_audit_cid") or "")
        stage = "leo" if use_sage_history else "sage"
        self._set_merge_continuation(owner, updated, audit_cid, stage)
        meta = owner.setdefault("_adjudication", {})
        meta.update({"issue": updated, "issue_id": updated["id"],
                     "candidate_sha256": updated["candidate_sha256"],
                     "applied": False, "phase": stage})
        if stage == "leo":
            return {"status": "leo", "card": updated["card"]}
        return {"status": "sage", "issue": updated}

    def resume_adjudication(self, owner: dict, outcome: dict) -> dict:
        """Apply one bound Sage/Leo outcome to the original merge continuation."""
        self.parent = owner
        self.tree[str(owner.get("id") or "")] = owner
        meta = owner.get("_adjudication") or {}
        issue = meta.get("issue") or {}
        source = str((outcome or {}).get("source") or "")
        issue_id = str(issue.get("id") or meta.get("issue_id") or "")
        continuation = owner.get("_merge_continuation") or {}
        candidate = owner.get("merge") or {}
        candidate_sha = final_audit._canonical_sha256(candidate)
        expected_sha = str((issue.get("binding") or {}).get("candidate_sha256")
                           or issue.get("candidate_sha256")
                           or continuation.get("candidate_sha256") or "")
        if meta.get("applied") and str(meta.get("applied_issue_id") or "") == issue_id:
            return {"status": "accepted"}
        if (not issue_id or str((outcome or {}).get("issue_id") or "") != issue_id
                or not expected_sha or candidate_sha != expected_sha
                or str((outcome or {}).get("candidate_sha256") or "") != candidate_sha):
            return {"status": "failed", "reason": "issue/candidate binding mismatch"}

        if source == "audit":
            audit_ids = [str(x) for x in (outcome.get("audit_node_ids") or [])]
            if any(str((self.tree.get(aid) or {}).get("state") or "") != "完成"
                   for aid in audit_ids):
                return {"status": "failed", "reason": "incremental audit did not complete"}
            return self._refresh_adjudication_issue(owner, issue, "audit")

        if source == "repair":
            repair_ids = [str(x) for x in (outcome.get("repair_node_ids")
                                            or meta.get("repair_node_ids") or [])]
            if not repair_ids or any(str((self.tree.get(rid) or {}).get("state") or "")
                                     != "完成" for rid in repair_ids):
                return {"status": "failed", "reason": "bounded repair leaf did not complete"}
            return self._refresh_adjudication_issue(owner, issue, "repair")

        if source == "sage":
            valid, why, hard = self._adjudication_checks(owner, issue, require_snapshot=True)
            if not valid:
                if not why and hard:
                    specs = self._adjudication_specs(
                        owner, [{"id": row.get("id"), "item": row.get("item"),
                                 "级别": row.get("level") or "blocker"}
                                for row in (issue.get("blockers") or [])])
                    if specs:
                        return {"status": "rework", "specs": specs}
                return {"status": "failed", "reason": why or
                        "mechanical checks prevent applying Sage: %s" % hard}
            expected_blockers = {str(row.get("id") or "")
                                 for row in (issue.get("blockers") or [])}
            meta = owner.get("_adjudication") or {}
            review_ids = [str(x) for x in (meta.get("review_node_ids") or [])]
            reviews, by_blocker = [], {}
            for rid in review_ids:
                leaf = self.tree.get(rid) or {}
                verdict = leaf.get("_review_verdict")
                blocker_id = str(leaf.get("_review_blocker_id") or "")
                if (str(leaf.get("_review_issue_id") or "") != issue_id
                        or blocker_id not in expected_blockers
                        or str(leaf.get("state") or "") != "完成"
                        or leaf.get("judge_ok") is not True
                        or not isinstance(verdict, dict)):
                    continue
                if (verdict.get("verdict") == "pass"
                        and not final_audit._review_leaf_passes(
                            leaf, str(self.run_dir), blocker_id, issue_id)):
                    continue
                row = {"leaf_id": rid, "blocker_id": blocker_id,
                       "verdict": verdict.get("verdict"),
                       "covers": list(verdict.get("covers") or []),
                       "evidence": list(verdict.get("evidence") or []),
                       "note": str(verdict.get("note") or "")}
                reviews.append(row)
                by_blocker[blocker_id] = row
            all_pass = bool(expected_blockers) and all(
                bid in by_blocker and by_blocker[bid].get("verdict") == "pass"
                and bid in (by_blocker[bid].get("covers") or [])
                for bid in expected_blockers)
            if all_pass:
                return self._accept_merge_continuation(owner, issue)

            rework_ids = [bid for bid in expected_blockers
                          if by_blocker.get(bid, {}).get("verdict") == "rework"]
            if rework_ids:
                wanted = [{"id": row.get("id"), "item": row.get("item"),
                           "级别": row.get("level") or "blocker"}
                          for row in (issue.get("blockers") or [])
                          if str(row.get("id") or "") in rework_ids]
                specs = self._adjudication_specs(owner, wanted)
                if specs:
                    issue["sage_reviews"] = reviews
                    meta["issue"] = issue
                    return {"status": "rework", "specs": specs}
            updated = final_audit.make_adjudication_issue(
                owner, candidate,
                [{"id": str(row.get("id") or ""),
                  "item": str(row.get("item") or ""),
                  "级别": str(row.get("level") or "blocker")}
                 for row in (issue.get("blockers") or [])],
                self.tree, str(self.run_dir),
                {"source": "sage", "verdicts": reviews},
                {"repairs": max(0, final_audit.FINAL_AUDIT_REPAIR_MAX
                                - int(owner.get("_audit_repair_rounds") or 0)),
                 "sage_reviews": max(0, final_audit.FINAL_AUDIT_REVIEW_MAX
                                    - int(owner.get("_audit_review_rounds") or 0))},
                sage_reviews=reviews, hard_checks=[])
            self._set_merge_continuation(
                owner, updated,
                str(continuation.get("origin_audit_cid") or ""), "leo")
            meta.update({"issue": updated, "issue_id": updated["id"],
                         "candidate_sha256": updated["candidate_sha256"],
                         "phase": "leo", "applied": False})
            return {"status": "leo", "card": updated["card"]}

        if source == "leo":
            if str((outcome or {}).get("card_sha256") or "") != final_audit._canonical_sha256(
                    issue.get("card") or {}):
                return {"status": "failed", "reason": "Leo card hash mismatch"}
            receipt = outcome.get("receipt") or {}
            if receipt.get("status") != "done" or receipt.get("requires_user_approval"):
                return {"status": "failed", "reason": "Leo receipt is not an applicable judgement"}
            verdict = str(receipt.get("verdict") or "")
            if verdict == "pass":
                if receipt.get("blocking") is not False or receipt.get("missing"):
                    return {"status": "failed", "reason": "Leo pass has blocking/missing items"}
                return self._accept_merge_continuation(owner, issue)
            if verdict == "rework":
                blockers = [{"id": str(row.get("id") or ""),
                             "item": str(row.get("item") or ""),
                             "级别": str(row.get("level") or "blocker")}
                            for row in (issue.get("blockers") or [])]
                specs = self._adjudication_specs(owner, blockers)
                if specs:
                    return {"status": "rework", "specs": specs}
                return {"status": "failed", "reason": "Leo requested rework without remaining executable repair"}
            return {"status": "failed", "reason": "Leo did not return pass or executable rework"}
        return {"status": "failed", "reason": "unsupported adjudication outcome source"}

    def _structured_review_rows(self, p: dict) -> list:
        """Return only normalized Sage verdicts saved on this owner's read-only review leaves."""
        rows = []
        try:
            ids = node_mod.subtree_ids(str(p.get("id") or ""), self.tree)
        except (KeyError, TypeError, ValueError):
            ids = []
        for nid in ids:
            leaf = self.tree.get(nid) or {}
            verdict = leaf.get("_review_verdict")
            if (".fix.review" not in str(nid) or not isinstance(verdict, dict)
                    or str(leaf.get("_review_issue_id") or "") not in (
                        "", str((p.get("_adjudication") or {}).get("issue_id") or ""))):
                continue
            rows.append({
                "leaf_id": str(nid),
                "blocker_id": str(leaf.get("_review_blocker_id") or ""),
                "verdict": verdict.get("verdict"),
                "covers": list(verdict.get("covers") or []),
                "evidence": list(verdict.get("evidence") or []),
                "note": str(verdict.get("note") or ""),
            })
        return rows

    def _dispatch_repair_leaves(self, p: dict, specs: list, cycle: int = 1) -> list:
        """按修复规格直接建修复叶节点（机械派发，不走模型 spawn 轮）。

        id 形态 `父.fix.audit<轮>.<序号>`（含 ".fix." ⇒ 修复 kind；跨轮不撞车）；
        batch = 本 merge 轮号；每叶建前过 nodes_max（不过 ⇒ 跳过并记一笔）。
        @returns: 建成的节点 id 列表。
        """
        pid = str(p.get("id") or "")
        batch = int(p.get("round") or 0)
        out = []
        for i, spec in enumerate(specs or []):
            # 2026-11-13：修复目标必须在 authority.writable 白名单里；
            # 只读权威/受保护产物永不派修复叶（哪怕模型说它错了）。
            target_rel = str(spec.get("target") or "")
            if target_rel:
                try:
                    from harness import authority as _auth
                    if not _auth.is_writable(
                            target_rel, _auth.load_policy(str(self.run_dir))):
                        gate.log_violation(
                            self.sink, kind="终审_修复目标不可写", node_id=pid,
                            detail=("修复目标 %s 不在 authority.writable 白名单，拒绝派发"
                                    % target_rel))
                        continue
                except Exception:                                # noqa: BLE001
                    pass
            managed_doc = None
            try:
                rel = Path(str(spec.get("target") or ""))
                root = self.run_dir.resolve()
                target_abs = Path(str(spec.get("target_abs") or "")).resolve()
                target_from_run = (root / rel).resolve()
                if (not rel.is_absolute() and rel.parts and ".." not in rel.parts
                        and rel.parts[0].lower() not in ("input", "product")
                        and rel.suffix.lower() in (".md", ".txt")
                        and target_abs == target_from_run
                        and os.path.commonpath((str(root), str(target_abs))) == str(root)):
                    managed_doc = {"target": rel.as_posix()}
            except (OSError, ValueError, TypeError):
                managed_doc = None
            child = final_audit.build_repair_child(
                spec, str(self.run_dir), tree=self.tree, parent=p,
                managed_doc=managed_doc is not None)
            cid = "%s.fix.audit%d.%d" % (pid, int(cycle), i + 1)
            c = node_mod.new_node(cid, "执行", parent=pid,
                                  context=str(child.get("context") or ""),
                                  judge=child.get("judge"),
                                  验收标准=str(child.get("验收标准") or ""),
                                  auth=str(child.get("auth") or ""),
                                  overrides=dict(child.get("overrides") or {}),
                                  batch=batch, act="leaf", round=1)
            if managed_doc is not None:
                c["_managed_doc"] = managed_doc
            ok_nm, _why_nm = gate.check_nodes_max(
                len(self.tree), audit_mod.nodes_max(self.table), self.sink, cid)
            if not ok_nm:
                continue
            self.tree[cid] = c
            out.append(cid)
        return out

    def _mechanical_repair_from_merge(self, p: dict, raw: str) -> str:
        """spawn JSON 不可解析时的机械兜底：按 merge 点名的可写目标派修复叶。

        触发条件（全部满足）：
          · 父的 merge 已经给了下一批要点或 reply；
          · 文本里点名的路径在 authority.writable 白名单里；
          · 能解析出 run 内绝对路径。
        不满足就返回 ""，走原来的拒收/重试。无模型调用、无猜测。
        """
        merge = p.get("merge") or {}
        points = merge.get("下一批要点")
        if isinstance(points, list):
            text = "\n".join(str(x) for x in points)
        else:
            text = str(points or merge.get("reply") or "")
        if not text:
            return ""
        from harness import authority as _auth, evidence as _ev
        policy = _auth.load_policy(str(self.run_dir))
        target, target_abs = "", ""
        for cite in _ev.seen_citations(text):
            if not _auth.is_writable(cite, policy):
                continue
            rel, abs_path = final_audit._resolve_target(cite, str(self.run_dir))
            if rel and abs_path:
                target, target_abs = rel, abs_path
                break
        if not target:
            return ""
        spec = {"item": text[:800], "blocker_id": "mechanical-spawn",
                "target": target, "target_abs": target_abs, "repair_of": ""}
        cycle = int(p.get("round") or 0)
        created = self._dispatch_repair_leaves(p, [spec], cycle=cycle)
        if not created:
            return ""
        p["_audit_repair_rounds"] = int(p.get("_audit_repair_rounds") or 0) + 1
        p["act"], p["merge"] = "spawn", None
        p["batch"] = int(p.get("round") or 0)
        if p.get("state") != node_mod.WAIT_CHILDREN:
            node_mod.change_state(p, node_mod.WAIT_CHILDREN, self.sink)
        gate.log_violation(
            self.sink, kind="驱动_机械修复派发", node_id=str(p.get("id") or ""),
            detail=("spawn JSON 不可解析；按 merge 点名的可写目标 %s 机械派发修复叶 %s"
                    % (target, created[0])))
        return created[0]

    def _dispatch_review_leaves(self, p: dict, blockers: list, cycle: int = 1,
                                issue_id: str = "") -> list:
        """按终审 blocker 直接建纯复核叶节点（机械派发，不走模型 spawn 轮；决策 C）。

        每条 blocker 一个叶：id 形态 `父.fix.review<轮>.<序号>`；role=审计（只读，
        may_write=false、无工具）；batch = 本 merge 轮号；每叶建前过 nodes_max
        （不过 ⇒ 跳过并记一笔）。派发时对 product/ 与 doc/ 取 sha256 快照、
        连同被复核 blocker 原文记在叶上（_review_snapshot/_review_of），
        供终审采纳复核结论时重测（见 final_audit.apply_reviewed_drops）。
        @returns: 建成的节点 id 列表。
        """
        pid = str(p.get("id") or "")
        batch = int(p.get("round") or 0)
        out = []
        snap = final_audit.snapshot_product_files(str(self.run_dir))
        for i, b in enumerate(blockers or []):
            child = final_audit.build_review_child(
                b, str(self.run_dir), parent_reply=str(p.get("reply") or ""),
                tree=self.tree, issue_id=str(issue_id or ""),
                parent_id=pid)
            # 2026-11-13：复核叶也必须看到程序读出的权威事实，避免同模型二次编造。
            try:
                from harness import authority as _auth
                sheet = _auth.fact_sheet(str(self.run_dir))
                if sheet:
                    child["context"] = str(child.get("context") or "") + sheet
            except Exception:                                    # noqa: BLE001
                pass
            cid = "%s.fix.review%d.%d" % (pid, int(cycle), i + 1)
            existing = self.tree.get(cid)
            if existing:
                if (str(existing.get("_review_issue_id") or "") != str(issue_id or "")
                        or str(existing.get("_review_blocker_id") or "")
                        != str(child.get("_review_blocker_id") or "")):
                    raise ValueError("review node id is already bound to another issue")
                out.append(cid)
                continue
            c = node_mod.new_node(cid, audit_mod.AUDIT_ROLE, parent=pid,
                                  context=str(child.get("context") or ""),
                                  judge=child.get("judge"),
                                  验收标准=str(child.get("验收标准") or ""),
                                  auth=str(child.get("auth") or ""),
                                  overrides=dict(child.get("overrides") or {}),
                                  batch=batch, act="leaf", round=1)
            c["_review_of"] = [str((b or {}).get("item") or "")]
            c["_review_snapshot"] = dict(snap)
            for key in ("_structured_review", "_review_blocker_id", "_review_issue_id",
                        "_review_evidence_ids"):
                if key in child:
                    c[key] = child[key]
            ok_nm, _why_nm = gate.check_nodes_max(
                len(self.tree), audit_mod.nodes_max(self.table), self.sink, cid)
            if not ok_nm:
                continue
            self.tree[cid] = c
            out.append(cid)
        return out

    def spawn_children(self, p: dict, obj: dict, knobs: dict = None) -> list:
        """spawn 轮：把 children[] 落成节点（batch = 本 spawn 轮的 round 号）。

        ★ 去向=切分 时子挂到【切分目标】下面（拉孙）；否则挂在父自己下面。
        """
        go = (p.get("merge") or {}).get("去向")
        targets = (p.get("merge") or {}).get("切分目标") or []
        host = p["id"]
        if go == "切分" and targets:
            host = str(targets[0])                    # 拉孙：挂到切分目标那个【已有的子】下面
        limit = knobs.get("max_children_per_round") or 0
        kept, dropped = gate.truncate_children(obj.get("children") or [], knobs, self.sink)
        out = []
        n = 0
        empty_dropped = []
        # V2: 拒因按类计数（旧值只记空叶数，apply_output 把一切空出都报成“无可核验
        # 目标”——routeB-02 的 12 条同类拒收全被错报成形状问题，根 4 次修错方向）。
        _drops = {"empty": 0, "same_error": 0, "same_kind": 0, "nodes_max": 0}
        # V2.1 收尾 (B1) 要点：F2 用 plan children 的原始序号对应，过滤掉的子不登记；
        # F3 ID 带轮次，跨轮重派不混淆。
        try:
            _pts = _plan_points(p)
        except Exception:
            _pts = []
        _round = int(p.get("round") or 0)
        for _j, ch in enumerate(kept):
            if not isinstance(ch, dict):
                continue
            # 砍空叶（t-f6f9d7325d93）：无可核验目标的 child 不落节点。
            _ctx = str(ch.get("context") or "").strip()
            if len(_ctx) > _SPAWN_CONTEXT_MAX:
                gate.log_violation(
                    self.sink, kind="驱动_子上下文截断", node_id=str(p["id"]),
                    detail=("spawn child context %d 字 > %d，已截断；"
                            "文件正文应由执行叶自己读" % (len(_ctx), _SPAWN_CONTEXT_MAX)))
                _ctx = (_ctx[:_SPAWN_CONTEXT_MAX]
                        + "\n…（child context 超长已截断；请用工具读取目标文件）")
                ch["context"] = _ctx
            _acc = str(ch.get("验收标准") or "").strip()
            _jdg = ch.get("judge")
            if not _ctx or (not _acc and not _jdg):
                gate.log_violation(self.sink, kind="驱动_空叶", node_id=str(p["id"]),
                                   detail="spawn 的 child 无可核验目标被拒绝（context 空=%s，验收标准/judge 均空=%s）：%s"
                                   % (not _ctx, (not _acc and not _jdg), str(ch)[:200]))
                empty_dropped.append(ch)
                _drops["empty"] += 1
                continue
            # V2.1 P2 phantom-citation same-error redispatch guard.
            _norm = _norm_context(_ctx)
            _blocked_mid = None
            if _norm:
                for _mid in node_mod.subtree_ids(str(p["id"]), self.tree):
                    if _mid == str(p["id"]):
                        continue
                    _m = self.tree.get(_mid) or {}
                    if str(_m.get("failure_class") or "")!= "phantom_citation":
                        continue
                    if _norm == _norm_context(str(_m.get("context") or "")):
                        _blocked_mid = _mid
                        break
            if _blocked_mid:
                gate.log_violation(self.sink, kind="驱动_同错重派被拒", node_id=str(p["id"]),
                    detail="新生子 context 与幻影引用失败叶 %s 相同 ⇒ 同错重派被拒，父须换任务方案" % _blocked_mid)
                if getattr(self, "recovery", None) is not None:
                    self.recovery.record(str(p["id"]), _blocked_mid, "phantom_citation",
                        "同错重派被拒：新生子 context 与失败叶相同", None, None, "blocked_same_error")
                _drops["same_error"] += 1
                continue
            # V2.2 自愈：同 role 连续失败预算（统一恢复接线，与上者互补）。
            # 同一父下，某 role 已有 ≥2 个终结失败叶 ⇒ 再派同 role 子节点即被拒，
            # 父须换 role（如执行→调研）或将该事实记为未决，不许换个措辞原样重派。
            # P5 tier1-2 实测：父连续三轮派执行叶测 CPU（c2.2/c4.1/c6.1），
            # 换措辞绕过了 P2 的同错重派守卫（context 不完全相同），三次同形失败。
            # 只计失败叶（完成/部分不计），只拦同 role，不拦其他 role。
            _role = str(ch.get("role") or "")
            _fail_n = 0
            if _role:
                for _mid in node_mod.subtree_ids(str(p["id"]), self.tree):
                    if _mid == str(p["id"]):
                        continue
                    _m = self.tree.get(_mid) or {}
                    if not node_mod.is_terminal(_m):
                        continue
                    if str(_m.get("role") or "") != _role:
                        continue
                    if str(_m.get("state") or "") == "失败":
                        _fail_n += 1
            # V2 修复叶豁免：同类预算不拦“接着上轮失败叶继续干”的修复子节点。
            # 机械可判：overrides.repair_of 必须指向上轮同父失败叶，且 context 必须
            # 点名磁盘上已存在的文件（或该失败叶已登记的产物）；两者缺一则仍拒。
            # 同一失败叶最多修 2 次（防无限死循环）。换措辞绕过仍被拦（无 repair_of
            # 链接 + 无已存在文件引用时不是修复叶）。
            _repair_ok = False
            _rep_id = ""
            _ovr = ch.get("overrides") or {}
            if isinstance(_ovr, dict):
                _rep_id = str(_ovr.get("repair_of") or "")
            if _rep_id and _fail_n >= 2:
                _rep_node = self.tree.get(_rep_id) or {}
                _rep_in_subtree = _rep_id in node_mod.subtree_ids(str(p["id"]), self.tree)
                _rep_failed = str(_rep_node.get("state") or "") == "失败"
                _named = _existing_artifact_paths(_ctx, str(self.run_dir))
                _rep_has_art = bool(_rep_node.get("artifacts") or [])
                if _rep_in_subtree and _rep_failed and (_named or _rep_has_art):
                    _n_rep = 0
                    for _mid in node_mod.subtree_ids(str(p["id"]), self.tree):
                        _m = self.tree.get(_mid) or {}
                        _mo = _m.get("overrides") or {}
                        if isinstance(_mo, dict) and str(_mo.get("repair_of") or "") == _rep_id:
                            _n_rep += 1
                    if _n_rep < 2:
                        _repair_ok = True
                        gate.log_violation(self.sink, kind="驱动_同类重派豁免_修复",
                        node_id=str(p["id"]),
                        detail="修复叶豁免：child 修复失败叶 %s（context 点名已存在文件 %s；该叶已修 %d 次）"
                        % (_rep_id, ",".join(_named) if _named else "失败叶已登记产物",
                            _n_rep))
            if _fail_n >= 2 and not _repair_ok:
                # 换 role 建议从 role 表派生（有工具 + 不写 = 调查 role），不写死 role 名。
                _fallback = _investigate_role(self.table)
                gate.log_violation(self.sink, kind="驱动_同类重派被拒", node_id=str(p["id"]),
                    detail="role %r 在父 %s 下已有 %d 个失败叶 ⇒ 同类重派被拒，父须换 role 或记未决"
                           % (_role, str(p["id"]), _fail_n))
                if getattr(self, "recovery", None) is not None:
                    self.recovery.record(str(p["id"]), str(p["id"]), "same_kind_redispatch",
                        "同类重派被拒：role %s 已失败 %d 次" % (_role, _fail_n),
                        None, {"kind": "research", "role": _fallback,
                               "focus": "换 role 重派（原 role 已连续失败）"}, "blocked_same_kind")
                # E16 回馈：拒因写进 _last_reject，父下一轮 prompt 能看到、须换策略。
                p["_last_reject"] = {"act": "spawn",
                    "detail": "role %s 已连续失败 %d 次，本轮拒派同 role 子节点（同类预算）；请改派其他 role（如 %s），或将该事实记为未决，或派修复叶（overrides.repair_of + context 点名已存在文件）接着失败处继续干；不要换措辞重派同 role" % (_role, _fail_n, _fallback or "调查类 role")}
                _drops["same_kind"] += 1
                continue
            n += 1
            # 子 id 带**本批次的轮号**：同一批内的序号从 1 起，跨批不撞车（否则新一批会覆盖旧一批）
            cid = "%s.c%d.%d" % (host, int(p["round"]), n)
            # V2 定点修复（t-dc62df942275 项 3）：修复叶 context 模板化。
            # overrides.repair_of 指向上轮同父失败叶时，context 改由 Harness 按
            # 失败叶的机械记录（verify 失败项/产物/复测命令）生成，根原文只作
            # “修什么”补充。注意顺序：豁免判定在前（仍用根原文点名文件，语义
            # 不变），模板化在后（只改落盘执行的 context）；repair_of 未指向
            # 同父失败叶时不改动。
            _ctx_final = _ctx
            if _rep_id:
                _rep_node = self.tree.get(_rep_id) or {}
                if (_rep_id in node_mod.subtree_ids(str(p["id"]), self.tree)
                        and str(_rep_node.get("state") or "") == "失败"):
                    _tmpl = build_repair_context(_rep_node, str(self.run_dir), _ctx)
                    if _tmpl:
                        _ctx_final = _tmpl
            _ctx_final = bound_fact_block(_ctx_final, str(self.run_dir))
            c = node_mod.new_node(cid, str(ch.get("role") or ""), parent=host,
                                  context=_ctx_final,
                                  judge=ch.get("judge"), 验收标准=str(ch.get("验收标准") or ""),
                                  auth=str(ch.get("auth") or ""), overrides=dict(ch.get("overrides") or {}),
                                  batch=int(p["round"]))
            # ── ① tree-04 插入点：每子落进 self.tree 之前过 nodes_max（§2.4 ①）──
            #   不过 ⇒ 丢该子（check_nodes_max 已记一笔 截断_nodes_max）；nodes_max 从 role 表读。
            ok_nm, _why_nm = gate.check_nodes_max(len(self.tree), audit_mod.nodes_max(self.table),
                                                  self.sink, cid)
            if not ok_nm:
                _drops["nodes_max"] += 1
                continue
            self.tree[cid] = c
            out.append(cid)
            # F2+F3：该子创建成功才登记其原始序号对应的前提（带轮次 ID）；被过滤的
            # 子（空叶/同错重派/nodes_max）不登记，幸存子保持原始序号，不错位。
            if _j < len(_pts):
                try:
                    from harness import premise as _premise_mod
                    _premise_mod.register_premise(
                        str(self.run_dir), "P-%s-r%d-%d" % (str(p["id"]), _round, _j + 1),
                        _pts[_j], str(p["id"]), [cid])
                except Exception:
                    pass
        # V2.1 P2: parent switched strategy after phantom failure -> record recovery edge.
        if out:
            try:
                _has_ph = any(str((self.tree.get(_m) or {}).get("failure_class") or "") == "phantom_citation"
                    for _m in node_mod.subtree_ids(str(p["id"]), self.tree))
                if _has_ph and getattr(self, "recovery", None) is not None:
                    self.recovery.record(str(p["id"]), str(p["id"]), "phantom_citation",
                        "幻影失败后换任务方案重派 %d 个子节点" % len(out),
                        None, {"kind": "research", "role": _investigate_role(self.table),
                        "tools": "read,bash",
                        "focus": "换任务方案重派（与幻影失败叶不同任务）"}, "dispatched")
            except Exception:
                pass
        p["result"] = json.dumps({"children": out, "截断": len(dropped), "空叶拒绝": len(empty_dropped),
            "同错拒绝": _drops["same_error"], "同类拒绝": _drops["same_kind"], "超量拒绝": _drops["nodes_max"]},
                                 ensure_ascii=False)
        p["_spawn_drops"] = dict(_drops)
        return out
