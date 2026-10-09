"""sse.py SOLO 自定义 SSE 解析 → OpenAI SSE（流式转换 + 非流式聚合）。

对应原版 internal/upstream/solosse.go。

SOLO 事件序列（实测）：

    id:1
    event:metadata
    data:{"model":"","session_id":"...","prompt_completion_id":0,...}

    id:2
    event:timing_cost
    data:{"name":"llm_raw_chat_v2",...}

    event:output                          ← ×N，核心内容
    data:{"response":"<content 增量>",
          "reasoning_content":"<思考链增量>",
          "tool_calls":<null 或工具调用>}

    event:extra_info                       ← 含 reasoning_content 完整版
    event:token_usage
    data:{"prompt_tokens":21,"completion_tokens":142,"total_tokens":163,"reasoning_tokens":135}

    event:done
    data:{"finish_reason":"stop"}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .. import jsonutil

# ---------------------------------------------------------------------------
# 事件模型
# ---------------------------------------------------------------------------


@dataclass
class SOLOEvent:
    """单条 SOLO SSE 事件（归一化）。"""

    event: str = ""  # metadata | timing_cost | output | extra_info | token_usage | done | error
    response: str = ""  # output: content 增量
    reasoning: str = ""  # output: 思考链增量
    tool_calls: Any = None  # output: 工具调用（None 或对象/数组）
    usage: dict | None = None  # token_usage
    finish_reason: str = ""  # done
    error_code: int = 0  # error
    error_message: str = ""  # error


class SOLOStreamError(Exception):
    """上游 SSE 流内的业务错误（event:error）。

    非流式聚合时抛出，调用方可据此分类冷却账号并轮转。
    """

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"solo error code={code} msg={msg}")
        self.code = code
        self.msg = msg

    def kind(self) -> str:
        """将 SSE 流内错误分类。1005 → plan_limit；其余归 client。"""
        return "plan_limit" if self.code == 1005 else "client"


# ---------------------------------------------------------------------------
# 行解析
# ---------------------------------------------------------------------------


def parse_solo_line(event_name: str, data_line: str) -> SOLOEvent | None:
    """解析一条事件（event_name 为 event 行值，data_line 为 data 行值）。

    解析失败返回 None（对齐 Go：scanLine 丢弃无法解析的事件）。
    """
    ev = SOLOEvent(event=(event_name or "").strip())
    if data_line == "":
        return ev

    try:
        raw = json.loads(data_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return ev

    if ev.event == "output":
        value = raw.get("response")
        if isinstance(value, str):
            ev.response = value
        value = raw.get("reasoning_content")
        if isinstance(value, str):
            ev.reasoning = value
        if "tool_calls" in raw:
            ev.tool_calls = raw["tool_calls"]
    elif ev.event == "token_usage":
        ev.usage = raw
    elif ev.event == "done":
        value = raw.get("finish_reason")
        if isinstance(value, str):
            ev.finish_reason = value
    elif ev.event == "error":
        value = raw.get("code")
        if isinstance(value, (int, float)):
            ev.error_code = int(value)
        value = raw.get("message")
        if isinstance(value, str):
            ev.error_message = value
    return ev


@dataclass
class _SSEState:
    """维护一行 SSE 的 event/data 跨行累积。"""

    event: str = ""
    data: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.event = ""
        self.data.clear()

    def data_text(self) -> str:
        return "".join(self.data)


def scan_line(state: _SSEState, line: str) -> SOLOEvent | None:
    """处理一行；返回该行触发的事件（事件边界时解析并返回）。"""
    if line == "":
        if state.event == "":
            state.reset()
            return None
        ev = parse_solo_line(state.event, state.data_text())
        state.reset()
        return ev
    if line.startswith("event:"):
        state.event = line[len("event:") :].strip()
    elif line.startswith("data:"):
        # 对齐 Go：WriteString(TrimPrefix(line, "data:"))，不额外裁空格
        state.data.append(line[len("data:") :])
    elif line.startswith(":"):
        pass  # 注释行忽略
    return None


def iter_lines(chunks: Iterable[bytes], chunk_size: int = 65536) -> Iterator[str]:
    """把字节块流切成「行」，语义对齐 Go 的 bufio.ReadString('\\n')。

    - 每行不含行尾 `\\n`；调用方再 TrimRight("\\r\\n")。
    - 流结束时若缓冲区仍有残留（末尾无换行），也作为一行产出。
    """
    buf = b""
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1 :]
            yield line.decode("utf-8", errors="replace")
    if buf:
        yield buf.decode("utf-8", errors="replace")


def _trim_eol(line: str) -> str:
    """对齐 Go 的 strings.TrimRight(line, "\\r\\n")。"""
    return line.rstrip("\r\n")


# ---------------------------------------------------------------------------
# tool_call 合并
# ---------------------------------------------------------------------------


def merge_tool_call_json(
    tool_calls: dict[int, dict], tool_order: list[int], raw: Any
) -> None:
    """把 SOLO output.tool_calls（可能 None/对象/数组）合并进 tool_calls（按 index）。"""
    if raw is None:
        return
    if isinstance(raw, list):
        arr = raw
    elif isinstance(raw, dict):
        arr = [raw]
    else:
        return

    for call in arr:
        if not isinstance(call, dict):
            continue
        idx = 0
        value = call.get("index")
        if isinstance(value, (int, float)):
            idx = int(value)
        merged = tool_calls.get(idx)
        if merged is None:
            merged = {"index": idx}
            tool_calls[idx] = merged
            tool_order.append(idx)
        merge_tool_call_delta(merged, call)


def merge_tool_call_delta(merged: dict, delta: dict) -> None:
    """把流式 tool_call 片段合并到累计对象：

    id/type/function.name 直覆盖，function.arguments 拼接。
    上游 SOLO 用 `function_call` 字段（实测），OpenAI 标准用 `function`；两者都兼容。
    """
    value = delta.get("id")
    if isinstance(value, str) and value:
        merged["id"] = value
    value = delta.get("type")
    if isinstance(value, str) and value:
        merged["type"] = value

    df = delta.get("function")
    if not isinstance(df, dict):
        df = delta.get("function_call")  # SOLO 专属字段名
    if not isinstance(df, dict):
        return

    # 清理 SOLO 专属字段,只保留标准 OpenAI function 结构(name/arguments)
    df.pop("namespace", None)
    df.pop("partial_arguments", None)

    mf = merged.get("function")
    if not isinstance(mf, dict):
        mf = {}
        merged["function"] = mf
    value = df.get("name")
    if isinstance(value, str) and value:
        mf["name"] = value
    value = df.get("arguments")
    if isinstance(value, str) and value:
        prev = mf.get("arguments")
        if isinstance(prev, str) and prev:
            mf["arguments"] = prev + value
        else:
            mf["arguments"] = value


# ---------------------------------------------------------------------------
# 非流式聚合
# ---------------------------------------------------------------------------


def aggregate(chunks: Iterable[bytes]) -> dict:
    """读取完整 SOLO SSE，聚合 response + reasoning + tool_calls + usage，

    产出单个 OpenAI chat.completion（非流式）。
    遇到上游 event:error 抛 SOLOStreamError。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = "stop"
    usage: dict | None = None
    tool_calls: dict[int, dict] = {}
    tool_order: list[int] = []
    upstream_err: SOLOStreamError | None = None

    state = _SSEState()
    for line in iter_lines(chunks):
        ev = scan_line(state, _trim_eol(line))
        if ev is None:
            continue
        if ev.event == "output":
            content_parts.append(ev.response)
            reasoning_parts.append(ev.reasoning)
            merge_tool_call_json(tool_calls, tool_order, ev.tool_calls)
        elif ev.event == "token_usage":
            usage = ev.usage
        elif ev.event == "done":
            if ev.finish_reason:
                finish_reason = ev.finish_reason
        elif ev.event == "error":
            upstream_err = SOLOStreamError(ev.error_code, ev.error_message)

    if upstream_err is not None:
        raise upstream_err

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_order:
        calls = [tool_calls[idx] for idx in sorted(tool_order)]
        message["tool_calls"] = calls

    resp: dict[str, Any] = {
        "id": _new_completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _new_completion_id() -> str:
    """chatcmpl-<unixnano>（对齐 Go time.Now().UnixNano()）。"""
    return f"chatcmpl-{time.time_ns()}"


# ---------------------------------------------------------------------------
# 流式转换
# ---------------------------------------------------------------------------


def iter_openai_sse(
    chunks: Iterable[bytes],
    on_error: Callable[[SOLOStreamError], None] | None = None,
    on_usage: Callable[[dict], None] | None = None,
) -> Iterator[str]:
    """流式转换：SOLO SSE → OpenAI SSE chunk，保证至少一个 [DONE]。

    生成器逐个产出 SSE 文本块（调用方负责编码与 flush）。
    """
    completion_id = _new_completion_id()
    pending_usage: dict | None = None
    saw_done = False
    state = _SSEState()

    def make_chunk(delta: dict, finish: str) -> str:
        nonlocal pending_usage
        choice: dict[str, Any] = {"index": 0, "delta": delta}
        if finish:
            choice["finish_reason"] = finish
        chunk: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "",
            "choices": [choice],
        }
        if pending_usage is not None:
            chunk["usage"] = pending_usage
            pending_usage = None
        return "data: " + jsonutil.dumps(chunk) + "\n\n"

    for line in iter_lines(chunks):
        ev = scan_line(state, _trim_eol(line))
        if ev is None:
            continue

        if ev.event == "output":
            delta: dict[str, Any] = {}
            if ev.response:
                delta["content"] = ev.response
            if ev.reasoning:
                delta["reasoning_content"] = ev.reasoning
            if ev.tool_calls is not None and not _is_null(ev.tool_calls):
                calls = ev.tool_calls
                if isinstance(calls, dict):
                    calls = [calls]
                if isinstance(calls, list):
                    converted: list[Any] = []
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        fc = call.get("function_call")
                        if isinstance(fc, dict):
                            call["function"] = fc
                            del call["function_call"]
                        # 清理 SOLO 专属字段,只保留标准 OpenAI function 结构
                        fn = call.get("function")
                        if isinstance(fn, dict):
                            fn.pop("namespace", None)
                            fn.pop("partial_arguments", None)
                        converted.append(call)
                    delta["tool_calls"] = converted
            if delta:
                yield make_chunk(delta, "")

        elif ev.event == "token_usage":
            pending_usage = ev.usage
            if on_usage is not None and ev.usage is not None:
                on_usage(ev.usage)

        elif ev.event == "done":
            yield make_chunk({}, ev.finish_reason)
            yield "data: [DONE]\n\n"
            saw_done = True

        elif ev.event == "error":
            # 上游业务错误：回调 + 写一条 error 事件 + [DONE]。
            err = SOLOStreamError(ev.error_code, ev.error_message)
            if on_error is not None:
                on_error(err)
            msg = f"solo error code={ev.error_code} msg={ev.error_message}"
            yield "event: error\ndata: " + jsonutil.dumps(msg) + "\n\n"
            yield "data: [DONE]\n\n"
            saw_done = True

    if not saw_done:
        # 幂等兜底：上游中断（无 done）仍写 [DONE]。
        yield "data: [DONE]\n\n"


def _is_null(value: Any) -> bool:
    """对齐 Go 的 `string(ev.ToolCalls) != "null"` 判断。"""
    return value is None
