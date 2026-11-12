"""V2.2 role tools 机制冒烟（t-1d3f11824339，第一步：机制平移，不做新设计）。
验证：① 当前行为平移不变（执行写、调研只读、父/审计 --no-tools）；
② 机制：改 role.yaml 一行（临时表），叶工具跟着变，不动代码；
③ 非法工具名加载即拒。
跑法：cd <候选根> && python3 tests/smoke-v22-role-tools.py（离线，不跑 pi）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harness import role as role_mod, session as session_mod

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:200]) if extra and not cond else ""))

TABLE = role_mod.load_roles()
def argv(role, act="leaf", table=None):
    knobs = role_mod.effective(role, None, table=table or TABLE)
    return session_mod.build_argv({"id": "root.c2.1", "role": role}, act, knobs, "prompt", "sid")

# 1. 当前行为平移不变（值是现状的平移，未做新设计）。
a = argv("执行")
check("smoke exec leaf gets write tools",
      "--tools" in a and a[a.index("--tools") + 1] == "read,bash,grep,find,ls,write", a)
a = argv("调研")
check("smoke research leaf read-only",
      "--tools" in a and a[a.index("--tools") + 1] == "read,bash,grep,find,ls", a)
for _r in ("根", "规划", "决策", "审计"):
    a = argv(_r)
    check("smoke %s leaf --no-tools" % _r, "--no-tools" in a and "--tools" not in a, a)
a = argv("根", act="merge")
check("smoke parent merge --no-tools", "--no-tools" in a and "--tools" not in a, a)

# 2. 机制：改 role.yaml 一行，叶工具跟着变（不改代码）。
_src = (ROOT / "role.yaml").read_text(encoding="utf-8")
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "role.yaml"
    mod = _src.replace('tools: "read,bash,grep,find,ls"', 'tools: "read"')
    assert mod != _src, "调研 tools 行未找到"
    p.write_text(mod, encoding="utf-8")
    t2 = role_mod.load_roles(str(p))
    a2 = argv("调研", table=t2)
    check("smoke changing one role.yaml line changes leaf tools",
          "--tools" in a2 and a2[a2.index("--tools") + 1] == "read", a2)
    mod2 = _src.replace('tools: "read,bash,grep,find,ls,write"', 'tools: ""')
    assert mod2 != _src, "执行 tools 行未找到"
    p.write_text(mod2, encoding="utf-8")
    t3 = role_mod.load_roles(str(p))
    a3 = argv("执行", table=t3)
    check("smoke emptying exec tools gives --no-tools",
          "--no-tools" in a3 and "--tools" not in a3, a3)
    bad = _src.replace('tools: "read,bash,grep,find,ls"', 'tools: "read,fly"')
    p.write_text(bad, encoding="utf-8")
    raised = None
    try:
        role_mod.load_roles(str(p))
    except ValueError as exc:
        raised = str(exc)
    check("smoke illegal tool name rejected at load",
          raised is not None and "fly" in raised, raised)

print("SMOKE: %d/%d PASS" % (len(PASS), len(PASS) + len(FAIL)))
if FAIL:
    print("FAILURES: " + ", ".join(FAIL))
    sys.exit(1)
