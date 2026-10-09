"""jsonutil.py JSON 编解码小工具。

存在的理由：Go 的 `encoding/json` 把 `map[string]any` 里的数字统一当作 float64，
序列化时整数值输出为 `1`（而不是 Python 默认的 `1.0`）。为了让转发给上游的
请求体与原版行为一致，这里在编码前把「整数值的 float」归一化成 int。

同时提供 `html_escape=True` 选项以复刻 Go 默认的 `<`/`>`/`&` 转义
（默认关闭：客户端解析后语义相同，不需要）。
"""

from __future__ import annotations

import json
from typing import Any


def normalize_numbers(value: Any) -> Any:
    """递归把整数值的 float 归一化成 int（对齐 Go float64 的序列化输出）。"""
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return int(value)
        return value
    if isinstance(value, dict):
        return {k: normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_numbers(v) for v in value]
    return value


def dumps(
    value: Any,
    *,
    sort_keys: bool = False,
    ensure_ascii: bool = False,
    html_escape: bool = False,
    indent: int | None = None,
    compact: bool = True,
) -> str:
    """序列化为 JSON 文本。

    compact=True 时使用 Go 风格的无空格分隔符（`,` 与 `:`）。
    """
    normalized = normalize_numbers(value)
    if compact and indent is None:
        separators = (",", ":")
    else:
        separators = None
    text = json.dumps(
        normalized,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        indent=indent,
        separators=separators,
        allow_nan=False,
    )
    if html_escape:
        text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return text


def loads(text: str | bytes) -> Any:
    """解析 JSON 文本。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return json.loads(text)
