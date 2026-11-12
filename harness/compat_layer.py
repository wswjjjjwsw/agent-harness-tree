"""兼容层骨架：旧 kind=tree 提交 -> 新 Session/Call 的映射（v2.1 Part C）。

★ 兼容层本身须受闸控，不得绕过资源闸（V2-06 / Part C C.1 各 Phase 行）：
  本模块只做纯函数映射（旧字段 -> 新 SessionSubmission + ModelCall），
  不接 LLAAT、不发请求、不创建任务、不产生流量。映射出的对象仍须经
  LLAAT admission、授权/权限校验和 lifecycle 才能执行；兼容 API 的
  存在不等于绕过路径。

去重（V2-03 / B.3 / B08 修复）：messages / tools 只在 ModelCall 里出现
一次。旧提交里与 request.messages 重复的 execution.prompt（以及
request 内残留的 prompt 字符串）一律丢弃，不抄进 SessionSubmission，
丢弃情况记在返回的 metadata 里。

旧 kind=tree 形状（实测，harness/llaat_adapter.py submit_tree）：
  {"kind": "tree", "client_key": <uuid hex>, "request": {"model", "messages",
   "max_tokens", "temperature"?, "response_format"?, "tools"?,
   "tool_choice"?, "chat_template_kwargs"?}, "tier"?, "tags": {"src"?},
   "timeout_s"?}
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta

from harness import execution_spec as spec_mod

# 兼容层闸控声明：每次映射都在 metadata 里原样带上，调用方不得剥离。
GATE_REQUIREMENT = (
    "兼容层本身须受闸控，不得绕过资源闸：映射出的 SessionSubmission / "
    "ModelCall 仍须经 LLAAT admission、授权/权限校验和 lifecycle 才能执行。"
)

# token 估算口径与 LLAAT/适配器一致：ceil(字符数 / 1.5)（llaat_adapter.py）。
CHARS_PER_TOKEN = 1.5

# 旧 kind=tree 提交没有 Session 主机内存估算：记 0 并在 metadata 里明示缺失，
# 不得编一个估算值冒充实测。
COMPAT_UNKNOWN_HOST_MEMORY_BYTES = 0

# 旧提交缺省 ctx_budget（role 意图缺省，v2 §4.2.1：全表统一 32768）。
COMPAT_DEFAULT_CTX_BUDGET = 32768


class CompatError(ValueError):
    """旧提交无法映射（kind 不符、缺必填、消息为空等）时抛出。"""


def _estimate_tokens(messages):
    chars = 0
    for m in messages:
        if isinstance(m, dict):
            chars += len(str(m.get("content") or ""))
    return int(math.ceil(chars / CHARS_PER_TOKEN)) if chars else 0


def _iso_add_seconds(iso_text, seconds):
    text = iso_text[:-1] + "+00:00" if iso_text.endswith("Z") else iso_text
    dt = datetime.fromisoformat(text) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def map_tree_submission(old, compiled_spec, runtime_id,
                        model_deployment_id, deployment_generation,
                        workspace_ref, staging_dir,
                        queue_wait_deadline_at, execution_deadline_s,
                        attempt_id=None, trace=None,
                        commit_mode="atomic_publish"):
    """旧 kind=tree 提交 -> (SessionSubmission, ModelCall, metadata)。

    参数：
      old: 旧提交 dict（见模块 docstring 的形状）。
      compiled_spec: CompiledExecutionSpec（须先由 L5 编译并通过 hash 校验）。
      runtime_id / model_deployment_id / deployment_generation：新提交的
        部署标识（deployment_generation 由调用方传入当前实例指纹）。
      workspace_ref / staging_dir / commit_mode：工作区字段（V2-05）。
      queue_wait_deadline_at: ISO8601；execution_deadline_s: 秒。
      attempt_id: 不传则新分配 uuid。trace: 不传则生成最小 trace。
    返回 (SessionSubmission, ModelCall, metadata dict)。
    映射只做字段搬运与去重，不做任何网络调用。
    """
    if not isinstance(old, dict):
        raise CompatError("旧提交必须是 dict，实际为 %r" % type(old))
    if old.get("kind") != "tree":
        raise CompatError("兼容层只映射 kind=tree 的旧提交，实际 kind=%r"
                          % (old.get("kind"),))
    request = old.get("request")
    if not isinstance(request, dict):
        raise CompatError("旧提交缺 request dict")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CompatError("旧提交 request.messages 必须是非空列表")

    # --- 去重（B08）：messages/tools 只进 ModelCall，不抄进 SessionSubmission。
    dropped_duplicate_prompt = False
    execution = old.get("execution")
    if isinstance(execution, dict) and "prompt" in execution:
        dropped_duplicate_prompt = True
    dropped_request_prompt = "prompt" in request
    if dropped_request_prompt and isinstance(request.get("prompt"), str):
        # request.prompt 与 messages 是同一份 prompt 的两种写法，只留 messages。
        pass

    if attempt_id is None:
        attempt_id = uuid.uuid4().hex
    idempotency_key = old.get("client_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise CompatError("旧提交缺 client_key（用作 idempotency_key）")
    # client_key 须符合新幂等 key 格式，否则映射失败（不在这里静默改写）。
    spec_mod.validate_idempotency_key(idempotency_key)

    if not isinstance(compiled_spec, spec_mod.CompiledExecutionSpec):
        raise CompatError("compiled_spec 必须是 CompiledExecutionSpec")
    compiled_spec.verify_hash()

    max_tokens = request.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) \
            or max_tokens <= 0:
        raise CompatError("request.max_tokens 必须是正整数，实际为 %r"
                          % (max_tokens,))
    est_prompt_tokens = _estimate_tokens(messages)
    resource_hints = {
        "ctx_budget": COMPAT_DEFAULT_CTX_BUDGET,
        "max_tokens": max_tokens,
        "est_prompt_tokens": est_prompt_tokens,
        "host_memory_bytes": COMPAT_UNKNOWN_HOST_MEMORY_BYTES,
    }
    if trace is None:
        trace = {"trace_id": uuid.uuid4().hex,
                 "span_id": uuid.uuid4().hex[:16],
                 "tenant": "compat-layer"}
    session_trace = {"trace_id": trace.get("trace_id", uuid.uuid4().hex),
                     "span_id": trace.get("span_id", uuid.uuid4().hex[:16]),
                     "tenant": trace.get("tenant", "compat-layer")}
    call_trace = {"trace_id": session_trace["trace_id"],
                  "span_id": session_trace["span_id"],
                  "call_id": uuid.uuid4().hex}

    submission = spec_mod.SessionSubmission(
        spec_ref="immutable://spec/" + compiled_spec.spec_hash,
        spec_hash=compiled_spec.spec_hash,
        runtime_id=runtime_id,
        model_deployment_id=model_deployment_id,
        deployment_generation=deployment_generation,
        attempt_id=attempt_id,
        idempotency_key=idempotency_key,
        queue_wait_deadline_at=queue_wait_deadline_at,
        execution_deadline_s=execution_deadline_s,
        workspace_ref=workspace_ref,
        staging_dir=staging_dir,
        commit_mode=commit_mode,
        resource_hints=resource_hints,
        trace=session_trace,
    )

    # 旧 kind=tree 调用不隶属任何 Session：走 standalone_call（V2-07），
    # 调用方身份取自旧 tags.src（无则用 compat-layer 缺省）；配额引用须由
    # L4 注册表校验，兼容层不虚构授权。
    tags = old.get("tags") if isinstance(old.get("tags"), dict) else {}
    src = tags.get("src") or "compat-layer:kind-tree"
    call = spec_mod.ModelCall(
        identity={"kind": "standalone_call",
                  "caller_identity": "compat-layer:%s" % src,
                  "caller_quota_ref": "compat-layer-quota:%s" % src},
        model_deployment_id=model_deployment_id,
        deployment_generation=deployment_generation,
        messages=[dict(m) for m in messages],
        tools=list(request["tools"]) if isinstance(request.get("tools"),
                                                   list) else None,
        sampling=_build_sampling(request, max_tokens),
        deadline_at=_iso_add_seconds(queue_wait_deadline_at,
                                     execution_deadline_s),
        trace=call_trace,
    )

    # 部署指纹（V2-11）：映射不伪造 generation——调用方传入的值原样带上，
    # L4 在 dispatch 时比对，不一致即暂停/失败/重绑定。
    metadata = {
        "source_kind": "tree",
        "dropped_duplicate_prompt": dropped_duplicate_prompt,
        "dropped_request_prompt": dropped_request_prompt,
        "host_memory_estimate_missing": True,
        "deployment_generation_passthrough": True,
        "deployment_generation": deployment_generation,
        "gate": GATE_REQUIREMENT,
    }
    return submission, call, metadata


def _build_sampling(request, max_tokens):
    sampling = {"max_tokens": max_tokens}
    if isinstance(request.get("temperature"), (int, float)):
        sampling["temperature"] = float(request["temperature"])
    if isinstance(request.get("response_format"), dict):
        sampling["response_format"] = request["response_format"]
    return sampling
