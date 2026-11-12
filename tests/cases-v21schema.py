"""v2.1 schema 安全实施回归（t-v21schema）。跑法：cd <候选根> && python3 tests/cases-v21schema.py

离线：不联网、不打 8092/8903、不启动服务、不调真实模型。只测新模块：
harness/execution_spec（Part B 四类型）、harness/session_accounting（V2-13 分账）、
harness/config_ownership（v2 §4 归属表）、harness/compat_layer（Part C 兼容层骨架）。
"""
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import compat_layer as compat
from harness import config_ownership as cfg
from harness import execution_spec as spec_mod
from harness import session_accounting as acct

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" -- " + str(extra)[:300]) if extra and not cond else ""))


def raises(name, fn, extra=""):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 校验失败即预期行为
        check(name, isinstance(exc, (spec_mod.SpecValidationError, ValueError)), repr(exc))
        return
    check(name, False, "未抛错 " + str(extra))


GEN = hashlib.sha256(b"sage-a1.5-4bit-mlx|4bit|flags|2026-11-07T00:00:00Z|72433").hexdigest()
POLICY = {"tools": {"allow": ["read_file", "search"], "deny": ["write_file"]},
          "permissions": {"filesystem": "read_only", "network": "none",
                          "spawn_subagent": False},
          "max_tool_calls": 5}
INTENT = {"ctx_budget": 32768, "max_tokens": 8192,
          "est_prompt_tokens": 1200, "host_memory_bytes": 300 * 1024 * 1024}


def make_spec(**over):
    d = {"spec_id": str(uuid.uuid4()), "role_id": "research_local",
         "role_revision": "20261107-a1",
         "prompt_hash": hashlib.sha256(b"prompt").hexdigest(),
         "prompt_ref": "immutable://prompt/" + hashlib.sha256(b"prompt").hexdigest(),
         "policy_hash": hashlib.sha256(b"policy").hexdigest(),
         "policy": POLICY, "output_contract_ref": "immutable://contract/abc",
         "resource_intent": INTENT, "compiled_at": "2026-11-07T05:00:00Z",
         "compiler_version": "role-compiler-0.1"}
    d.update(over)
    return spec_mod.CompiledExecutionSpec.from_dict(d)


def make_submission(**over):
    d = {"spec_ref": "immutable://spec/" + "0" * 64,
         "spec_hash": "0" * 64, "runtime_id": "pi",
         "model_deployment_id": "sage-8092", "deployment_generation": GEN,
         "attempt_id": str(uuid.uuid4()), "idempotency_key": "test-key-001",
         "queue_wait_deadline_at": "2026-11-07T06:00:00Z",
         "execution_deadline_s": 300,
         "workspace_ref": "/tmp/llaat-iso-t05/work/run-1",
         "staging_dir": "/tmp/llaat-iso-t05/work/run-1/staging",
         "commit_mode": "atomic_publish", "resource_hints": INTENT,
         "trace": {"trace_id": "t1", "span_id": "s1", "tenant": "harness"}}
    d.update(over)
    return spec_mod.SessionSubmission.from_dict(d)


def make_call(**over):
    d = {"identity": {"kind": "standalone_call",
                      "caller_identity": "adapter-pi",
                      "caller_quota_ref": "quota-adapter-pi"},
         "model_deployment_id": "sage-8092", "deployment_generation": GEN,
         "messages": [{"role": "user", "content": "hello"}],
         "deadline_at": "2026-11-07T06:00:00Z",
         "trace": {"trace_id": "t1", "span_id": "s1", "call_id": "c1"}}
    d.update(over)
    return spec_mod.ModelCall.from_dict(d)


# ---------- B.1 CompiledExecutionSpec ----------
s = make_spec()
check("spec build ok + hash self-consistent",
      s.verify_hash() and s.spec_hash == spec_mod.compute_spec_hash(s._payload_without_hash()))
check("spec hash excludes itself",
      spec_mod.compute_spec_hash({"a": 1, "spec_hash": "x" * 64}) == spec_mod.compute_spec_hash({"a": 1}))
