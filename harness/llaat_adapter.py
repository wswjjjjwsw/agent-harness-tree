"""LLAAT 同步适配器：把 pi 的 OpenAI-completions 调用翻译成 LLAAT 入队请求（V2.1 收尾缺口 A）。

为什么需要它：pi 只会同步调 OpenAI 协议的模型端点（baseUrl），而 LLAAT 是异步队列
（POST /submit -> 轮询 GET /task/<id>），且只接受 kind="tree" 的结构化请求。
本模块是“最薄代理”：不重写 LLAAT 调度、不碰模型服务，只做协议翻译 + 准入。

红线（写进代码，不靠自觉）：
  * 预算：prompt 估算 + max_tokens > 163840（服务端 max-kv-size）⇒ 400 拒收，绝不提交。
  * tree 提交长度门（第二轮 C）：messages + tools 总字符数 > 100000（与 LLAAT text 路径
    `_reject` 同口径）⇒ 400 拒收，零模型推理。生产 LLAAT 的 `_submit_tree` 本身无此门，
    门放在本适配器（提交前量长度）；生产 queue.py 一字不动。
  * 单请求会话预算（第二轮 D）：prompt 估算 token > 32768（autoconfig session_token_limit）
    ⇒ 400 拒收。校准：integ-q01 失控的 3 次 merge 重试估算为 36,278 / 46,668 / 57,057，
    其余四轮最高 32,000——该门恰好只拦失控增长，不拦正常轮次。
  * 不自动重试：LLAAT 拒收/任务失败 ⇒ 原样返回 502/504，不重新提交。
    pi 若重试，重试请求同样走本适配器 -> LLAAT，不存在绕行。
  * 模型 id：pi 传的 model 串（bonsai//...）与服务端服役 id 不同，LLAAT 会判
    “active model changed”。本模块每次从 LLAAT /health 取服役 model id 再提交。
  * 只用标准库；无状态（可多进程、可被杀后由 session.ensure_adapter 重拉）。

跑法：python3 harness/llaat_adapter.py（端口 TREE_LLAAT_ADAPTER_PORT，缺省 8904；
LLAAT 地址 TREE_LLAAT_URL，缺省 http://127.0.0.1:8903）。
健康检查：GET /health（/v1/health 同义）。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_LLAAT_URL = os.environ.get("TREE_LLAAT_URL") or "http://127.0.0.1:8903"
DEFAULT_PORT = int(os.environ.get("TREE_LLAAT_ADAPTER_PORT") or 8904)
# 服务端单请求上限（max-kv-size）：prompt + max_tokens 不许超过它。
MAX_REQUEST_TOKENS = 163840
# tree 提交长度门上限（字符数，与生产 queue.py 的 LLAAT_MAX_PROMPT_CHARS 同名环境变量；
# 生产默认值 48000、实测 env=100000；本适配器缺省 100000，与 text 路径实测的
# "prompt 100001 字超过单会话上限 100000"同一口径）。
try:
    MAX_TREE_PROMPT_CHARS = int(os.environ.get("LLAAT_MAX_PROMPT_CHARS") or 100000)
except ValueError:
    MAX_TREE_PROMPT_CHARS = 100000
# 单请求会话预算（token 估算，autoconfig session_token_limit=32768）：prompt 估算
# 超过它 ⇒ 拒收（第二轮 D；见模块 docstring 校准）。
SESSION_TOKEN_LIMIT = 32768
# token 估算口径：与 LLAAT est_prompt_tokens / harness/submit.estimate_tokens 同一口径。
CHARS_PER_TOKEN = 1.5
# 轮询间隔与默认超时（与叶硬上限 300s 对齐）。
POLL_INTERVAL_S = 1.0
DEFAULT_POLL_TIMEOUT_S = 300.0
# LLAAT tree 任务的 tier：后台档（与树任务一致）。
TREE_TIER = "background"
# LLAAT /submit 只接受的 request 键（白名单，见 queue.py _submit_tree）。
ALLOWED_REQUEST_KEYS = ("model", "messages", "max_tokens", "temperature",
                        "response_format", "chat_template_kwargs", "tools", "tool_choice")


def estimate_tokens(text: str) -> int:
    """token 估算：ceil(len(text) / 1.5)，与 LLAAT 同口径。"""
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def _message_chars(msg: dict) -> int:
    """一条 message 的字符数（content 可能是 str 或 parts 列表）。"""
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for part in c:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                n += len(part["text"])
        return n
    return 0


def _http_json(method: str, url: str, payload: dict = None, timeout: float = 10.0):
    """小工具：JSON 请求，返回 (status, parsed)。HTTPError 也返回码 + 体，不抛。"""
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error": raw}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - 连不上/超时 => 调用方按 502 处理
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def served_model_id(llaat_url: str, timeout: float = 5.0) -> str:
    """从 LLAAT /health 取当前服役模型 id（tree_backend.model）。拿不到 ⇒ 抛。"""
    status, body = _http_json("GET", llaat_url.rstrip("/") + "/health", timeout=timeout)
    if status != 200:
        raise RuntimeError("LLAAT /health 不通（%s）：%s" % (status, body))
    model = ((body or {}).get("tree_backend") or {}).get("model")
    if not model:
        raise RuntimeError("LLAAT /health 未报告服役模型：%s" % (body,))
    return str(model)


def tree_prompt_chars(request: dict) -> int:
    """tree 请求的总字符数（messages + tools JSON），与 LLAAT text 路径量 prompt
    同口径（F4：tools 的 JSON 字符也计入）。"""
    req = request or {}
    total = 0
    for m in (req.get("messages") or []):
        if isinstance(m, dict):
            total += _message_chars(m)
    tools = req.get("tools")
    if tools is not None:
        total += len(json.dumps(tools, ensure_ascii=False))
    return total


def check_tree_length(request: dict) -> str:
    """tree 提交长度门：总字符数 > 上限 ⇒ 拒收原因（空串 = 通过）。零模型推理
    （调用方在 POST /submit 之前检查，绝不建任务）。"""
    n = tree_prompt_chars(request)
    if n > MAX_TREE_PROMPT_CHARS:
        return ("prompt %d 字超过单会话上限 %d（tree 提交长度门，与 text 路径同口径），拒收"
                % (n, MAX_TREE_PROMPT_CHARS))
    return ""


def submit_tree(llaat_url: str, request: dict, timeout_s: float = 300.0):
    """POST /submit kind=tree。@returns (task_id, rejection)。拒收时 task_id 为空。"""
    _len_why = check_tree_length(request)
    if _len_why:
        return "", _len_why
    body = {"kind": "tree", "client_key": uuid.uuid4().hex,
            "request": request, "tier": TREE_TIER, "tags": {"src": "tree/adapter"},
            "timeout_s": float(timeout_s)}
    status, resp = _http_json("POST", llaat_url.rstrip("/") + "/submit", body, timeout=30.0)
    if status in (200, 202):
        tid = str((resp or {}).get("id") or "")
        if tid:
            return tid, ""
        return "", "LLAAT /submit 未返回任务 id：%s" % (resp,)
    return "", "LLAAT 拒收（%s）：%s" % (status, (resp or {}).get("rejected") or resp)


def poll_task(llaat_url: str, task_id: str, timeout_s: float = DEFAULT_POLL_TIMEOUT_S) -> dict:
    """轮询 GET /task/<id> 直到 done/failed。超时 ⇒ TimeoutError；失败 ⇒ RuntimeError。"""
    deadline = time.time() + float(timeout_s)
    last = {}
    while True:
        status, body = _http_json("GET", llaat_url.rstrip("/") + "/task/" + str(task_id),
                                  timeout=10.0)
        if status == 200 and isinstance(body, dict):
            last = body
            state = str(body.get("state") or "")
            if state == "done":
                return body
            if state == "failed":
                raise RuntimeError("LLAAT 任务失败：%s" % (body.get("error") or body))
        if time.time() >= deadline:
            raise TimeoutError("等 LLAAT 任务 %s 超时 %.0fs（最后状态 %s）"
                               % (task_id, timeout_s, (last or {}).get("state")))
        time.sleep(POLL_INTERVAL_S)


def _usage_from(task: dict, prompt_tokens: int) -> dict:
    """usage：优先 LLAAT 回的 tree_response.usage，否则按估算补。"""
    u = (((task or {}).get("tree_response") or {}).get("usage") or {})
    if isinstance(u, dict) and u.get("total_tokens"):
        return {"prompt_tokens": int(u.get("prompt_tokens") or prompt_tokens),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("total_tokens") or 0)}
    text = str((task or {}).get("text") or "")
    comp = estimate_tokens(text)
    return {"prompt_tokens": int(prompt_tokens), "completion_tokens": comp,
            "total_tokens": int(prompt_tokens) + comp}


def openai_response(task: dict, model: str, prompt_tokens: int) -> dict:
    """把 LLAAT 任务结果包成 OpenAI chat.completion 形状。
    优先用 tree_response.choices[0].message 原文（含 tool_calls），否则用 text 拼。"""
    choices = (((task or {}).get("tree_response") or {}).get("choices") or [])
    msg = None
    finish = "stop"
    if choices and isinstance(choices[0], dict):
        m = choices[0].get("message") or {}
        if isinstance(m, dict) and (m.get("content") is not None or m.get("tool_calls")):
            msg = {"role": str(m.get("role") or "assistant")}
            if m.get("content") is not None:
                msg["content"] = m.get("content")
            if m.get("tool_calls"):
                msg["tool_calls"] = m.get("tool_calls")
                finish = "tool_calls"
            elif choices[0].get("finish_reason"):
                finish = str(choices[0]["finish_reason"])
    if msg is None:
        msg = {"role": "assistant", "content": str((task or {}).get("text") or "")}
    return {"id": "chatcmpl-" + str((task or {}).get("id") or uuid.uuid4().hex[:8]),
            "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": _usage_from(task, prompt_tokens)}


def _extract_messages(body: dict):
    """pi 请求体 -> messages 列表。无 messages 但有 prompt 字符串时包成一条 user 消息。"""
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        return msgs, ""
    prompt = body.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return [{"role": "user", "content": prompt}], ""
    return None, "请求里没有 messages（且无 prompt 字符串）"


def _log(msg: str):
    """适配器日志（stdout/stderr -> ensure_adapter 指定的 log 文件）。只记关键行。"""
    print("[llaat-adapter %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def handle_chat_completions(body: dict, llaat_url: str,
                            poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S):
    """一次 pi 模型调用 -> LLAAT 入队 -> 等结果 -> OpenAI 形状。
    @returns (http_status, response_dict, is_stream)。"""
    messages, why = _extract_messages(body)
    if messages is None:
        return 400, {"error": {"message": why, "type": "invalid_request"}}, False
    max_tokens = body.get("max_tokens")
    try:
        max_tokens = int(max_tokens) if max_tokens is not None else 2048
    except (TypeError, ValueError):
        return 400, {"error": {"message": "max_tokens 不是整数：%r" % (max_tokens,), "type": "invalid_request"}}, False
    if max_tokens <= 0:
        return 400, {"error": {"message": "max_tokens 必须 > 0", "type": "invalid_request"}}, False
    total_chars = sum(_message_chars(m) for m in messages if isinstance(m, dict))
    # F4：服务端口径（queue.py task_prompt_tokens）把 tools 的 JSON 字符也计入
    # prompt token；只计 messages 会放行“服务端超限”的请求（队列里挂到 300s 才 504）。
    _tools = body.get("tools")
    if _tools is not None:
        total_chars += len(json.dumps(_tools, ensure_ascii=False))
    prompt_tokens = int(math.ceil(total_chars / CHARS_PER_TOKEN)) if total_chars else 0
    # ★ 第二轮 C：tree 提交长度门（与 text 路径同口径）——100,001 字 ⇒ 400，零模型推理。
    if total_chars > MAX_TREE_PROMPT_CHARS:
        _log("length gate reject: prompt %d chars > %d" % (total_chars, MAX_TREE_PROMPT_CHARS))
        return 400, {"error": {"message": "prompt %d 字超过单会话上限 %d（tree 提交长度门，与 text 路径同口径），拒收"
                                          % (total_chars, MAX_TREE_PROMPT_CHARS), "type": "invalid_request"}}, False
    # ★ 第二轮 D：单请求会话预算——prompt 估算 > 32768 ⇒ 400 拒收（不建任务、不烧模型）。
    if prompt_tokens > SESSION_TOKEN_LIMIT:
        _log("session budget reject: prompt_tokens~%d > %d" % (prompt_tokens, SESSION_TOKEN_LIMIT))
        return 400, {"error": {"message": "单请求超会话预算：prompt 估算 %d > %d（session_token_limit），拒收"
                                          % (prompt_tokens, SESSION_TOKEN_LIMIT), "type": "invalid_request"}}, False
    # ★ 预算硬闸：prompt + max_tokens > 163840 ⇒ 拒收，绝不提交。
    if prompt_tokens + max_tokens > MAX_REQUEST_TOKENS:
        return 400, {"error": {"message": "单请求超服务端上限：prompt 估算 %d + max_tokens %d = %d > %d（max-kv-size），拒收"
                                          % (prompt_tokens, max_tokens, prompt_tokens + max_tokens,
                                             MAX_REQUEST_TOKENS), "type": "invalid_request"}}, False
    try:
        model = served_model_id(llaat_url)
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": {"message": "取服役模型失败：%s" % exc, "type": "upstream_error"}}, False
    request = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if isinstance(body.get("temperature"), (int, float)):
        request["temperature"] = float(body["temperature"])
    for key in ("response_format", "tools", "tool_choice"):
        if body.get(key) is not None:
            request[key] = body[key]
    ctk = body.get("chat_template_kwargs")
    if ctk == {"enable_thinking": False}:
        request["chat_template_kwargs"] = ctk
    _log("submit kind=tree model=%s prompt_tokens~%d max_tokens=%d stream=%s"
         % (model, prompt_tokens, max_tokens, bool(body.get("stream"))))
    tid, rej = submit_tree(llaat_url, request, timeout_s=poll_timeout_s)
    if not tid:
        # ★ LLAAT 拒收 ⇒ 502 原样返回，不重新提交（不自动重试）。
        _log("LLAAT reject: %s" % rej)
        return 502, {"error": {"message": rej, "type": "upstream_rejected"}}, False
    _log("submitted task=%s, polling" % tid)
    try:
        task = poll_task(llaat_url, tid, timeout_s=poll_timeout_s)
    except TimeoutError as exc:
        _log("poll timeout task=%s" % tid)
        return 504, {"error": {"message": str(exc), "type": "upstream_timeout"}}, False
    except Exception as exc:  # noqa: BLE001
        _log("poll error task=%s: %s" % (tid, exc))
        return 502, {"error": {"message": str(exc), "type": "upstream_error"}}, False
    _log("task %s done, text_chars=%d" % (tid, len(str(task.get("text") or ""))))
    resp = openai_response(task, model, prompt_tokens)
    # pi 等 agent 常以 stream:true 请求并按 SSE 解析；若它要流式，给它一段 SSE
    #（单块全文 + [DONE]），否则它会等流关闭而挂住。非流式请求照常回 JSON。
    if body.get("stream"):
        return 200, resp, True
    return 200, resp, False


def create_server(port: int, llaat_url: str,
                  poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S) -> ThreadingHTTPServer:
    """建一个适配器 HTTP 服务（不启动）。LLAAT 地址与轮询超时绑在 handler 上。"""
    url = str(llaat_url or DEFAULT_LLAAT_URL).rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # noqa: ANN002 - 静默，不污染日志
            pass

        def _send(self, code: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/health", "/v1/health"):
                self._send(200, {"ok": True, "llaat_url": url})
            elif path in ("/v1/models", "/models"):
                # pi 等客户端启动时会拉模型列表做校验；没有它会 404 后挂住。
                try:
                    model = served_model_id(url)
                except Exception as exc:  # noqa: BLE001
                    self._send(502, {"error": {"message": "取服役模型失败：%s" % exc, "type": "upstream_error"}})
                    return
                self._send(200, {"object": "list",
                                 "data": [{"id": model, "object": "model",
                                           "created": int(time.time()), "owned_by": "llaat"}]})
            else:
                self._send(404, {"error": "认不出的路径：%s（有 /health、GET /v1/models、POST /v1/chat/completions）" % self.path})

        def _send_sse(self, resp: dict, legacy: bool):
            """把已完成的回复包成一段 SSE（单块全文 + [DONE]）。"""
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish = choice.get("finish_reason") or "stop"
            if legacy:
                chunk1 = {"id": resp.get("id"), "object": "text_completion", "created": resp.get("created"),
                          "model": resp.get("model"),
                          "choices": [{"index": 0, "text": msg.get("content") or "", "finish_reason": None}]}
                chunk2 = {"id": resp.get("id"), "object": "text_completion", "created": resp.get("created"),
                          "model": resp.get("model"),
                          "choices": [{"index": 0, "text": "", "finish_reason": finish}]}
            else:
                delta = {"role": "assistant", "content": msg.get("content") or ""}
                if msg.get("tool_calls"):
                    delta["tool_calls"] = msg["tool_calls"]
                chunk1 = {"id": resp.get("id"), "object": "chat.completion.chunk", "created": resp.get("created"),
                          "model": resp.get("model"),
                          "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                chunk2 = {"id": resp.get("id"), "object": "chat.completion.chunk", "created": resp.get("created"),
                          "model": resp.get("model"),
                          "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            payload = ("data: %s\n\n" % json.dumps(chunk1, ensure_ascii=False)
                       + "data: %s\n\n" % json.dumps(chunk2, ensure_ascii=False)
                       + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            # pi 的 SSE 客户端读到 EOF 才认为流结束；keep-alive 下它会一直等更多块。
            # 发完 [DONE] 即关连接，让它看到流结束。
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            legacy = path in ("/v1/completions", "/completions")
            if path not in ("/v1/chat/completions", "/v1/completions",
                            "/chat/completions", "/completions"):
                self._send(404, {"error": "认不出的路径：%s" % path})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                self._send(400, {"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request"}})
                return
            _log("POST %s keys=%s stream=%s model=%s"
                 % (path, sorted(body.keys()), bool(body.get("stream")), body.get("model")))
            code, resp, is_stream = handle_chat_completions(body, url, poll_timeout_s=poll_timeout_s)
            _log("-> %s stream=%s" % (code, is_stream))
            if code == 200 and is_stream:
                self._send_sse(resp, legacy)
                return
            if code == 200 and legacy:
                # 旧版 completions 端点要 text_completion 形状（choices[0].text）。
                msg = ((resp.get("choices") or [{}])[0].get("message") or {})
                self._send(200, {"id": resp.get("id"), "object": "text_completion",
                                 "created": resp.get("created"), "model": resp.get("model"),
                                 "choices": [{"index": 0, "text": msg.get("content") or "",
                                             "finish_reason": "stop"}],
                                 "usage": resp.get("usage")})
                return
            self._send(code, resp)

    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    srv.daemon_threads = True
    return srv


def main() -> int:
    port = int(os.environ.get("TREE_LLAAT_ADAPTER_PORT") or DEFAULT_PORT)
    llaat_url = os.environ.get("TREE_LLAAT_URL") or DEFAULT_LLAAT_URL
    poll_timeout = float(os.environ.get("TREE_LLAAT_ADAPTER_TIMEOUT") or DEFAULT_POLL_TIMEOUT_S)
    srv = create_server(port, llaat_url, poll_timeout_s=poll_timeout)
    print("[llaat-adapter] listening 127.0.0.1:%d -> LLAAT %s (poll_timeout %.0fs)"
          % (port, llaat_url, poll_timeout), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
