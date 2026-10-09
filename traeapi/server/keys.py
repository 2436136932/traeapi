"""keys.py 面板在线管理 API 密钥。

原版 Go 的密钥只能通过环境变量 TW2A_API_KEY 提供、必须重启才能更换；
这里补上「面板自定义密码」能力。

安全设计：
  - 读取当前状态 GET  /admin/api/key      —— 只返回脱敏信息，永不返回明文
  - 修改密钥     POST /admin/api/key      —— 需先通过旧密钥鉴权（Bearer）
  - 清除密钥     DELETE /admin/api/key    —— 需先通过旧密钥鉴权（Bearer），清除后服务不鉴权

修改/清除后立即生效（无需重启），并持久化到 data/admin_key.json。
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import apikey as apikey_mod
from .. import jsonutil
from .state import ServerState

log = logging.getLogger("traeapi.keys")


def _err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": msg, "type": "api_error", "code": code}},
    )


async def admin_get_key(state: ServerState) -> JSONResponse:
    """GET /admin/api/key：当前密钥状态（只读、脱敏）。"""
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            **state.key_status(),
            "note": (
                "密钥可在此页在线修改，立即生效并持久化到 data/admin_key.json（不重启也生效）。"
                "删除该文件并重启即可回退到 .env 里的 TW2A_API_KEY。"
                "密钥只用于写操作鉴权；忘记密钥时可删除 data/admin_key.json 后重启。"
            ),
        },
    )


async def admin_set_key(request: Request, state: ServerState) -> JSONResponse:
    """POST /admin/api/key：设置新密钥（需旧密钥鉴权）。

    body: {"api_key": "新密钥"}
    """
    raw = await request.body()
    payload: dict = {}
    if raw:
        try:
            parsed = jsonutil.loads(raw)
        except (ValueError, TypeError) as exc:
            return _err(400, "invalid_request", f"parse json: {exc}")
        if isinstance(parsed, dict):
            payload = parsed

    value = payload.get("api_key")
    if not isinstance(value, str):
        return _err(400, "invalid_request", "缺少 api_key 字段")

    try:
        key = apikey_mod.validate_key(value)
    except apikey_mod.ApiKeyError as exc:
        return _err(400, "invalid_api_key_value", str(exc))

    previous = state.current_key()
    if key == previous:
        # 幂等：相同密钥直接成功，不重复落盘
        return JSONResponse(
            status_code=200,
            content={"ok": True, "changed": False, **state.key_status()},
        )

    try:
        apikey_mod.save_stored_key(state.key_file, key)
    except OSError as exc:
        return _err(500, "save_failed", f"写入密钥文件失败：{exc}")

    state.set_key(key, "stored")
    log.info("admin api key updated via panel (len=%d)", len(key))
    return JSONResponse(
        status_code=200,
        content={"ok": True, "changed": True, **state.key_status()},
    )


async def admin_clear_key(state: ServerState) -> JSONResponse:
    """DELETE /admin/api/key：清除持久化密钥（需当前密钥鉴权）。

    清除后服务不再鉴权（等价于未配置 TW2A_API_KEY），面板写操作无需 Key。
    """
    removed = apikey_mod.clear_stored_key(state.key_file)
    state.set_key("", "none")
    log.info("admin api key cleared via panel (file_removed=%s)", removed)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "removed": removed,
            **state.key_status(),
            "note": "已清除密钥：服务不再鉴权，面板写操作无需 Key。重启后也不会恢复（除非 .env 里有 TW2A_API_KEY）。",
        },
    )
