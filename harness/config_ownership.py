"""配置归属表落地为代码常量（v2.1 Part B / v2 §4）。

把 v2 §4.1 的六类配置归属表 + §4.2 的魔鬼数字清单落地为只读常量/映射。

★ 只做常量定义，不改任何现有配置加载逻辑，不读任何配置文件，不产生
  任何运行时行为。OBSERVED_* 里的数值是 v2 §4.2 实测记录的当前观测值
  （占位/文档性质），不是本模块加载或生效的配置；真正部署配置版本
  锁定在 Phase C/D（V2-09），本轮不做。

六类配置（v2 §4.1）：
  ModelProfile        —— 模型能力（L2 模型方）
  DeploymentProfile   —— 模型部署资源（L2 部署方）
  RuntimeProfile      —— Runtime 参数（L3 Runtime Adapter）
  ExecutionPolicy     —— 执行策略（L5 Harness）
  HostRuntimeCapacity —— 主机运行时容量（L4 LLAAT）
  ServiceDiscovery    —— 服务发现（L4 + L3）

§4.2.8 的 8 项“未找到，疑似只存在于设计稿”单列在 NOT_FOUND_IN_CODE，
调用方不得把它们当作已定位的配置值使用。
"""
from __future__ import annotations

CONFIG_CLASSES = (
    "ModelProfile",
    "DeploymentProfile",
    "RuntimeProfile",
    "ExecutionPolicy",
    "HostRuntimeCapacity",
    "ServiceDiscovery",
)