check("spec round-trip to_dict/from_dict",
      spec_mod.CompiledExecutionSpec.from_dict(s.to_dict()).spec_hash == s.spec_hash)
tampered = s.to_dict()
tampered["role_revision"] = "20261107-a2"
raises("spec tamper role_revision -> hash mismatch", lambda: spec_mod.CompiledExecutionSpec.from_dict(tampered))
missing = s.to_dict()
del missing["prompt_hash"]
raises("spec missing prompt_hash raises", lambda: spec_mod.CompiledExecutionSpec.from_dict(missing))
bad_policy = s.to_dict()
bad_policy["policy"] = dict(POLICY)
bad_policy["policy"]["permissions"] = dict(POLICY["permissions"])
bad_policy["policy"]["permissions"]["filesystem"] = "write_all"
raises("spec bad filesystem enum raises", lambda: spec_mod.CompiledExecutionSpec.from_dict(bad_policy))
bad_hash = s.to_dict()
bad_hash["prompt_hash"] = "not-a-hash"
raises("spec bad prompt_hash format raises", lambda: spec_mod.CompiledExecutionSpec.from_dict(bad_hash))

# ---------- B.2 SessionSubmission ----------
sub = make_submission()
check("submission build ok + round-trip",
      spec_mod.SessionSubmission.from_dict(sub.to_dict()).attempt_id == sub.attempt_id)
for bad_key in ("bad key!", "", "x" * 129, "key/slash"):
    raises("submission bad idempotency_key %r raises" % bad_key,
           lambda k=bad_key: make_submission(idempotency_key=k))
raises("submission bad commit_mode raises",
       lambda: make_submission(commit_mode="direct_write"))
raises("submission bad runtime_id raises",
       lambda: make_submission(runtime_id="qwen"))
raises("submission staging outside workspace raises",
       lambda: make_submission(staging_dir="/tmp/elsewhere/staging"))
in_place = make_submission(workspace_ref="in_place:/tmp/run-1",
                           staging_dir="/tmp/run-1/staging")
check("submission in_place workspace staging inside ok",
      in_place.workspace_ref == "in_place:/tmp/run-1")
raises("submission parent_session without capability raises",
       lambda: make_submission(parent_session_id="sess-1"))
raises("submission bad deployment_generation raises",
       lambda: make_submission(deployment_generation="not-a-fingerprint"))
raises("submission missing trace raises",
       lambda: make_submission(trace={"trace_id": "t1"}))
check("submission parent_session+capability ok",
      make_submission(parent_session_id="sess-1",
                      parent_capability="cap-abc").parent_capability == "cap-abc")

# ---------- B.3 ModelCall ----------
owned = make_call(identity={"kind": "session_owned_call",
                            "parent_session_id": "sess-1",
                            "parent_capability": "cap-issued-by-llaat"})
check("call session_owned with capability ok",
      owned.identity["kind"] == "session_owned_call")
check("call standalone ok",
      make_call().identity["kind"] == "standalone_call")
check("call round-trip",
      spec_mod.ModelCall.from_dict(make_call().to_dict()).messages[0]["role"] == "user")
raises("call session_owned without capability raises",
       lambda: make_call(identity={"kind": "session_owned_call",
                                   "parent_session_id": "sess-1"}))
raises("call session_owned with only parent id raises",
       lambda: make_call(identity={"kind": "session_owned_call",
                                   "parent_session_id": "sess-1",
                                   "parent_capability": ""}))
raises("call standalone without quota raises",
       lambda: make_call(identity={"kind": "standalone_call",
                                   "caller_identity": "adapter-pi"}))
raises("call mixed identities raises",
       lambda: make_call(identity={"kind": "session_owned_call",
                                   "parent_session_id": "sess-1",
                                   "parent_capability": "cap",
                                   "caller_identity": "adapter-pi",
                                   "caller_quota_ref": "q"}))
raises("call missing identity raises",
       lambda: make_call(identity={"kind": "bogus"}))
raises("call empty messages raises",
       lambda: make_call(messages=[]))
raises("call bad sampling max_tokens raises",
       lambda: make_call(sampling={"max_tokens": 0}))

