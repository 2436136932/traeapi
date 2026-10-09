"""admin_ext.py /admin/api/models 与 /admin/api/usage 等面板数据源。

对应原版 internal/server/admin_ext.go。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import jsonutil
from ..upstream import constants as C
from .state import ServerState

MODELS_NOTE = (
    "仅列出面向用户的官方模型（已过滤你在 TRAE 客户端自行添加的自定义模型，"
    "以及 browser_use_subagent 这类内部功能模型）；倍率优先取上游真实值"
    "（rate_source=upstream），其次 config.json → model_rates（config），"
    "都没有时按 1.0 占位（default）。注意：倍率只是上游展示系数，不是计费公式——"
    "实测实际扣费按输入/输出分别计价（输出比输入贵 5~6 倍）、模型之间与倍率不成正比，"
    "真实消耗以「积分池明细」/「额度监控」的前后差值为准"
)

FUNCTION_NOTE = (
    "切换只影响后续对话请求的 function 字段，立即生效；"
    "重启服务后回到 config.json 的 solo_function / 环境变量 TW2A_FUNCTION 配置值。"
)


def _as_int64(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _as_string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def admin_models(state: ServerState) -> JSONResponse:
    """GET /admin/api/models：列出可用模型与倍率（只读，无鉴权）。"""
    listing, hidden_custom, hidden_internal = state.catalog.model_list_detailed(
        state.pick_account
    )
    out: list[dict[str, Any]] = []
    for model in listing:
        model_id = _as_string(model.get("id"))
        if not model_id:
            continue
        entry: dict[str, Any] = {
            "id": model_id,
            "name": _as_string(model.get("name")),
            "context_length": _as_int64(model.get("context_length")),
            "rate": 1.0,
            "rate_source": "default",
            "fee_level": int(_as_int64(model.get("fee_level"))),
        }
        flag = model.get("context_from_upstream")
        if isinstance(flag, bool):
            entry["context_from_upstream"] = flag
        # 倍率优先级：上游真实值 > config.json 配置 > 默认 1.0 占位
        upstream_rate = model.get("rate")
        if isinstance(upstream_rate, (int, float)) and upstream_rate > 0:
            entry["rate"] = float(upstream_rate)
            entry["rate_source"] = "upstream"
            original = model.get("original_rate")
            if isinstance(original, (int, float)):
                entry["original_rate"] = float(original)
            entry["discount_percent"] = int(_as_int64(model.get("discount_percent")))
            matched = model.get("discount_matched")
            if isinstance(matched, bool):
                entry["discount_matched"] = matched
        else:
            configured = state.model_rates.get(model_id)
            if configured is not None and configured > 0:
                entry["rate"] = float(configured)
                entry["rate_source"] = "config"
        out.append(entry)

    return JSONResponse(
        status_code=200,
        content={
            "object": "list",
            "total": len(out),
            "hidden_custom": hidden_custom,
            "hidden_internal": hidden_internal,
            "data": out,
            "note": MODELS_NOTE,
        },
    )


def admin_refresh_models(state: ServerState) -> JSONResponse:
    """POST /admin/api/models/refresh：绕过缓存立即重新拉取上游模型表。"""
    try:
        total = state.catalog.refresh_dynamic(state.pick_account)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": str(exc),
                    "type": "api_error",
                    "code": "refresh_models_failed",
                }
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "upstream_total": total,
            "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def admin_usage(state: ServerState, limit: int = 0) -> JSONResponse:
    """GET /admin/api/usage：最近的调用记录与汇总（只读，无鉴权）。"""
    return JSONResponse(
        status_code=200,
        content={
            "summary": state.stats.summary(),
            "records": [r.to_dict() for r in state.stats.recent(limit)],
        },
    )


def admin_function(state: ServerState) -> JSONResponse:
    """GET /admin/api/function：当前 SOLO function 与可切换列表（只读）。"""
    return JSONResponse(
        status_code=200,
        content={
            "current": C.active_function(),
            "default": C.Function,
            "options": C.function_options(),
            "config": state.solo_function,
            "note": FUNCTION_NOTE,
        },
    )


async def admin_set_function(request: Request, state: ServerState) -> JSONResponse:
    """POST /admin/api/function：热切换 SOLO function（写操作，需 Bearer）。

    body: {"function":"solo_work_remote"}; 传空串恢复默认。
    """
    raw = await request.body()
    function = ""
    if raw:
        try:
            parsed = jsonutil.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            value = parsed.get("function")
            function = value if isinstance(value, str) else ""

    if not C.set_function(function):
        options = ", ".join(C.known_functions())
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": f"unknown function: {function}（可选：{options}）",
                    "type": "api_error",
                    "code": "unknown_function",
                }
            },
        )
    return JSONResponse(
        status_code=200,
        content={"ok": True, "current": C.active_function()},
    )
