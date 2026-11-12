"""Session/Call 分账的纯函数部分（v2.1 V2-13）。

Session 与 Call 分别记账、分别拥有内存账目，在同一张机器实际内存视图
下统一校验总量：

    (运行中 Session 的主机内存之和)
  + (运行中 Call 的 KV/prefill 内存之和)
  + reserve(2GB) <= 机器实际可用内存

纯函数/纯数据结构：不接 LLAAT、不读 vm_stat、不调模型。机器实际可用
内存（vm_stat 实测值）由调用方传入 —— 本模块只做算式，不采集。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# V2-13：统一校验里的固定 reserve，2GB。
RESERVE_BYTES_DEFAULT = 2 * 1024 ** 3


class AccountingError(ValueError):
    """记账输入非法（负数内存、重复记账等）时抛出。"""


def _require_nonneg_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountingError("%s 必须是非负整数，实际为 %r" % (name, value))
    return value


@dataclass
class SessionLedger:
    """Session 级记账：每个运行中 Session 的执行进程主机内存（RSS 估算）。

    只记 Session 进程的主机内存，不记 prefill lane / KV（V2-13：Session
    不占 prefill lane/KV，但进程 RSS 真实存在，不得推断为零）。
    """

    sessions: dict = field(default_factory=dict)

    def record_session(self, session_id, host_memory_bytes):
        if not isinstance(session_id, str) or not session_id:
            raise AccountingError("session_id 必须是非空字符串")
        _require_nonneg_int(host_memory_bytes, "host_memory_bytes")
        if session_id in self.sessions:
            raise AccountingError("Session %r 已记账，不得重复记" % session_id)
        self.sessions[session_id] = host_memory_bytes

    def release_session(self, session_id):
        """Session 结束时释放其主机内存记账（以进程组终止为准）。"""
        self.sessions.pop(session_id, None)

    def total_host_memory_bytes(self):
        return sum(self.sessions.values())

    def session_count(self):
        return len(self.sessions)


@dataclass
class CallLedger:
    """Call 级记账：每个运行中 Call 的 KV / prefill 内存。"""

    calls: dict = field(default_factory=dict)

    def record_call(self, call_id, kv_bytes, prefill_bytes=0):
        if not isinstance(call_id, str) or not call_id:
            raise AccountingError("call_id 必须是非空字符串")
        _require_nonneg_int(kv_bytes, "kv_bytes")
        _require_nonneg_int(prefill_bytes, "prefill_bytes")
        if call_id in self.calls:
            raise AccountingError("Call %r 已记账，不得重复记" % call_id)
        self.calls[call_id] = {"kv_bytes": kv_bytes,
                               "prefill_bytes": prefill_bytes}

    def release_call(self, call_id):
        self.calls.pop(call_id, None)

    def total_kv_bytes(self):
        return sum(c["kv_bytes"] for c in self.calls.values())

    def total_prefill_bytes(self):
        return sum(c["prefill_bytes"] for c in self.calls.values())

    def total_model_memory_bytes(self):
        """Call 的模型内存总量 = KV + prefill。"""
        return self.total_kv_bytes() + self.total_prefill_bytes()

    def call_count(self):
        return len(self.calls)


def check_memory_budget(session_host_bytes, call_model_bytes,
                        available_bytes,
                        reserve_bytes=RESERVE_BYTES_DEFAULT):
    """统一内存校验（V2-13，纯函数）。

    (Session 主机内存) + (Call KV/prefill 内存) + reserve <= 机器实际可用内存。
    available_bytes 由调用方传入（vm_stat 实测），本函数不采集。
    返回 dict：ok / 总量 / headroom 等明细；输入非法抛 AccountingError。
    """
    _require_nonneg_int(session_host_bytes, "session_host_bytes")
    _require_nonneg_int(call_model_bytes, "call_model_bytes")
    _require_nonneg_int(available_bytes, "available_bytes")
    _require_nonneg_int(reserve_bytes, "reserve_bytes")
    total = session_host_bytes + call_model_bytes + reserve_bytes
    return {
        "ok": total <= available_bytes,
        "total_bytes": total,
        "session_host_bytes": session_host_bytes,
        "call_model_bytes": call_model_bytes,
        "reserve_bytes": reserve_bytes,
        "available_bytes": available_bytes,
        "headroom_bytes": available_bytes - total,
    }


def admission_check(session_ledger, call_ledger,
                    new_session_host_bytes=0, new_call_model_bytes=0,
                    available_bytes=0,
                    reserve_bytes=RESERVE_BYTES_DEFAULT):
    """准入检查：把待准入的新 Session/Call 加进当前账目后再做统一校验。

    两个子项分开记（host_memory 与 model_memory），在同一机器视图下加总，
    避免互相挤占时无法归因（V2-13 §2.3 补记账小节）。
    """
    if not isinstance(session_ledger, SessionLedger):
        raise AccountingError("session_ledger 必须是 SessionLedger")
    if not isinstance(call_ledger, CallLedger):
        raise AccountingError("call_ledger 必须是 CallLedger")
    _require_nonneg_int(new_session_host_bytes, "new_session_host_bytes")
    _require_nonneg_int(new_call_model_bytes, "new_call_model_bytes")
    session_total = (session_ledger.total_host_memory_bytes()
                     + new_session_host_bytes)
    call_total = (call_ledger.total_model_memory_bytes()
                  + new_call_model_bytes)
    result = check_memory_budget(session_total, call_total, available_bytes,
                                 reserve_bytes)
    result["running_sessions"] = session_ledger.session_count()
    result["running_calls"] = call_ledger.call_count()
    return result