# ---------- B.4 ExecutionResult + ValidatorVerdict ----------
res = spec_mod.ExecutionResult(
    session_id="sess-1", attempt_id=str(uuid.uuid4()),
    execution_status="succeeded", runtime_id="pi",
    model_deployment_id="sage-8092", deployment_generation=GEN,
    artifact_refs=["/tmp/run-1/staging/out.md"],
    usage={"prompt_tokens": 100, "completion_tokens": 50, "model_calls": 1,
           "wall_s": 12.5})
check("result succeeded ok + round-trip",
      spec_mod.ExecutionResult.from_dict(res.to_dict()).execution_status == "succeeded")
check("result to_dict has no verdict/judge_ok keys",
      "verdict" not in res.to_dict() and "judge_ok" not in res.to_dict())
raises("result failed without error_code raises",
       lambda: spec_mod.ExecutionResult(
           session_id="sess-1", attempt_id=str(uuid.uuid4()),
           execution_status="failed", runtime_id="pi",
           model_deployment_id="sage-8092", deployment_generation=GEN,
           artifact_refs=[], usage={"prompt_tokens": 0, "completion_tokens": 0,
                                    "model_calls": 0, "wall_s": 0}))
raises("result succeeded with error_code raises",
       lambda: spec_mod.ExecutionResult(
           session_id="sess-1", attempt_id=str(uuid.uuid4()),
           execution_status="succeeded", runtime_id="pi",
           model_deployment_id="sage-8092", deployment_generation=GEN,
           artifact_refs=[], usage={"prompt_tokens": 0, "completion_tokens": 0,
                                    "model_calls": 0, "wall_s": 0},
           error_code="boom"))
bad_result = res.to_dict()
bad_result["judge_ok"] = True
raises("result judge_ok rejected (B.4)",
       lambda: spec_mod.ExecutionResult.from_dict(bad_result))
verdict = spec_mod.ValidatorVerdict(verdict="accepted", reason="contract ok",
                                    session_id="sess-1")
check("validator verdict accepted separate from execution_status",
      verdict.verdict == "accepted" and res.execution_status == "succeeded")
raises("verdict bad value raises",
       lambda: spec_mod.ValidatorVerdict(verdict="maybe", reason="x"))

# ---------- V2-13 Session/Call 分账 ----------
sl, cl = acct.SessionLedger(), acct.CallLedger()
sl.record_session("s1", 300 * 1024 * 1024)
sl.record_session("s2", 200 * 1024 * 1024)
cl.record_call("c1", kv_bytes=100 * 1024 * 1024, prefill_bytes=50 * 1024 * 1024)
check("ledgers totals split host vs model",
      sl.total_host_memory_bytes() == 500 * 1024 * 1024
      and cl.total_model_memory_bytes() == 150 * 1024 * 1024)
check("memory check ok under budget",
      acct.check_memory_budget(500 * 1024 * 1024, 150 * 1024 * 1024,
                               32 * 1024 ** 3)["ok"])
check("memory check fails over budget",
      not acct.check_memory_budget(30 * 1024 ** 3, 2 * 1024 ** 3,
                                   32 * 1024 ** 3)["ok"])
check("memory check boundary equality ok",
      acct.check_memory_budget(1024, 1024, 1024 + 1024 + acct.RESERVE_BYTES_DEFAULT)["ok"])
check("reserve default is 2GB",
      acct.RESERVE_BYTES_DEFAULT == 2 * 1024 ** 3)
r = acct.admission_check(sl, cl, new_session_host_bytes=100 * 1024 * 1024,
                         available_bytes=32 * 1024 ** 3)
check("admission_check includes new session + counts",
      r["ok"] and r["running_sessions"] == 2 and r["running_calls"] == 1)
r2 = acct.admission_check(sl, cl, new_session_host_bytes=40 * 1024 ** 3,
                          available_bytes=32 * 1024 ** 3)
check("admission_check blocks oversize new session", not r2["ok"])
sl.release_session("s1")
check("release_session frees host memory",
      sl.total_host_memory_bytes() == 200 * 1024 * 1024)
