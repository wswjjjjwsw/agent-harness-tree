"""v2.1 Part B 四个核心类型的 schema + 校验（纯 Python，stdlib only）。

依据：统一消息队列设计 v2.1 Part B（B.1–B.4）与 Part A
V2-02（幂等）、V2-03（spec 拆分 / spec_hash 不自引用）、V2-07（两种
Call 身份）、V2-11（deployment_generation）。

只做数据结构与校验，不接 LLAAT、不发请求、不读服务、不改生产行为。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

API_VERSION_SPEC = "execution-spec.v2.1"
API_VERSION_SUBMISSION = "session-submission.v2.1"
API_VERSION_CALL = "model-call.v2.1"
API_VERSION_RESULT = "execution-result.v2.1"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

RUNTIME_IDS = ("pi", "claude")
COMMIT_MODES = ("atomic_publish", "in_place_fenced")
FILESYSTEM_MODES = ("read_only", "read_write_scoped")
NETWORK_MODES = ("none", "allowlisted")
EXECUTION_STATUSES = ("succeeded", "failed", "cancelled", "expired")
CALL_IDENTITY_KINDS = ("session_owned_call", "standalone_call")


class SpecValidationError(ValueError):
    """任一 v2.1 schema 校验失败时抛出（缺必填、非法枚举、hash 不匹配等）。"""


# ---------------------------------------------------------------- 校验原语
def _require_str(obj, name):
    if not isinstance(obj, str) or not obj:
        raise SpecValidationError("%s 必须是非空字符串，实际为 %r" % (name, obj))
    return obj


def _require_int(obj, name, minimum=None):
    if isinstance(obj, bool) or not isinstance(obj, int):
        raise SpecValidationError("%s 必须是整数，实际为 %r" % (name, obj))
    if minimum is not None and obj < minimum:
        raise SpecValidationError("%s 必须 >= %d，实际为 %d" % (name, minimum, obj))
    return obj


def _require_enum(obj, name, allowed):
    if obj not in allowed:
        raise SpecValidationError("%s 必须是 %s 之一，实际为 %r" % (name, allowed, obj))
    return obj


def _require_sha256(obj, name):
    _require_str(obj, name)
    if not SHA256_RE.match(obj):
        raise SpecValidationError("%s 必须是 64 位小写 sha256 hex，实际为 %r" % (name, obj))
    return obj


def _require_uuid(obj, name):
    _require_str(obj, name)
    try:
        uuid.UUID(str(obj))
    except ValueError:
        raise SpecValidationError("%s 必须是 uuid，实际为 %r" % (name, obj))
    return obj


def _require_iso8601(obj, name):
    _require_str(obj, name)
    text = obj[:-1] + "+00:00" if obj.endswith("Z") else obj
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise SpecValidationError("%s 必须是 ISO8601 时间，实际为 %r" % (name, obj))
    return obj


def _require_str_list(obj, name):
    if not isinstance(obj, list) or any(not isinstance(x, str) for x in obj):
        raise SpecValidationError("%s 必须是字符串列表，实际为 %r" % (name, obj))
    return list(obj)


def _reject_unknown(data, allowed, type_name):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SpecValidationError("%s 含未知字段：%s" % (type_name, unknown))


def _require_keys(data, required, type_name):
    missing = [k for k in required if k not in data or data[k] is None]
    if missing:
        raise SpecValidationError("%s 缺必填字段：%s" % (type_name, missing))


def canonical_json(payload):
    """固定键序、无空白的 canonical JSON（供 hash 用）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_spec_hash(payload):
    """CompiledExecutionSpec 的 spec_hash：canonical payload 的 sha256。

    哈希输入 = 除 spec_hash 自身外的全部字段（V2-03：hash 不进入自身
    哈希输入，杜绝自引用）。
    """
    if not isinstance(payload, dict):
        raise SpecValidationError("spec payload 必须是 dict，实际为 %r" % type(payload))
    body = {k: v for k, v in payload.items() if k != "spec_hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def validate_idempotency_key(key):
    """幂等 key 格式校验（B.2：[A-Za-z0-9_-]{1,128}）。"""
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.match(key):
        raise SpecValidationError(
            "idempotency_key 格式非法（须为 [A-Za-z0-9_-]{1,128}），实际为 %r" % (key,))
    return key


def validate_deployment_generation(gen):
    """deployment_generation（V2-11）：模型实例指纹，sha256 hex。"""
    return _require_sha256(gen, "deployment_generation")


def _validate_policy(policy):
    if not isinstance(policy, dict):
        raise SpecValidationError("policy 必须是 dict，实际为 %r" % type(policy))
    _reject_unknown(policy, ("tools", "permissions", "max_tool_calls"), "policy")
    _require_keys(policy, ("tools", "permissions", "max_tool_calls"), "policy")
    tools = policy["tools"]
    if not isinstance(tools, dict):
        raise SpecValidationError("policy.tools 必须是 dict")
    _reject_unknown(tools, ("allow", "deny"), "policy.tools")
    _require_keys(tools, ("allow", "deny"), "policy.tools")
    _require_str_list(tools["allow"], "policy.tools.allow")
    _require_str_list(tools["deny"], "policy.tools.deny")
    perms = policy["permissions"]
    if not isinstance(perms, dict):
        raise SpecValidationError("policy.permissions 必须是 dict")
    _reject_unknown(perms, ("filesystem", "network", "spawn_subagent"),
                    "policy.permissions")
    _require_keys(perms, ("filesystem", "network", "spawn_subagent"),
                  "policy.permissions")
    _require_enum(perms["filesystem"], "policy.permissions.filesystem",
                  FILESYSTEM_MODES)
    _require_enum(perms["network"], "policy.permissions.network", NETWORK_MODES)
    if not isinstance(perms["spawn_subagent"], bool):
        raise SpecValidationError(
            "policy.permissions.spawn_subagent 必须是 bool，实际为 %r"
            % (perms["spawn_subagent"],))
    _require_int(policy["max_tool_calls"], "policy.max_tool_calls", minimum=0)
    return policy


def _validate_resource_intent(intent, name):
    if not isinstance(intent, dict):
        raise SpecValidationError("%s 必须是 dict，实际为 %r" % (name, type(intent)))
    _reject_unknown(intent, ("ctx_budget", "max_tokens", "est_prompt_tokens",
                             "host_memory_bytes"), name)
    _require_keys(intent, ("ctx_budget", "max_tokens", "est_prompt_tokens",
                           "host_memory_bytes"), name)
    for k in ("ctx_budget", "max_tokens", "est_prompt_tokens",
              "host_memory_bytes"):
        _require_int(intent[k], "%s.%s" % (name, k), minimum=0)
    return intent


def _validate_trace(trace, required_keys):
    if not isinstance(trace, dict):
        raise SpecValidationError("trace 必须是 dict，实际为 %r" % type(trace))
    _reject_unknown(trace, required_keys, "trace")
    _require_keys(trace, required_keys, "trace")
    for k in required_keys:
        _require_str(trace[k], "trace.%s" % k)
    return dict(trace)


def _staging_within_workspace(workspace_ref, staging_dir):
    """V2-05：staging_dir 须在工作区内。workspace_ref 为临时 worktree 路径，
    或 in_place:<run_dir>。"""
    _require_str(workspace_ref, "workspace_ref")
    _require_str(staging_dir, "staging_dir")
    if workspace_ref.startswith("in_place:"):
        base = workspace_ref[len("in_place:"):]
    else:
        base = workspace_ref
    base = base.rstrip("/")
    staging = staging_dir.rstrip("/")
    if staging != base and not staging.startswith(base + "/"):
        raise SpecValidationError(
            "staging_dir %r 不在 workspace_ref %r 内（V2-05）"
            % (staging_dir, workspace_ref))
    return staging_dir


# ---------------------------------------------------------------- B.1
@dataclass
class CompiledExecutionSpec:
    """B.1：L5 编译的不可变执行规格（编译期字段 only）。

    spec_hash 按 canonical payload 算（不含自身，V2-03）。构造时若不传
    spec_hash 则自动计算；若传入则重算比对，不匹配即报错。
    """

    spec_id: str
    role_id: str
    role_revision: str
    prompt_hash: str
    prompt_ref: str
    policy_hash: str
    policy: dict
    output_contract_ref: str
    resource_intent: dict
    compiled_at: str
    compiler_version: str
    spec_hash: str = None
    api_version: str = API_VERSION_SPEC

    def __post_init__(self):
        _require_enum(self.api_version, "api_version", (API_VERSION_SPEC,))
        _require_uuid(self.spec_id, "spec_id")
        _require_str(self.role_id, "role_id")
        _require_str(self.role_revision, "role_revision")
        _require_sha256(self.prompt_hash, "prompt_hash")
        _require_str(self.prompt_ref, "prompt_ref")
        _require_sha256(self.policy_hash, "policy_hash")
        _validate_policy(self.policy)
        _require_str(self.output_contract_ref, "output_contract_ref")
        _validate_resource_intent(self.resource_intent, "resource_intent")
        _require_iso8601(self.compiled_at, "compiled_at")
        _require_str(self.compiler_version, "compiler_version")
        expected = compute_spec_hash(self._payload_without_hash())
        if self.spec_hash is None:
            self.spec_hash = expected
        elif self.spec_hash != expected:
            raise SpecValidationError(
                "spec_hash 不匹配：声明 %s，重算 %s（V2-03）"
                % (self.spec_hash, expected))

    def _payload_without_hash(self):
        d = asdict(self)
        d.pop("spec_hash", None)
        return d

    def verify_hash(self):
        """重算 hash 比对（V2-03）；不匹配抛 SpecValidationError。"""
        expected = compute_spec_hash(self._payload_without_hash())
        if self.spec_hash != expected:
            raise SpecValidationError(
                "spec_hash 不匹配：声明 %s，重算 %s" % (self.spec_hash, expected))
        return True

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SpecValidationError("CompiledExecutionSpec 输入必须是 dict")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        _reject_unknown(data, allowed, "CompiledExecutionSpec")
        _require_keys(data, ("spec_id", "role_id", "role_revision",
                             "prompt_hash", "prompt_ref", "policy_hash",
                             "policy", "output_contract_ref",
                             "resource_intent", "compiled_at",
                             "compiler_version"),
                      "CompiledExecutionSpec")
        # spec_hash 可选：缺失时由构造自动按 canonical payload 计算（V2-03）；
        # 传入时重算比对，不匹配即报错。
        return cls(**{k: data[k] for k in allowed if k in data})


# ---------------------------------------------------------------- B.2
@dataclass
class SessionSubmission:
    """B.2：Session 提交信封（提交期字段 only，L4 只验信封、不读 prompt）。"""

    spec_ref: str
    spec_hash: str
    runtime_id: str
    model_deployment_id: str
    deployment_generation: str
    attempt_id: str
    idempotency_key: str
    queue_wait_deadline_at: str
    execution_deadline_s: int
    workspace_ref: str
    staging_dir: str
    commit_mode: str
    resource_hints: dict
    trace: dict
    parent_session_id: str = None
    parent_capability: str = None
    api_version: str = API_VERSION_SUBMISSION

    def __post_init__(self):
        _require_enum(self.api_version, "api_version",
                      (API_VERSION_SUBMISSION,))
        _require_str(self.spec_ref, "spec_ref")
        _require_sha256(self.spec_hash, "spec_hash")
        _require_enum(self.runtime_id, "runtime_id", RUNTIME_IDS)
        _require_str(self.model_deployment_id, "model_deployment_id")
        validate_deployment_generation(self.deployment_generation)
        _require_uuid(self.attempt_id, "attempt_id")
        validate_idempotency_key(self.idempotency_key)
        _require_iso8601(self.queue_wait_deadline_at, "queue_wait_deadline_at")
        _require_int(self.execution_deadline_s, "execution_deadline_s",
                     minimum=1)
        if self.parent_session_id is not None:
            # session_owned 的子 Session 时 parent_capability 必填（B.2 / V2-07）。
            _require_str(self.parent_session_id, "parent_session_id")
            _require_str(self.parent_capability,
                         "parent_capability（有 parent_session_id 时必填）")
        elif self.parent_capability is not None:
            raise SpecValidationError(
                "parent_capability 无 parent_session_id 时不得单独出现")
        _staging_within_workspace(self.workspace_ref, self.staging_dir)
        _require_enum(self.commit_mode, "commit_mode", COMMIT_MODES)
        _validate_resource_intent(self.resource_hints, "resource_hints")
        _validate_trace(self.trace, ("trace_id", "span_id", "tenant"))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SpecValidationError("SessionSubmission 输入必须是 dict")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        _reject_unknown(data, allowed, "SessionSubmission")
        _require_keys(data, ("spec_ref", "spec_hash", "runtime_id",
                             "model_deployment_id", "deployment_generation",
                             "attempt_id", "idempotency_key",
                             "queue_wait_deadline_at", "execution_deadline_s",
                             "workspace_ref", "staging_dir", "commit_mode",
                             "resource_hints", "trace"), "SessionSubmission")
        return cls(**{k: data[k] for k in allowed if k in data})


# ---------------------------------------------------------------- B.3
@dataclass
class ModelCall:
    """B.3：单次模型调用（L3 → L4）。两种身份二选一，不得混用，不得省略。

    session_owned_call 须带 LLAAT 签发的 parent_capability；仅靠客户端
    自报 parent_session_id 的调用一律拒绝（V2-07）。
    """

    identity: dict
    model_deployment_id: str
    deployment_generation: str
    messages: list
    deadline_at: str
    trace: dict
    tools: list = None
    sampling: dict = None
    job_type: str = "call"
    api_version: str = API_VERSION_CALL

    def __post_init__(self):
        _require_enum(self.api_version, "api_version", (API_VERSION_CALL,))
        _require_enum(self.job_type, "job_type", ("call",))
        self.identity = _validate_identity(self.identity)
        _require_str(self.model_deployment_id, "model_deployment_id")
        validate_deployment_generation(self.deployment_generation)
        if not isinstance(self.messages, list) or not self.messages:
            raise SpecValidationError("messages 必须是非空列表")
        for i, m in enumerate(self.messages):
            if not isinstance(m, dict) or not isinstance(m.get("role"), str) \
                    or "content" not in m:
                raise SpecValidationError(
                    "messages[%d] 必须是含 role/content 的 dict，实际为 %r"
                    % (i, m))
        if self.tools is not None:
            if not isinstance(self.tools, list):
                raise SpecValidationError("tools 必须是列表，实际为 %r"
                                          % (self.tools,))
        if self.sampling is not None:
            if not isinstance(self.sampling, dict):
                raise SpecValidationError("sampling 必须是 dict")
            _reject_unknown(self.sampling,
                            ("temperature", "max_tokens", "response_format"),
                            "sampling")
            if "max_tokens" in self.sampling:
                _require_int(self.sampling["max_tokens"],
                             "sampling.max_tokens", minimum=1)
            if "temperature" in self.sampling and not isinstance(
                    self.sampling["temperature"], (int, float)):
                raise SpecValidationError("sampling.temperature 必须是数字")
        _require_iso8601(self.deadline_at, "deadline_at")
        _validate_trace(self.trace, ("trace_id", "span_id", "call_id"))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SpecValidationError("ModelCall 输入必须是 dict")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        _reject_unknown(data, allowed, "ModelCall")
        _require_keys(data, ("identity", "model_deployment_id",
                             "deployment_generation", "messages",
                             "deadline_at", "trace"), "ModelCall")
        return cls(**{k: data[k] for k in allowed if k in data})


def _validate_identity(identity):
    """两种身份二选一校验（V2-07）。返回规范化后的 identity dict。"""
    if not isinstance(identity, dict):
        raise SpecValidationError("identity 必须是 dict，实际为 %r"
                                  % (type(identity),))
    kind = identity.get("kind")
    _require_enum(kind, "identity.kind", CALL_IDENTITY_KINDS)
    session_keys = ("parent_session_id", "parent_capability")
    standalone_keys = ("caller_identity", "caller_quota_ref")
    _reject_unknown(identity, ("kind",) + session_keys + standalone_keys,
                    "identity")
    if kind == "session_owned_call":
        if any(identity.get(k) is not None for k in standalone_keys):
            raise SpecValidationError(
                "session_owned_call 不得同时带 standalone 字段 %s（两种身份不得混用）"
                % (standalone_keys,))
        for k in session_keys:
            # 禁止仅靠客户端自报 parent id 做授权：无 capability 即拒绝。
            _require_str(identity.get(k),
                         "identity.%s（session_owned 时必填；无 capability "
                         "的 session_owned 调用一律拒绝，V2-07）" % k)
        return {"kind": kind,
                "parent_session_id": identity["parent_session_id"],
                "parent_capability": identity["parent_capability"]}
    # standalone_call
    if any(identity.get(k) is not None for k in session_keys):
        raise SpecValidationError(
            "standalone_call 不得同时带 session_owned 字段 %s（两种身份不得混用）"
            % (session_keys,))
    for k in standalone_keys:
        _require_str(identity.get(k),
                     "identity.%s（standalone 时必填）" % k)
    return {"kind": kind,
            "caller_identity": identity["caller_identity"],
            "caller_quota_ref": identity["caller_quota_ref"]}


# ---------------------------------------------------------------- B.4
@dataclass
class ExecutionResult:
    """B.4：执行结果（L4 → L5）。只报告执行状态，不写业务 judge。

    业务验收由 ValidatorVerdict（accepted/rejected）单独表达，与
    execution_status 分离，绝不复用 judge_ok（B.4 / B13 修复）。
    """

    session_id: str
    attempt_id: str
    execution_status: str
    runtime_id: str
    model_deployment_id: str
    deployment_generation: str
    artifact_refs: list
    usage: dict
    stdout_ref: str = None
    trace_ref: str = None
    error_code: str = None
    api_version: str = API_VERSION_RESULT

    def __post_init__(self):
        _require_enum(self.api_version, "api_version", (API_VERSION_RESULT,))
        _require_str(self.session_id, "session_id")
        _require_uuid(self.attempt_id, "attempt_id")
        _require_enum(self.execution_status, "execution_status",
                      EXECUTION_STATUSES)
        _require_enum(self.runtime_id, "runtime_id", RUNTIME_IDS)
        _require_str(self.model_deployment_id, "model_deployment_id")
        validate_deployment_generation(self.deployment_generation)
        _require_str_list(self.artifact_refs, "artifact_refs")
        if not isinstance(self.usage, dict):
            raise SpecValidationError("usage 必须是 dict")
        _reject_unknown(self.usage, ("prompt_tokens", "completion_tokens",
                                     "model_calls", "wall_s"), "usage")
        _require_keys(self.usage, ("prompt_tokens", "completion_tokens",
                                   "model_calls", "wall_s"), "usage")
        _require_int(self.usage["prompt_tokens"], "usage.prompt_tokens",
                     minimum=0)
        _require_int(self.usage["completion_tokens"],
                     "usage.completion_tokens", minimum=0)
        _require_int(self.usage["model_calls"], "usage.model_calls",
                     minimum=0)
        if not isinstance(self.usage["wall_s"], (int, float)) \
                or isinstance(self.usage["wall_s"], bool) \
                or self.usage["wall_s"] < 0:
            raise SpecValidationError("usage.wall_s 必须是非负数字")
        if self.execution_status == "failed" and not self.error_code:
            raise SpecValidationError(
                "execution_status=failed 时 error_code 必填（机器可读分类）")
        if self.execution_status == "succeeded" and self.error_code:
            raise SpecValidationError(
                "execution_status=succeeded 时不得带 error_code")
        if self.error_code is not None:
            _require_str(self.error_code, "error_code")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SpecValidationError("ExecutionResult 输入必须是 dict")
        if "judge_ok" in data:
            # B.4：旧 judge_ok 只保留为形状合规的过渡含义，L4 不得再写
            # 业务 judge 字段；新类型一律拒绝。
            raise SpecValidationError(
                "ExecutionResult 不接受 judge_ok 字段：业务验收请用 "
                "ValidatorVerdict（accepted/rejected），与 execution_status "
                "分离（B.4）")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        _reject_unknown(data, allowed, "ExecutionResult")
        _require_keys(data, ("session_id", "attempt_id", "execution_status",
                             "runtime_id", "model_deployment_id",
                             "deployment_generation", "artifact_refs",
                             "usage"), "ExecutionResult")
        return cls(**{k: data[k] for k in allowed if k in data})


@dataclass
class ValidatorVerdict:
    """L5 Validator 的业务验收（accepted/rejected），与 ExecutionResult
    的 execution_status 是两个独立字段，不得混用（B.4 / B13 修复）。"""

    verdict: str
    reason: str
    session_id: str = None
    attempt_id: str = None

    def __post_init__(self):
        _require_enum(self.verdict, "verdict", ("accepted", "rejected"))
        _require_str(self.reason, "reason")
        if self.session_id is not None:
            _require_str(self.session_id, "session_id")
        if self.attempt_id is not None:
            _require_uuid(self.attempt_id, "attempt_id")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise SpecValidationError("ValidatorVerdict 输入必须是 dict")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        _reject_unknown(data, allowed, "ValidatorVerdict")
        _require_keys(data, ("verdict", "reason"), "ValidatorVerdict")
        return cls(**{k: data[k] for k in allowed if k in data})
