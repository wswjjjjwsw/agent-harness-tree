"""D10 形状审查（**恢复**，76 §5.6，task-def §2.3 / §3.9）。

三层语义里的**柔性**那一层：越【建议值】（每层 ≤9 / 深度 3）⇒ 不截断，起一个 role=审计 的检查点，
输入 = 当前树的【结构快照】+ 越限事实，产出 {判断, 理由, 建议}，去向 = 写进【紧接着的那一轮】merge
的 对审计发现（item / 建议 / 说明）。

★ 审查【只给建议、不自己裁】；裁决权在父（父的裁决落在同轮 `去向`）。
★ `nodes_max=80` 硬闸**不撤**：检查点创建**前**也过 gate.check_nodes_max（落在
  `audit.make_checkpoint` 里，本模块建检查点一律走它 ⇒ 不截断的只是**父的子**，检查点自身仍受硬闸）；
  审查通过也不能破 80。本模块对树的写操作**只有**「经 `audit.make_checkpoint` 建一个检查点」这一种。
★ 顺序钉死（task-def §2.4 ② / §3.9 末句，两处字面一致）：
  落子（含 nodes_max 硬截）→ **抽检触发** → **形状审查触发** ⇒ 本模块**不截断任何子**。
  （G2 修正：attempt-1 的本行写的是「抽检触发 → 形状审查触发」但 `audit.on_spawn` 里代码是反的 ——
    文件里的说明与文件里的行为不一致；现已两侧一致，顺序与正文相同。）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from harness import audit, gate, node as node_mod, role as role_mod, submit

DEFAULT_WIDTH_MAX = 9        # §6.4 建议值（role.yaml 的 driver 段为准，这里只是兜底）
DEFAULT_DEPTH_MAX = 3

PROMPT_EXTRA_SHAPE = (
    "\n## 本轮格式要求（只写格式，不写业务）\n"
    "只输出三个键：{\"判断\": \"合理\"|\"不合理\", \"理由\": \"…\", \"建议\": \"继续派\"|\"切分\"|\"收束\"}。\n"
    "出现别的键一律被丢弃并记一笔。这是形状审查：**只给建议、不裁决**，采纳与否由父决定。"
)
SHAPE_KEYS = ("判断", "理由", "建议")
JUDGES = ("合理", "不合理")
ADVICES = ("继续派", "切分", "收束")     # 与 schema.ADVICES 同源同值


# ---------------------------------------------------------------- 阈值 / 统计
def limits(table: dict = None) -> dict:
    """建议值从**数据**读（role.yaml 的 driver 段），不硬编码。"""
    t = table if isinstance(table, dict) else role_mod.load_roles()
    d = t.get("driver") or {}
    return {"width_max": int(d.get("width_max", DEFAULT_WIDTH_MAX)),
            "depth_max": int(d.get("depth_max", DEFAULT_DEPTH_MAX))}


def width_of(parent: dict, tree: dict) -> int:
    """该父自己名下 role != 审计（= 不含检查点）的子节点数（含历史各批、按节点计、去重）。"""
    pid = str((parent or {}).get("id") or "")
    return len({nid for nid, n in (tree or {}).items()
                if str((n or {}).get("parent") or "") == pid
                and str((n or {}).get("role") or "") != audit.AUDIT_ROLE})


def _content_nodes(tree: dict) -> list:
    """非检查点的节点 id（深度/宽度统计口径：**不含**检查点）。"""
    return [nid for nid, n in (tree or {}).items()
            if str((n or {}).get("role") or "") != audit.AUDIT_ROLE]


def depth_of(tree: dict) -> int:
    """全树最大深度（根 = 0，用 node.layer_of）；**排除检查点**（否则审计点自己把深度 +1）。"""
    ids = _content_nodes(tree)
    if not ids:
        return 0
    return max(node_mod.layer_of(nid, tree) for nid in ids)


def snapshot(tree: dict) -> dict:
    """结构快照（键集**逐字钉死**，§3.9）：只有这五个键，节点条目只有 id/role/parent/state 四键。

    ★ **绝不含**任何节点的 reply/context/未决/result/doc 正文 ——
      这就是 §5.1②「只读兄弟产出、不进子孙」的唯一例外：只读结构、不读内容。
    ★ 统计口径：`节点总数` / `节点[]` **含**检查点；`最大深度` / `每层宽度` **不含**检查点。
    """
    layers = {}
    for nid in _content_nodes(tree):
        lv = node_mod.layer_of(nid, tree)
        layers[lv] = layers.get(lv, 0) + 1
    return {
        "节点总数": len(tree or {}),
        "最大深度": depth_of(tree),
        "每层宽度": [{"层": lv, "宽度": layers[lv]} for lv in sorted(layers)],
        "节点": [{"id": nid, "role": (tree[nid] or {}).get("role"),
                  "parent": (tree[nid] or {}).get("parent"),
                  "state": (tree[nid] or {}).get("state")} for nid in sorted(tree or {})],
    }


def over_limit(parent: dict, tree: dict, table: dict = None) -> list:
    """越限事实：@returns [{"触发": "宽度"|"深度", ...}]（空 = 未越建议值）。"""
    lim = limits(table)
    out = []
    w = width_of(parent, tree)
    if w > lim["width_max"]:
        out.append({"触发": "宽度", "父id": str((parent or {}).get("id") or ""), "宽度": w,
                    "深度": depth_of(tree), "建议值": lim, "超": w - lim["width_max"]})
    dep = depth_of(tree)
    if dep > lim["depth_max"]:
        out.append({"触发": "深度", "父id": str((parent or {}).get("id") or ""), "宽度": w,
                    "深度": dep, "建议值": lim, "超": dep - lim["depth_max"]})
    return out


# ---------------------------------------------------------------- 账
LEDGER_KEYS = ("ts", "触发", "父id", "宽度", "深度", "建议值", "检查点id",
               "产出键", "判断", "建议", "承载轮")


def ledger(d, row: dict) -> str:
    """shape.jsonl 每触发一次一行（键集逐字照 §3.9）。

    ★ 未触发成审查（被 nodes_max 拒 / 输入超预算 / 审查失败）时也记一行：钉死的 11 键**一个不少**
      （缺的填空值），另附 `状态=未审` + `原因` —— 与 §3.9「记一行 未审」同一口径。
    ★ 这样 `audit.jsonl` 才能守住 §3.7 的「**每批一行**」：形状审查的记账不挤进抽检的账里。
    """
    full = {k: row.get(k, "") for k in LEDGER_KEYS}
    full.update({k: v for k, v in (row or {}).items() if k not in LEDGER_KEYS})
    return audit.append_jsonl(Path(d.run_dir) / "shape.jsonl", full)


def ledger_unreviewed(d, over: list, 触发: str, pid: str, 承载轮: int, 原因: str) -> str:
    """形状审查没能产出判断时的一行（§3.9「走未审记账」）。"""
    return ledger(d, {"ts": round(time.time(), 3), "触发": 触发, "父id": pid,
                      "宽度": over[0]["宽度"] if over else 0,
                      "深度": over[0]["深度"] if over else 0,
                      "建议值": limits(d.table), "检查点id": "", "产出键": [],
                      "判断": "", "建议": "", "承载轮": 承载轮,
                      "状态": "未审", "原因": 原因})


def read_ledger(d) -> list:
    p = Path(d.run_dir) / "shape.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 触发
def on_spawn(d, parent: dict):
    """由 audit.on_spawn 在抽检之前调用。@returns: 检查点 id 或 None。"""
    from harness import driver as driver_mod                      # 局部导入：避免 import 环
    over = over_limit(parent, d.tree, d.table)
    if not over:
        return None                                          # 未越建议值 ⇒ 0 次调用、无行
    pid = str((parent or {}).get("id") or "")
    trig = int((parent or {}).get("round") or 0)
    batch_no = "%s#%d" % (pid, trig)
    knobs = role_mod.effective(audit.AUDIT_ROLE, None, table=d.table)
    fact = ("\n\n## 越限事实（超出的是【建议值】，不是硬顶；**不许截断**）\n"
            + json.dumps({"越限": over}, ensure_ascii=False, indent=1)
            + "\n建议值口径：每层宽度 ≤%d / 树深度 ≤%d（软建议，只给建议）"
              % (limits(d.table)["width_max"], limits(d.table)["depth_max"]))
    ctx = ("## 当前树的结构快照（只有结构，不含任何节点正文）\n"
           + json.dumps(snapshot(d.tree), ensure_ascii=False, indent=1) + fact)

    est = submit.estimate_tokens(ctx)
    budget = int(knobs.get("ctx_budget") or 0)
    if budget and est > budget:
        ledger_unreviewed(d, over, over[0]["触发"], pid, trig + 1,
                          "形状审查输入超 ctx_budget（%d token > %d）" % (est, budget))
        return None

    cid = "%s.%s%d.1" % (pid, audit.SHAPE_SLOT, trig)
    node = audit.make_checkpoint(d, cid, pid, ctx, knobs)
    if node is None:
        ledger_unreviewed(d, over, over[0]["触发"], pid, trig + 1,
                          "检查点创建被 nodes_max 拒（总量已到上限）")
        return None

    prompt = submit.assemble_prompt(node, knobs, act="leaf") + PROMPT_EXTRA_SHAPE
    try:
        raw = audit.run_checkpoint(d, node, knobs, prompt)
        obj = driver_mod.parse_act_output(raw, "leaf")
    except Exception as exc:                                 # noqa: BLE001
        audit.finish(node, "失败", d)
        ledger_unreviewed(d, over, over[0]["触发"], pid, trig + 1,
                          "形状审查失败：%s: %s" % (type(exc).__name__, exc))
        return cid

    keep, dropped, why = check_output(obj, d, cid)
    if why:
        audit.finish(node, "失败", d)
        ledger_unreviewed(d, over, over[0]["触发"], pid, trig + 1, "形状审查失败：%s" % why)
        return cid

    node["reply"] = "%s：%s" % (keep["判断"], keep["理由"])
    node["未决"] = []                                        # 检查点不是父：不产出未决
    node["result"] = json.dumps(keep, ensure_ascii=False)
    audit.finish(node, "完成", d)
    ledger(d, {"ts": round(time.time(), 3), "触发": over[0]["触发"], "父id": pid,
               "宽度": width_of(parent, d.tree), "深度": depth_of(d.tree),
               "建议值": {"width_max": limits(d.table)["width_max"],
                          "depth_max": limits(d.table)["depth_max"]},
               "检查点id": cid, "产出键": list(obj.keys()),
               "判断": keep["判断"], "建议": keep["建议"], "承载轮": trig + 1})
    return cid


def check_output(obj: dict, d, cid: str) -> tuple:
    """动作契约由**本模块自卡**（§3.9：它是「检查点动作」，不走 schema.validate）。

    · 三元键缺失 / 枚举不符 ⇒ 该检查点判**失败** ⇒ 走「未审」记账（不阻塞）；
    · 出现**别的键** ⇒ 丢弃那些键 + 记一笔 审查_越权字段（与 schema.check_permission 同一风格），不中断。
    @returns: (留下的三元键 dict, 被丢弃的键, 失败原因)
    """
    if not isinstance(obj, dict):
        return {}, [], "产出不是 JSON 对象"
    dropped = [k for k in obj if k not in SHAPE_KEYS]
    if dropped:
        gate.log_violation(d.sink, kind="审查_越权字段", node_id=cid,
                           detail=("形状审查产出出现越权键 %s ⇒ 丢弃（只留 %s）"
                                   % (dropped, list(SHAPE_KEYS))))
    keep = {k: obj.get(k) for k in SHAPE_KEYS}
    miss = [k for k in SHAPE_KEYS if not str(keep.get(k) or "").strip()]
    if miss:
        return {}, dropped, "三元键缺失：%s" % miss
    if keep["判断"] not in JUDGES:
        return {}, dropped, "判断=%r 不在 %s" % (keep["判断"], list(JUDGES))
    if keep["建议"] not in ADVICES:
        return {}, dropped, "建议=%r 不在 %s" % (keep["建议"], list(ADVICES))
    return {k: str(keep[k]) for k in SHAPE_KEYS}, dropped, ""