raises("ledger duplicate session raises",
       lambda: sl.record_session("s2", 1))
raises("ledger negative memory raises",
       lambda: acct.check_memory_budget(-1, 0, 100))

# ---------- v2 §4 配置归属表 ----------
check("config six classes present",
      cfg.CONFIG_CLASSES == ("ModelProfile", "DeploymentProfile", "RuntimeProfile",
                             "ExecutionPolicy", "HostRuntimeCapacity",
                             "ServiceDiscovery"))
check("owner_of token_budget is DeploymentProfile",
      cfg.owner_of("token_budget") == "DeploymentProfile")
check("owner_of workers is HostRuntimeCapacity",
      cfg.owner_of("workers") == "HostRuntimeCapacity")
check("owner_of leaf_timeout_s is ExecutionPolicy",
      cfg.owner_of("leaf_timeout_s") == "ExecutionPolicy")
check("owner_of unknown field is None", cfg.owner_of("no_such_field") is None)
check("observed deployment defaults match v2 §4.2",
      cfg.OBSERVED_DEPLOYMENT_DEFAULTS["token_budget"] == 245760
      and cfg.OBSERVED_DEPLOYMENT_DEFAULTS["prefill_lanes"] == 2
      and cfg.OBSERVED_DEPLOYMENT_DEFAULTS["max_num_seqs"] == 8)
check("not-found list marks tokenizer + 8905 as design-only",
      "tokenizer" in cfg.NOT_FOUND_IN_CODE
      and "adapter_cc_8905" in cfg.NOT_FOUND_IN_CODE)
check("fields_of returns mapping",
      isinstance(cfg.fields_of("ServiceDiscovery"), dict)
      and "llaat_url" in cfg.fields_of("ServiceDiscovery"))

# ---------- Part C 兼容层骨架 ----------
old = {"kind": "tree", "client_key": uuid.uuid4().hex,
       "request": {"model": "sage-8092",
                   "messages": [{"role": "user", "content": "做调研"}],
                   "max_tokens": 2048, "temperature": 0,
                   "tools": [{"type": "function",
                              "function": {"name": "read_file"}}]},
       "execution": {"prompt": "做调研（重复的 prompt 副本）"},
       "tier": "tree", "tags": {"src": "tree/adapter"}, "timeout_s": 300}
spec_obj = make_spec()
sub2, call2, meta = compat.map_tree_submission(
    old, spec_obj, "pi", "sage-8092", GEN,
    "/tmp/llaat-iso-t05/work/run-1", "/tmp/llaat-iso-t05/work/run-1/staging",
    "2026-11-07T06:00:00Z", 300)
check("compat drops duplicate execution.prompt",
      meta["dropped_duplicate_prompt"] is True)
check("compat submission carries no messages/prompt (dedup, B.3/B08)",
      "messages" not in sub2.to_dict() and "prompt" not in sub2.to_dict()
      and sub2.spec_hash == spec_obj.spec_hash)
check("compat call carries messages+tools exactly once",
      call2.messages == old["request"]["messages"]
      and call2.tools == old["request"]["tools"])
check("compat call identity is standalone (old tree has no session)",
      call2.identity["kind"] == "standalone_call")
check("compat metadata carries gate requirement",
      "不得绕过资源闸" in meta["gate"])
check("compat idempotency_key reuses client_key",
      sub2.idempotency_key == old["client_key"])
raises("compat non-tree kind raises",
       lambda: compat.map_tree_submission(
           {"kind": "agent"}, spec_obj, "pi", "sage-8092", GEN,
           "/tmp/w", "/tmp/w/staging", "2026-11-07T06:00:00Z", 300))
raises("compat empty messages raises",
       lambda: compat.map_tree_submission(
           {"kind": "tree", "client_key": "k1", "request": {"messages": [], "max_tokens": 10}},
           spec_obj, "pi", "sage-8092", GEN,
           "/tmp/w", "/tmp/w/staging", "2026-11-07T06:00:00Z", 300))

print("\nV2.1 schema: %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
    sys.exit(1)
