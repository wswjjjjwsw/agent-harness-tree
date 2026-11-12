#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器长度门 + 会话预算守卫回归（第二轮 C/D，集成验收 t-132692485362）。

跑法：cd <候选根> && python3 tests/cases-adapter-gates.py
不打模型、不起服务（400 的门在取服役模型之前；502 的对照走死端口、瞬间拒连）。
两门顺序钉死在 handle_chat_completions 里：长度门（字符数）先于会话预算（token 估算）——
  1. 100,001 字 ⇒ 400，长度门文案（与 text 路径同口径）；
  2. 100,000 字 ⇒ 通过长度门（直调 check_tree_length 为空），但撞会话预算 ⇒ 400 预算文案；
  3. tools JSON 计入长度（直调：messages 99,000 字 + tools 2,000 字 ⇒ 长度门拒收）；
  4. prompt 估算 > 32768 ⇒ 400（会话预算）；恰 32768 ⇒ 通过（502 对照）；
  5. submit_tree 直调：超长 ⇒ 拒收且不发 HTTP（指到死端口仍得长度门原因）；
  6. 小请求 ⇒ 通过两门（死端口 ⇒ 502）。
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import llaat_adapter as ad

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))

DEAD = "http://127.0.0.1:1"  # 无服务端口：任何真实 HTTP 都瞬间拒连


def _body(chars, tools=None, max_tokens=2048):
    b = {"messages": [{"role": "user", "content": "x" * chars}], "max_tokens": max_tokens}
    if tools is not None:
        b["tools"] = tools
    return b


def _req(chars, tools=None):
    r = {"model": "m", "messages": [{"role": "user", "content": "x" * chars}], "max_tokens": 2048}
    if tools is not None:
        r["tools"] = tools
    return r


# ---------- 1) 100,001 字 ⇒ 400，长度门文案 ----------
code, resp, _ = ad.handle_chat_completions(_body(100001), DEAD)
check("长度门: 100,001 字 ⇒ 400", code == 400, (code, resp))
check("长度门: 文案与 text 路径同口径",
      "100001" in str(resp) and "超过单会话上限" in str(resp), resp)

# ---------- 2) 100,000 字 ⇒ 通过长度门，但撞会话预算 ----------
check("长度门: 100,000 字直调通过", ad.check_tree_length(_req(100000)) == "",
      ad.check_tree_length(_req(100000)))
check("长度门: 100,001 字直调拒收", ad.check_tree_length(_req(100001)) != "",
      ad.check_tree_length(_req(100001)))
code, resp, _ = ad.handle_chat_completions(_body(100000), DEAD)
check("会话预算: 100,000 字 ⇒ 400 预算文案", code == 400 and "会话预算" in str(resp), (code, resp))

# ---------- 3) tools JSON 计入长度 ----------
big_tools = [{"type": "function", "function": {"name": "f", "description": "y" * 2000}}]
n = ad.tree_prompt_chars(_req(99000, tools=big_tools))
check("长度门: tree_prompt_chars 含 tools", n > 100000, n)
check("长度门: messages + tools 超限 ⇒ 直调拒收",
      ad.check_tree_length(_req(99000, tools=big_tools)) != "",
      ad.check_tree_length(_req(99000, tools=big_tools)))
code, resp, _ = ad.handle_chat_completions(_body(99000, tools=big_tools), DEAD)
check("长度门: messages + tools 超限 ⇒ 400 长度文案",
      code == 400 and "超过单会话上限" in str(resp), (code, resp))

# ---------- 4) 会话预算：估算 > 32768 ⇒ 400；恰 32768 ⇒ 通过 ----------
# 估算口径 ceil(chars/1.5)：49153 字 ⇒ 32769 token；49152 字 ⇒ 32768 token。
code, resp, _ = ad.handle_chat_completions(_body(49153), DEAD)
check("会话预算: 估算 32769 ⇒ 400", code == 400, (code, resp))
check("会话预算: 文案点名 session_token_limit",
      "32768" in str(resp) or "会话预算" in str(resp), resp)
code, resp, _ = ad.handle_chat_completions(_body(49152), DEAD)
check("会话预算: 估算恰 32768 ⇒ 不拦（死端口故 502）", code == 502, (code, resp))

# ---------- 5) submit_tree 直调：超长 ⇒ 拒收且不发 HTTP ----------
tid, rej = ad.submit_tree(DEAD, _req(100001))
check("submit_tree: 超长 ⇒ 空 task_id + 长度门原因",
      tid == "" and "超过单会话上限" in rej, (tid, rej))
tid2, rej2 = ad.submit_tree(DEAD, {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                    "max_tokens": 2048})
check("submit_tree: 正常长度 ⇒ 走 HTTP（死端口故 LLAAT 拒收）",
      tid2 == "" and "LLAAT" in rej2, (tid2, rej2))

# ---------- 6) 小请求 ⇒ 通过两门 ----------
code, resp, _ = ad.handle_chat_completions(_body(100), DEAD)
check("小请求: 通过两门（死端口故 502）", code == 502, (code, resp))

print("\n== %d passed, %d failed ==" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: %s" % FAIL)
    sys.exit(1)