# 字段归属（v2 §4.1 逐行表）。值：字段名 -> 所属层（owner）。
FIELD_OWNERSHIP = {
    "ModelProfile": {
        # 模型 ID、context_limit、tokenizer、工具/JSON 支持、response format、
        # 输出上限、thinking 开关；不得含部署地址、端口、KV 大小、并发数。
        "model_id": "L2 模型方",
        "context_limit": "L2 模型方",
        "tokenizer": "L2 模型方",
        "supports_tools": "L2 模型方",
        "supports_json": "L2 模型方",
        "response_format": "L2 模型方",
        "max_output_tokens": "L2 模型方",
        "thinking_switch": "L2 模型方",
        "model_structure": "L2 模型方",  # 层数/头数/hidden/full_attention_interval
        "max_tokens_range": "L2 模型方",  # 提交校验 1..1000000（API 边界）
        "tools_limit": "L2 模型方",  # tools <=64 条，JSON <=128000 字节
        "context_window_declared": "L2 模型方",  # pi models.json 声明值
    },
    "DeploymentProfile": {
        # Sage 后端地址、local model 实际 ID、量化、KV 算法、--max-num-seqs、
        # 163840 硬上限、TOKEN_BUDGET、PREFILL_LANES、prefill 系数、reserve、
        # pressure multiplier；不得含 Role、Prompt、Runtime CLI 参数。
        "backend_url": "L2 部署方",
        "local_model_id": "L2 部署方",
        "quantization": "L2 部署方",
        "kv_algorithm": "L2 部署方",
        "max_num_seqs": "L2 部署方",
        "max_tokens_server": "L2 部署方",
        "max_kv_size": "L2 部署方",
        "kv_bits": "L2 部署方",
        "prefill_step_size": "L2 部署方",
        "token_budget": "L2 部署方",
        "token_per_char": "L2 部署方",
        "prefill_lanes": "L2 部署方",
        "prefill_ms_per_token": "L2 部署方",
        "prefill_buffer_s": "L2 部署方",
        "memory_reserve_bytes": "L2 部署方",
        "pressure_multiplier": "L2 部署方",
        "capacity_tiers": "L2 部署方",  # sage 档 total/serial/parallel
        "deployment_generation": "L2 部署方",  # V2-11：模型实例指纹
    },
    "RuntimeProfile": {
        # Pi/CC 的 CLI 参数、配置目录、沙箱、串行锁、--max-turns、模型别名
        # 映射、agent 文件格式；不得含模型能力声明、部署容量。
        "pi_argv": "L3 Runtime Adapter",
        "pi_agent_dir": "L3 Runtime Adapter",
        "pi_sandbox": "L3 Runtime Adapter",
        "pi_serial_lock": "L3 Runtime Adapter",
        "cc_max_turns": "L3 Runtime Adapter",
        "cc_model_alias": "L3 Runtime Adapter",
        "cc_config_dir": "L3 Runtime Adapter",
        "cc_agent_file_format": "L3 Runtime Adapter",
        "kind_agent_argv": "L3 Runtime Adapter",
        "kind_agent_route_guard": "L3 Runtime Adapter",
        "models_json_mapping": "L3 Runtime Adapter",
    },
    "ExecutionPolicy": {
        # 超时策略、重试策略、权限策略、ctx_budget ladder、max_tokens 档位、
        # 工具调用上限；不得含模型部署地址、Runtime CLI 细节。
        "leaf_timeout_s": "L5 Harness",
        "task_timeout_s": "L5 Harness",
        "timeout_min_s": "L5 Harness",
        "timeout_max_s": "L5 Harness",
        "agent_default_timeout_s": "L5 Harness",
        "retry_max": "L5 Harness",
        "ctx_budget_default": "L5 Harness",
        "ctx_budget_ladder": "L5 Harness",
        "ctx_budget_ceiling": "L5 Harness",
        "max_tokens_tiers": "L5 Harness",
        "max_tool_calls": "L5 Harness",
        "permission_policy": "L5 Harness",
        "poll_timeout_s": "L5 Harness",
    },
    "HostRuntimeCapacity": {
        # _workers、Session/Call 独立 executor 配额、STALL_RESTART_AFTER、
        # PARALLEL_MAX_CHARS、并发环境变量上限口；不得含 Role、Prompt、模型 id。
        "workers": "L4 LLAAT",
        "workers_wish": "L4 LLAAT",
        "session_executor_quota": "L4 LLAAT",
        "model_call_executor_quota": "L4 LLAAT",
        "stall_restart_after": "L4 LLAAT",
        "parallel_max_chars": "L4 LLAAT",
        "concurrency_env_caps": "L4 LLAAT",
    },
    "ServiceDiscovery": {
        # LLAAT/适配器/ccswitch/Sage/8091 的地址与端口；不得硬编码在业务
        # 代码里；地址变更只改此处。
        "llaat_url": "L4 LLAAT + L3",
        "adapter_pi_url": "L4 LLAAT + L3",
        "adapter_cc_url": "L4 LLAAT + L3",
        "ccswitch_url": "L4 LLAAT + L3",
        "sage_url": "L4 LLAAT + L3",
        "legacy_8091_url": "L4 LLAAT + L3",
    },
}

# 每类不得含的内容（v2 §4.1 “不得含”列）。
MUST_NOT_CONTAIN = {
    "ModelProfile": ("部署地址", "端口", "KV 大小", "并发数"),
    "DeploymentProfile": ("Role", "Prompt", "Runtime CLI 参数"),
    "RuntimeProfile": ("模型能力声明", "部署容量"),
    "ExecutionPolicy": ("模型部署地址", "Runtime CLI 细节"),
    "HostRuntimeCapacity": ("Role", "Prompt", "模型 id"),
    "ServiceDiscovery": ("硬编码在业务代码里的地址",),
}

