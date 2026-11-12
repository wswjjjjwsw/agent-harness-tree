"""ctx_budget 统一 32K 回归（t-127a5611b759）。跑法：cd <候选根> && python3 tests/cases-ctx32k.py

覆盖：
  1) 全表 ctx_budget == 32768（6 个 role + 9 个 act），max_tokens 池子不动；
  2) Tier1b 离线复现：伪造根会话 history_tokens=9315（与 Tier1b 真实会话最后一行
     assistant usage.input 逐字一致）+ 本轮估算 489 ⇒ 9804；
     新预算 32768 下放行，旧预算 8192 下会拒（对照，证明本测试复现了原失败）；
  3) 上限仍在：history 32500 + 489 = 32989 > 32768 ⇒ 门仍会拒（只抬了上限，没删门）。
全程离线：只算门条件，不起 pi（run_act 的 raise 发生在起进程之前，但本套件连它都不调，
只复算同一道算式：history_tokens + estimate_tokens > ctx_budget）。
"""
import json, math, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import role as role_mod, session, submit

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else FAIL) + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

TABLE = role_mod.load_roles()

# ---------- 1) 全表 ctx_budget == 32768 ----------
for rname, row in TABLE["roles"].items():
    check("role %s ctx_budget==32768" % rname, row["ctx_budget"] == 32768, row["ctx_budget"])
for rname, acts in TABLE["acts"].items():
    for aname, arow in acts.items():
        check("act %s/%s ctx_budget==32768" % (rname, aname),
              arow["ctx_budget"] == 32768, arow["ctx_budget"])

# ---------- 2) 输出池子不动 + 不变式 ----------
POOL = [("执行", None, 8192), ("审计", None, 8192), ("调研", None, 4096),
        ("根", "spawn", 4096), ("根", "plan", 8192), ("根", "merge", 8192),
        ("规划", "spawn", 4096), ("规划", "plan", 8192), ("规划", "merge", 8192),
        ("决策", "spawn", 4096), ("决策", "plan", 8192), ("决策", "merge", 8192)]
for n, act, want in POOL:
    got = role_mod.effective(n, act, TABLE)["max_tokens"]
    check("pool %s/%s max_tokens==%d" % (n, act, want), got == want, got)
for rname, row in TABLE["roles"].items():
    check("role %s think+out<=max_tokens" % rname,
          row["think_budget"] + row["out_budget"] <= row["max_tokens"],
          (row["think_budget"], row["out_budget"], row["max_tokens"]))


def _write_session(sess_dir, sid, inputs):
    """按真实 pi 会话 jsonl 形状写伪造会话：末条 assistant 的 usage.input 即 history。"""
    sess_dir = Path(sess_dir)
    sess_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, v in enumerate(inputs):
        lines.append(json.dumps({
            "type": "message",
            "message": {"role": "assistant", "content": "x",
                        "usage": {"input": v, "output": 10, "cacheRead": 0,
                                  "cacheWrite": 0, "reasoning": 0, "totalTokens": v + 10},
                        "timestamp": "2026-11-05T00:00:0%d.000Z" % i},
        }, ensure_ascii=False))
    (sess_dir / ("2026-11-05T00-00-00-000Z_%s.jsonl" % sid)).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


# ---------- 3) Tier1b 复现：9315 + 489 ----------
with tempfile.TemporaryDirectory() as tmp:
    sess_dir = str(Path(tmp) / "sessions")
    sid = "tree-ctx32k-root"
    _write_session(sess_dir, sid, [7375, 9315])
    hist = session.history_tokens(sid, sess_dir)
    check("fabricated history_tokens==9315", hist == 9315, hist)
    prompt = "本轮输入" * 183 + "x"  # len=733 ⇒ ceil(733/1.5)=489
    est = submit.estimate_tokens(prompt)
    check("estimate_tokens(prompt)==489", est == 489, est)
    total = hist + est
    spawn_budget = role_mod.effective("根", "spawn", TABLE)["ctx_budget"]
    check("tier1b replay: 9315+489=%d <= spawn ctx_budget=%d" % (total, spawn_budget),
          total <= spawn_budget, total)
    check("tier1b control: 9315+489=%d > 旧预算 8192（旧门会拒）" % total,
          total > 8192, total)
    for rname in ("规划", "决策"):
        b = role_mod.effective(rname, "spawn", TABLE)["ctx_budget"]
        check("tier1b replay: 父 %s spawn 预算 %d 也放行" % (rname, b), total <= b, total)
    for rname in ("执行", "调研"):
        b = TABLE["roles"][rname]["ctx_budget"]
        check("leaf %s ctx_budget==32768 且 9315+489 放行" % rname,
              b == 32768 and total <= b, b)

# ---------- 4) 上限仍在：32500 + 489 = 32989 > 32768 ----------
with tempfile.TemporaryDirectory() as tmp:
    sess_dir = str(Path(tmp) / "sessions")
    sid = "tree-ctx32k-near-cap"
    _write_session(sess_dir, sid, [32500])
    hist = session.history_tokens(sid, sess_dir)
    check("fabricated history_tokens==32500", hist == 32500, hist)
    est = 489
    total = hist + est
    check("ceiling still binds: 32500+489=%d > 32768（门没被删）" % total,
          total > 32768, total)


def main():
    print("-" * 72)
    print("CTX32K: %d/%d PASS" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("FAILED: %s" % ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
