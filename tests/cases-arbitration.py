"""V2.1 阶段三 P4 外部急救仲裁接线回归（t-1ce5503f1445）。跑法：cd <候选根> && python3 tests/cases-arbitration.py
离线：不联网、不打 8092/8903、不起 pi、不调 DeepSeek（真实探针待验证，本轮只做模拟）。
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import arbitration as arb
from harness import contract as ct
from harness import leafrun
from harness import role as role_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))


def base_decision(**kw):
    d = {
        "id": "d1", "question": "选 A 还是 B？", "context": "测试上下文",
        "options": [
            {"label": "A", "pros": ["快"], "cons": ["糙"], "cost": "low",
             "reversible": True, "evidence": []},
            {"label": "B", "pros": ["稳"], "cons": ["慢"], "cost": "low",
             "reversible": True, "evidence": []},
        ],
        "principles_in_play": [], "red_lines": [], "irreversible": False,
        "new_precedent": False, "role_conflict": False, "leo_reserved": False,
        "confidence": 0.9,
    }
    d.update(kw)
    return d


# ---------- gate：硬上升 ---------- #
v = arb.gate(base_decision(red_lines=["付款"]))
check("gate red line -> escalate", v["action"] == "escalate" and v["card"], v)
v = arb.gate(base_decision(irreversible=True))
check("gate irreversible -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(options=[
    {"label": "A", "pros": ["x"], "cons": [], "cost": "high", "reversible": True, "evidence": []},
    {"label": "B", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []}]))
check("gate high cost -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(role_conflict=True))
check("gate role conflict -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(leo_reserved=True))
check("gate leo reserved -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(confidence=0.6))
check("gate confidence < 0.8 -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(confidence=0.9, options=[
    {"label": "A", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": [], "score": 0.50},
    {"label": "B", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": [], "score": 0.45}]))
check("gate score gap < 0.1 -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(principles_in_play=["付款流程"]))
check("gate principle hits red-line word -> escalate", v["action"] == "escalate", v)
v = arb.gate(base_decision(principles_in_play=["权限调整"]))
check("gate auth principle -> escalate", v["action"] == "escalate", v)

# ---------- gate：defer ---------- #
v = arb.gate(base_decision(options=[
    {"label": "A", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []}]))
check("gate options < 2 -> defer", v["action"] == "defer" and v["missing"], v)
v = arb.gate(base_decision(options=[
    {"label": "A", "cons": [], "cost": "low", "reversible": True, "evidence": []},
    {"label": "B", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []}]))
check("gate option missing pros -> defer", v["action"] == "defer", v)
v = arb.gate(base_decision(question=""))
check("gate empty question -> defer", v["action"] == "defer", v)
v = arb.gate(base_decision(context=""))
check("gate empty context -> defer", v["action"] == "defer", v)

# ---------- gate：decide / 新先例 ---------- #
v = arb.gate(base_decision(question="验收用哪个标准？"))
check("gate acceptance rule -> decide", v["action"] == "decide" and v["rule_hits"] == ["within-acceptance"], v)
v = arb.gate(base_decision(options=[
    {"label": "A", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []},
    {"label": "B", "pros": ["x"], "cons": [], "cost": "low", "reversible": False, "evidence": []}]))
check("gate unique safe option -> decide A", v["action"] == "decide" and v["option"] == "A", v)
v = arb.gate(base_decision(new_precedent=True, options=[
    {"label": "A", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []},
    {"label": "B", "pros": ["x"], "cons": [], "cost": "low", "reversible": True, "evidence": []}]))
check("gate new precedent (no rule) -> escalate", v["action"] == "escalate", v)
d_np = arb.decision_from_failure("env_error", "act 失败", new_precedent=True)
v_np = arb.gate(d_np)
check("gate new_precedent + rule hit -> decide (rule first)",
      v_np["action"] == "decide", v_np)
v = arb.gate(base_decision())
check("gate output contract keys",
      set(v.keys()) == {"id", "action", "option", "reason", "rule_hits", "confidence", "card", "missing"}, v.keys())

# ---------- decision_from_failure ---------- #
d = arb.decision_from_failure("env_error", "act_runner 失败", parent_id="root", node_id="root.c2.1")
check("decision_from_failure has >= 2 options", len(d["options"]) >= 2, d["options"])
check("decision_from_failure ordinary confidence 0.9", d["confidence"] == 0.9, d)
v = arb.gate(d)
check("decision_from_failure env_error -> decide local", v["action"] == "decide" and v["option"] == "A", v)
d = arb.decision_from_failure("phantom_citation", "0 工具调用却引用不存在文件", parent_id="root", node_id="root.c2.1")
v = arb.gate(d)
check("decision_from_failure phantom -> decide local (not escalate)", v["action"] == "decide", v)
d = arb.decision_from_failure("premise_falsified", "前提 P-1 被证伪", parent_id="root", node_id="root.c2.1",
                              new_precedent=True)
check("decision_from_failure premise confidence 0.6", d["confidence"] == 0.6, d)
v = arb.gate(d)
check("decision_from_failure premise + new_precedent -> escalate", v["action"] == "escalate" and v["card"], v)
d = arb.decision_from_failure("timeout", "单叶超时", parent_id="root", node_id="root.c2.1", auth_affected=True)
check("decision_from_failure auth -> red line", "权限" in d["red_lines"], d)
v = arb.gate(d)
check("decision_from_failure auth -> escalate", v["action"] == "escalate", v)

# ---------- advisor_for ---------- #
check("advisor_for auth -> judge",
      arb.advisor_for({"auth_affected": True, "options": []}) == "judge")
check("advisor_for leo_reserved -> judge",
      arb.advisor_for({"leo_reserved": True, "options": []}) == "judge")
check("advisor_for high cost -> judge",
      arb.advisor_for({"options": [{"label": "A", "cost": "high"}]}) == "judge")
check("advisor_for technical -> direction",
      arb.advisor_for({"options": [{"label": "A", "cost": "low"}]}) == "direction")

# ---------- card ---------- #
d = arb.decision_from_failure("premise_falsified", "前提 P-1 被证伪", parent_id="root",
                              node_id="root.c2.1", new_precedent=True,
                              attempted_recovery=["策略1：重查"], affected_subtree=["root", "root.c2.1"],
                              evidence=["ev-1"], existing_results=["doc/结论.md"],
                              auth_affected=True)
card = arb.build_card(d)
check("card has required fields",
      all(k in card for k in ("problem", "attempted_recovery", "affected_subtree",
                              "evidence", "existing_results", "options", "advisor",
                              "auth_affected", "content_hash")), card.keys())
check("card auth flag set", card["auth_affected"] is True, card)
card2 = arb.build_card(d)
check("card content_hash stable (ignores ts)", card["content_hash"] == card2["content_hash"], (card, card2))
text = arb.render_card_text(card)
check("card text <= 1200 chars", len(text) <= 1200, len(text))
check("card text flags auth", "auth" in text, text)
check("card text has question + options", "前提 P-1" in text and "A" in text and "B" in text, text)

# ---------- apply_advisor_result ---------- #
r = arb.apply_advisor_result(card, {"option": "B", "note": "换方向做 B"})
check("apply valid suggestion ok", r["ok"] and r["suggestion"]["option"] == "B" and r["applied"] is False, r)
r = arb.apply_advisor_result(card, {"option": "B", "note": "换方向"})
check("apply auth suggestion requires approval", r["requires_user_approval"] is True and r["applied"] is False, r)
r = arb.apply_advisor_result(card, "not a dict")
check("apply non-dict invalid", r["ok"] is False, r)
r = arb.apply_advisor_result(card, {"option": "Z", "note": "x"})
check("apply unknown option invalid", r["ok"] is False, r)
r = arb.apply_advisor_result(card, {})
check("apply empty invalid", r["ok"] is False, r)
r = arb.apply_advisor_result(card, {"verdict": {"note": "缺证据，重做", "verdict": "rework"}})
check("apply judge verdict shape ok", r["ok"] and "缺证据" in (r["suggestion"]["note"] or ""), r)
plain_card = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.1"))
r = arb.apply_advisor_result(plain_card, {"option": "A", "note": "本地重试"})
check("apply non-auth no approval needed", r["ok"] and r["requires_user_approval"] is False, r)

# ---------- escalate：mock 传输 ---------- #
def fake_ok(card, advisor):
    fake_ok.calls.append((card.get("id"), advisor))
    return {"option": "B", "note": "外部建议：换方向做 B", "confidence": 0.7}
fake_ok.calls = []

def fake_raise(card, advisor):
    raise RuntimeError("cloud down")

tmp = tempfile.mkdtemp(prefix="arb-test-")
a = arb.Arbitrator(tmp)
d = arb.decision_from_failure("premise_falsified", "前提 P-1 被证伪", node_id="root.c2.1",
                              new_precedent=True)
card = arb.build_card(d)
out = a.escalate(card, transport=fake_ok, live=False)
check("escalate live=False does not call transport",
      out["status"] == "not_sent" and fake_ok.calls == [], out)
check("escalate live=False returns local fallback", out["fallback"] == "local", out)
out = a.escalate(card, transport=None, live=True)
check("escalate live=True without transport does not call", out["status"] == "not_sent", out)
out = a.escalate(card, transport=fake_ok, live=True)
check("escalate live=True + fake transport sent",
      out["status"] == "sent" and out["suggestion"]["option"] == "B" and out["applied"] is False, out)
check("escalate sent counted once", a.escalations_sent() == 1, a.escalations_sent())
out2 = a.escalate(card, transport=fake_ok, live=True)
check("escalate idempotent replay (transport called once)",
      out2["status"] == "replayed" and len(fake_ok.calls) == 1, (out2, fake_ok.calls))
# F2 回归：预算耗尽后重放已发送卡，应返回 replayed 而非预算耗尽。
a1 = arb.Arbitrator(tempfile.mkdtemp(prefix="arb-replay-"), max_escalations=1)
card_r = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.3"))
out_r1 = a1.escalate(card_r, transport=fake_ok, live=True)
check("replay setup sent", out_r1["status"] == "sent", out_r1)
n_before = len(fake_ok.calls)
out_r2 = a1.escalate(card_r, transport=fake_ok, live=True)
check("replay after budget exhausted -> replayed (not budget)",
      out_r2["status"] == "replayed" and len(fake_ok.calls) == n_before, (out_r2, fake_ok.calls))
card3 = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.2"))
out3 = a.escalate(card3, transport=fake_raise, live=True)
check("escalate transport failure -> local fallback, no raise",
      out3["status"] == "not_sent" and "本地兜底" in (out3.get("reason") or ""), out3)
out4 = a.escalate(card3, transport=lambda c, adv: {"bogus": True}, live=True)
check("escalate invalid advisor output -> local fallback",
      out4["status"] == "not_sent" and "输出无效" in (out4.get("reason") or ""), out4)
a2 = arb.Arbitrator(tempfile.mkdtemp(prefix="arb-test-budget-"), max_escalations=1)
c1 = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.1"))
c2 = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.2"))
a2.escalate(c1, transport=fake_ok, live=True)
outb = a2.escalate(c2, transport=fake_ok, live=True)
check("escalate budget exhausted -> local fallback",
      outb["status"] == "not_sent" and "预算耗尽" in (outb.get("reason") or ""), outb)
acard = arb.build_card(arb.decision_from_failure("timeout", "超时", node_id="root.c2.3",
                                                 auth_affected=True))
outa = arb.Arbitrator(tempfile.mkdtemp(prefix="arb-test-auth-")).escalate(
    acard, transport=fake_ok, live=True)
check("escalate auth card requires approval",
      outa["status"] == "sent" and outa["requires_user_approval"] is True, outa)

# ---------- ledger ---------- #
led = arb.read_ledger(tmp)
check("ledger records gate + escalate entries",
      any(e.get("kind") == "escalate_sent" for e in led) and len(led) >= 2, led)
check("ledger already_sent", a.already_sent(card["content_hash"]) is True, a.entries)

# ---------- wiring：Scheduler 咨询仲裁器 ---------- #
TABLE = role_mod.load_roles()
sched = leafrun.Scheduler(tempfile.mkdtemp(prefix="arb-sched-"), "test-arb",
                          TABLE, "测试根上下文",
                          act_runner=lambda *args, **kwargs: "")
check("Scheduler owns arbitrator", hasattr(sched, "arbitrator") and isinstance(sched.arbitrator, arb.Arbitrator))
v = sched._consult_arbitrator("premise_falsified", "前提 P-1 被证伪", parent_id="root",
                              node_id="root.c2.1", new_precedent=True,
                              affected_subtree=["root", "root.c2.1"], evidence=["ev-1"])
check("Scheduler consult premise -> escalate verdict",
      isinstance(v, dict) and v.get("action") == "escalate", v)
led2 = arb.read_ledger(str(sched.run_dir))
check("Scheduler consult writes arbitration ledger",
      any(e.get("kind") == "gate" for e in led2)
      and any(e.get("kind") == "escalate_not_sent" for e in led2), led2)
v2 = sched._consult_arbitrator("env_error", "act_runner 失败", parent_id="root", node_id="root.c2.1")
check("Scheduler consult ordinary fault -> decide local",
      isinstance(v2, dict) and v2.get("action") == "decide", v2)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % "; ".join(FAIL))
    sys.exit(1)
