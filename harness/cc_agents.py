"""CC agent 文件生成器：role.yaml（唯一事实来源）→ .claude/agents/tree-<role>.md。

第 1 步研究（/tmp/cc-role-customization-report.md §6.1）的接线实现。
原则：
- role.yaml 仍是唯一事实来源；agent 文件是生成物，不是手写配置。
  每次跑树之前（run 开始时）由驱动器调一次生成器；role.yaml 或 prompts
  变了 ⇒ agent 文件跟着变（生成物不手改）。
- 每个 role 一个文件：name = "tree-" + 拼音；frontmatter 写 name /
  description / tools / model / color；正文 = prompts/<role>.md 全文逐字。
- 工具映射（pi → CC，写死在生成器常量）：
  read→Read、bash→Bash、grep/find/ls→Bash（CC 2.1.231 没有 Grep/Glob，
  pi 的 grep/find/ls 统一走 Bash）；空串 → []。
- ★ write 不映射（doc 统领）：prompts/执行.md 明写“你不是文件系统操作者：
  正文放 doc，驱动器负责落盘（模型不拿文件系统权限）”。CC 的 Write/Edit
  工具不给叶节点，避免模型直接写文件造成 doc 与文件两份真值、以及 prose
  里编造工具动作（第 2 步 minor 项 4）。叶要改文件时用 `edits` 字段作
  JSON 描述（驱动器记录/应用），不直接拿文件系统权限。
  （第 1 步手写 agent 文件曾给执行 [Read, Bash, Write, Edit]，与 role_prompt
  的“模型不拿文件系统权限”矛盾；生成器以 role_prompt 合同为准，予以纠正。
  若 Leo 日后要给 CC 叶写文件权限，改 PI_TO_CC_TOOLS 一行即可。）
- ★ (a) 全部 role 不调工具（2026-11-06 重测后定，见下）：CC role 不调工具，
  模型只出 JSON，驱动器做工具活。重测（/tmp/cc-role-retest-15721-report.md）
  6 role 各 1 次真实 Sage 调用、全部走 ccswitch 15721：无工具 role 4/4 PASS
  （根/规划/决策/审计，进程正常退出 rc=0）；用工具的 role 0/2 FAIL（执行/
  调研各调 3 次 Bash、工具结果全对，但始终没产出最终 JSON，120s timeout
  杀掉 rc=124）。同一 agent、同一 prompt、同一路由，根因调查时调研成功过
  一次（rc=0、49s、最终 JSON）——**工具 role 在 15721 上是非确定性的**，
  有时收敛、有时停滞；实验代理 framing 修复解决的是确定性停滞，工具 role
  不收敛是另一个模型侧问题，修代理修不掉。**接线不要指望工具 role 每次都
  收敛。** 故生成器缺省 no_tools=True：全部 role 的 agent 文件 tools=[]，
  不管 role.yaml 的 tools 旋钮写什么（role.yaml 仍是 pi 路径的事实来源；
  CC 路径为稳妥起见覆写为无工具）。PI_TO_CC_TOOLS 映射表保留在代码里，
  是工具 role 可靠之后（或 Leo 明确要求）再开工具的路径：no_tools=False
  时按映射表给工具。工具 role 开启后须配 harness/cc_run.py 的硬兜底（工具调用
  次数上限 MAX_TOOL_CALLS + deadline），超限即判失败、不许空转到超时。
- model = "sonnet"（常量；经会话的 ANTHROPIC_DEFAULT_SONNET_MODEL 解析到
  Sage；ccswitch 15721 代理侧再按 provider 模型名覆盖）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

# role 名 → agent 名拼音（写死在一处常量，不要现拼）。
ROLE_PINYIN = {
    "根": "gen",
    "规划": "guihua",
    "决策": "juece",
    "执行": "zhixing",
    "调研": "diaoyan",
    "审计": "shenji",
}

# agent 颜色（纯展示用，TUI 里区分 role）。
ROLE_COLOR = {
    "根": "red",
    "规划": "blue",
    "决策": "purple",
    "执行": "green",
    "调研": "yellow",
    "审计": "orange",
}

# CC 会话模型别名（常量；真实 Sage id 由代理/provider 侧映射）。
CC_MODEL = "sonnet"

# pi 工具 → CC 工具映射（写死在生成器常量；值 None = 该 pi 工具不映射到 CC）。
# Grep/Glob 在 CC 2.1.231 里不存在（第 1 步实测：CC 自己把 6 个工具滤成 4 个）。
PI_TO_CC_TOOLS = {
    "read": "Read",
    "bash": "Bash",
    "grep": "Bash",
    "find": "Bash",
    "ls": "Bash",
    "write": "Write",  # V1（路线 B）：叶直接写文件；Edit 由 cc_tools_for_role 配对补上。
}

# (a) V1（路线 B，2026-11-07）：开工具。False => 按 PI_TO_CC_TOOLS 映射给工具
# （role.yaml 仍是唯一事实来源；执行 [Read,Bash,Write,Edit]、调研 [Read,Bash]，
# 根/规划/决策/审计 tools 为空串 => 仍为 []）。硬兜底在 harness/cc_run.py
CC_NO_TOOLS_DEFAULT = False


def agent_name(role: str) -> str:
    """role 名 → agent 名（tree-<拼音>）。未知 role ⇒ 响亮报错，不猜拼音。"""
    try:
        return "tree-" + ROLE_PINYIN[str(role)]
    except KeyError:
        raise KeyError("role %r 没有拼音映射（只有 %s）" % (role, sorted(ROLE_PINYIN)))


def agent_filename(role: str) -> str:
    """role 名 → agent 文件名（tree-<拼音>.md）。"""
    return agent_name(role) + ".md"


def cc_tools_for_role(knobs: dict, no_tools: bool = CC_NO_TOOLS_DEFAULT) -> list:
    """knobs["tools"]（pi allowlist，逗号分隔）→ CC 工具列表（去重保序）。

    no_tools=True（缺省，(a)）：一律返回 []，不管 role.yaml 的 tools 旋钮写什么
    （重测：工具 role 在 15721 上非确定性收敛，0/2 FAIL；无工具 role 4/4 PASS）。
    no_tools=False：按 PI_TO_CC_TOOLS 映射（工具 role 可靠之后再开，须配
    harness/cc_run.py 的硬兜底 MAX_TOOL_CALLS + deadline）。
    空串 ⇒ []（无工具 role）。非法 pi 工具名 ⇒ 响亮报错（role.py 加载期已
    校验过，这里是第二道防线）。write 不映射（doc 统领），不报错、只跳过。
    """
    if no_tools:
        return []
    raw = str((knobs or {}).get("tools") or "").strip()
    if not raw:
        return []
    out: list = []
    for tok in raw.split(","):
        name = tok.strip()
        if not name:
            raise ValueError("tools 旋钮含空段：%r" % raw)
        if name not in PI_TO_CC_TOOLS:
            raise ValueError("pi 工具 %r 不在 CC 映射表里（只有 %s）"
                             % (name, sorted(PI_TO_CC_TOOLS)))
        mapped = PI_TO_CC_TOOLS[name]
        if mapped is not None and mapped not in out:
            out.append(mapped)
    # V1：Write 配对 Edit（同文件先写后改；只给 Write 不给 Edit 会让叶改不了自己写错的文件）。
    if "Write" in out and "Edit" not in out:
        out.append("Edit")
    return out


def _description(role: str, prompt_text: str) -> str:
    """agent 的 description（一句话，供 CC 派发选型时读）。

    取 role_prompt 正文第一句（去 markdown 标记，截 80 字），前缀 role 名。
    --agent 方式下 description 不参与选型，仅作标识与审计。
    """
    text = (prompt_text or "").strip()
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = line
        break
    first = re.sub(r"[*_`]", "", first).strip()
    if len(first) > 80:
        first = first[:80] + "…"
    return "逻辑树%s节点：%s" % (role, first) if first else "逻辑树%s节点" % role


def render_agent(role: str, knobs: dict, prompt_text: str,
                 no_tools: bool = CC_NO_TOOLS_DEFAULT) -> str:
    """一个 role 的 agent 文件全文（frontmatter + 正文逐字）。

    正文 = prompts/<role>.md 全文，一字不改。frontmatter 由 knobs 派生。
    @param no_tools: True（缺省，(a)）⇒ frontmatter tools=[]。
    """
    name = agent_name(role)
    tools = cc_tools_for_role(knobs, no_tools=no_tools)
    color = ROLE_COLOR.get(str(role), "blue")
    desc = _description(role, prompt_text)
    tools_yaml = "[" + ", ".join(tools) + "]" if tools else "[]"
    front = (
        "---\n"
        "name: %s\n"
        "description: %s\n"
        "model: %s\n"
        "tools: %s\n"
        "color: %s\n"
        "---\n"
        % (name, desc, CC_MODEL, tools_yaml, color)
    )
    # 校验 frontmatter 可解析、name 与文件名一致（生成即自检）。
    parsed = yaml.safe_load(front.strip("-\n").strip())
    if not isinstance(parsed, dict) or parsed.get("name") != name:
        raise ValueError("agent %r 的 frontmatter 生成后校验失败：%r" % (name, parsed))
    if parsed.get("tools") != tools:
        raise ValueError("agent %r 的 tools 生成后校验失败：%r != %r"
                         % (name, parsed.get("tools"), tools))
    body = (prompt_text or "").strip("\n")
    return front + "\n" + body + "\n"


def generate_agents(table: dict, role_yaml_path: str, out_dir: str,
                    no_tools: bool = CC_NO_TOOLS_DEFAULT,
                    ack_leaf: bool = False) -> dict:
    """从 role 表生成全部 agent 文件。

    @param table: role_mod.load_roles() 的返回值（roles 映射）。
    @param role_yaml_path: role.yaml 的路径（role_prompt 相对它解析）。
    @param out_dir: 输出目录（通常是 <run_dir>/.claude/agents）。
    @param no_tools: True（缺省，(a)）⇒ 全部 role 的 agent 文件 tools=[]；
                     False ⇒ 按 PI_TO_CC_TOOLS 映射（工具 role 可靠之后再开）。
    @param ack_leaf: True（V2）⇒ 执行 role 的 agent 正文改读 prompts/执行-ack.md
                     （只做真实工作 + 最小 ACK，不写 doc/1500 字说明）；其余 role
                     与 pi 路径一字不动。
    @returns: {role: {"file": str, "tools": [...], "body_chars": int}}。
    @raises: role_prompt 文件缺失、未知 role、非法 tools ⇒ 响亮报错。
    """
    base = Path(role_yaml_path).resolve().parent
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    roles = (table or {}).get("roles") or {}
    if not roles:
        raise ValueError("role 表里没有 roles：%s" % role_yaml_path)
    report: dict = {}
    for role, knobs in roles.items():
        prompt_rel = str((knobs or {}).get("role_prompt") or "").strip()
        if ack_leaf and str(role) == "执行":
            # V2（ACK 叶）：CC 执行叶不读 pi 的 prompts/执行.md（doc≥1500 的旧契约），
            # 改读 prompts/执行-ack.md。pi 路径一字不动。
            prompt_rel = "prompts/执行-ack.md"
        if not prompt_rel:
            raise ValueError("role %r 没有 role_prompt 旋钮" % role)
        prompt_path = (base / prompt_rel).resolve()
        if not prompt_path.is_file():
            raise ValueError("role %r 的 role_prompt 文件不存在：%s" % (role, prompt_path))
        prompt_text = prompt_path.read_text(encoding="utf-8")
        text = render_agent(role, knobs, prompt_text, no_tools=no_tools)
        target = out / agent_filename(role)
        target.write_text(text, encoding="utf-8")
        report[str(role)] = {
            "file": str(target),
            "tools": cc_tools_for_role(knobs, no_tools=no_tools),
            "body_chars": len(prompt_text),
        }
    return report


def generate_for_run(table: dict, role_yaml_path: str, run_dir: str,
                     no_tools: bool = CC_NO_TOOLS_DEFAULT,
                     ack_leaf: bool = False) -> dict:
    """给一次 run 生成 agent 文件：<run_dir>/.claude/agents/（随 run 隔离）。

    不动 ~/.claude/agents/（Leo 的 62 个存量 agent），不动生产树。
    @param no_tools: True（缺省，(a)）⇒ 全部 role 的 agent 文件 tools=[]。
    @param ack_leaf: True（V2）⇒ 执行 role 改读 prompts/执行-ack.md；环境变量
                     TREE_CC_ACK_LEAF=1 同样开启（run 脚本显式传参优先）。
    @returns: 与 generate_agents 相同的报告。
    """
    if os.environ.get("TREE_CC_ACK_LEAF") == "1":
        ack_leaf = True
    out_dir = Path(run_dir) / ".claude" / "agents"
    return generate_agents(table, role_yaml_path, str(out_dir),
                           no_tools=no_tools, ack_leaf=ack_leaf)