# v2 §4.2 实测记录的当前观测值（占位/文档性质；本模块不加载、不生效）。
OBSERVED_MODEL_DEFAULTS = {
    "context_limit": 163840,  # 服务端实测 effective_context_limit
    "max_output_tokens_server": 16384,  # Sage 启动 --max-tokens
    "max_output_tokens_pi_declared": 8192,  # pi models.json maxTokens
    "context_window_pi_declared": 114688,  # pi models.json（与服务端不一致，待 Phase D 处理）
    "max_tokens_range": (1, 1000000),  # queue.py:1039 提交校验
    "tools_limit_count": 64,
    "tools_limit_json_bytes": 128000,
    "ctx_budget_default": 32768,
    "ctx_budget_ladder": (8192, 16384, 32768, 65536),
    "ctx_budget_ceiling": 65536,
}

OBSERVED_DEPLOYMENT_DEFAULTS = {
    "token_budget": 245760,  # = 6 路 x (32768+8192)
    "token_per_char": 1.5,
    "prefill_lanes": 2,
    "prefill_ms_per_token": 2.0,  # 实测 2.028，2.0 偏小（B11），Phase A 重标定
    "prefill_buffer_s": 2.0,
    "memory_reserve_bytes": 2 * 1024 ** 3,  # TREE_MEMORY_RESERVE_BYTES
    "max_num_seqs": 8,  # 仅存于现网 Sage 进程命令行；8->6 由实测定（D-6）
    "max_tokens_server": 16384,
    "max_kv_size": 163840,
    "kv_bits": 8,
    "prefill_step_size": 512,
    "capacity_sage_tier": {"total_max": 6, "serial_max": 2, "parallel_max": 6},
    "capacity_default_tier": {"total": 1, "serial": 1, "parallel": 0},
}

OBSERVED_EXECUTION_DEFAULTS = {
    "leaf_timeout_s": 300.0,
    "cc_timeout_default_s": 300.0,
    "task_timeout_s": 900,
    "timeout_min_s": 20,
    "timeout_max_s": 1800,
    "agent_default_timeout_s": 1800,
    "adapter_poll_timeout_s": 300.0,
    "adapter_poll_interval_s": 1.0,
    "max_tool_calls": 5,
}

OBSERVED_HOST_CAPACITY_DEFAULTS = {
    "workers": 8,  # _apply_capacity 写死；构造器初值 1；WORKERS_WISH 缺省 4
    "workers_wish_default": 4,
    "stall_restart_after": 3,
    "parallel_max_chars": 4000,
}

OBSERVED_SERVICE_ENDPOINTS = {
    "llaat_url": "http://127.0.0.1:8903",
    "adapter_pi_url": "http://127.0.0.1:8904/v1",
    "ccswitch_url": "http://127.0.0.1:15721",
    "sage_url": "http://127.0.0.1:8092/v1",
    "legacy_8091_url": "http://127.0.0.1:8091/v1",
    # 适配器（CC，8905）：v2 §4.2.7 未找到，疑似只存在于设计稿，见 NOT_FOUND_IN_CODE。
    "adapter_cc_url": None,
}

# v2 §4.2.8：8 项“未找到，疑似只存在于设计稿”——不得当作已定位的配置值。
NOT_FOUND_IN_CODE = (
    "tokenizer",
    "session_timeout_360s",
    "poll_timeout_420s",
    "timed_runner_450s",
    "agent_session_max",
    "session_call_independent_executors",
    "adapter_cc_8905",
    "max_num_seqs_config_file",
)

# v2 §4.3：统一配置入口的 revision 字段名（Phase C/D 才开始约束生产流量，V2-09）。
CONFIG_REVISION_FIELD = "config_revision"


def fields_of(config_class):
    """某类配置的字段归属映射（字段名 -> 所属层）。未知类抛 KeyError。"""
    return dict(FIELD_OWNERSHIP[config_class])


def owner_of(field_name):
    """字段名 -> 所属配置类；不在归属表内返回 None。"""
    for cls, fields in FIELD_OWNERSHIP.items():
        if field_name in fields:
            return cls
    return None


def all_fields():
    """全部六类配置的字段名集合。"""
    out = set()
    for fields in FIELD_OWNERSHIP.values():
        out.update(fields)
    return out
