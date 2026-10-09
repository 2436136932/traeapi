"""payload.py OpenAI → SOLO llm_utils_chat 请求体改写。

对应原版 internal/upstream/payload.go。

改写规则：
 1. messages: content 字符串 → [{"type":"text","text":...}]；已是数组 → 透传
 2. stream: 强制 true（非流式由服务端聚合）
 3. model → config_name + model
 4. function: 取自 active_function()（默认 solo_work_lite，可配置切换）
 5. tools/tool_choice: 归一化（"none" 删 tools；auto/required 保留；function 提取 name）
"""

from __future__ import annotations

import json
from typing import Any

from .. import jsonutil
from .constants import active_function

# 默认模型（glm-5.2，实测可用）。
DefaultConfigName = "glm-5.2"


def prepare_body(src: bytes | str) -> bytes:
    """单 pass 改写；无法解析时原样返回。

    OpenAI: {model, messages, stream, tools, tool_choice, ...}
    SOLO:   {messages, function:<active_function()>, stream:true,
             config_name:<model>, model:<model>}
    """
    if not src:
        return src if isinstance(src, bytes) else src.encode("utf-8")

    if isinstance(src, bytes):
        text = src.decode("utf-8", errors="replace")
    else:
        text = src

    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return src if isinstance(src, bytes) else src.encode("utf-8")
    if not isinstance(obj, dict):
        return src if isinstance(src, bytes) else src.encode("utf-8")

    obj["stream"] = True
    obj["function"] = active_function()

    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for message in msgs:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            present = "content" in message
            content = message.get("content")

            # assistant 消息回传 tool_calls: OpenAI function → 上游 function_call
            if role == "assistant":
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    kept: list[Any] = []
                    for call in tool_calls:
                        if not isinstance(call, dict):
                            continue
                        # OpenAI: function{name, arguments} → 上游 SOLO: function_call{name, arguments}
                        fn = call.get("function")
                        if isinstance(fn, dict):
                            call["function_call"] = fn
                            del call["function"]
                        # 上游要求 FunctionCall.Name 必填: 无 name 的 tool_call 剔除
                        fc = call.get("function_call")
                        if isinstance(fc, dict):
                            name = fc.get("name")
                            if not isinstance(name, str) or not name.strip():
                                continue
                        kept.append(call)
                    if not kept:
                        del message["tool_calls"]
                    else:
                        message["tool_calls"] = kept

            if not present or content is None:
                # 无 content 的消息（如纯 tool_calls assistant 已转换完）保留字段但跳过 content 改写
                continue
            if isinstance(content, str):
                message["content"] = [{"type": "text", "text": content}]
            # 其他（已是数组）→ 透传（兼容多模态，未实测，保守透传）

    model = obj.get("model")
    model = model.strip() if isinstance(model, str) else ""
    if not model:
        model = DefaultConfigName
    obj["config_name"] = model
    obj["model"] = model

    normalize_tool_choice(obj)
    normalize_tools(obj)

    try:
        return jsonutil.dumps(obj).encode("utf-8")
    except (TypeError, ValueError):
        return src if isinstance(src, bytes) else src.encode("utf-8")


def normalize_tool_choice(obj: dict) -> None:
    """按上游 Go struct（string 类型）改写 OpenAI tool_choice。

      - "none" / {"type":"none"} → 删 tool_choice + 删 tools/functions
      - {"type":"auto"/"required"} → 字符串 "auto"/"required"
      - {"type":"function","function":{"name":"x"}} → 字符串 "x"
      - 其他对象/非标量 → 删 tool_choice
    """

    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    choice = obj["tool_choice"]

    if isinstance(choice, str):
        if choice.strip().lower() == "none":
            del obj["tool_choice"]
            suppress()
        return

    if isinstance(choice, dict):
        typ = choice.get("type")
        typ = typ.strip().lower() if isinstance(typ, str) else ""
        if typ == "none":
            del obj["tool_choice"]
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = choice.get("function")
            if isinstance(fn, dict):
                raw_name = fn.get("name")
                name = raw_name if isinstance(raw_name, str) else ""
            if not name:
                raw_name = choice.get("name")
                name = raw_name if isinstance(raw_name, str) else ""
            name = name.strip()
            obj["tool_choice"] = name if name else "auto"
        else:
            del obj["tool_choice"]
        return

    del obj["tool_choice"]


def normalize_tools(obj: dict) -> None:
    """把 OpenAI tools 转为 SOLO 上游格式。

    实测上游 Go struct: FunctionDefinition.tools[].function.parameters 是 string 类型
    （OpenAI 标准是 object）→ 需把 parameters 对象序列化为 JSON 字符串。
    同时 tools 条目若不是 map 或缺 function，整体剔除（避免上游反序列化失败）。
    """
    if "tools" not in obj:
        return
    raw = obj["tools"]
    if not isinstance(raw, list) or not raw:
        return
    out: list[Any] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, dict):
            try:
                fn["parameters"] = jsonutil.dumps(params)
            except (TypeError, ValueError):
                pass
        out.append(item)
    if not out:
        del obj["tools"]
        return
    obj["tools"] = out
