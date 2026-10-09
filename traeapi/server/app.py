"""app.py FastAPI 应用：路由注册 + 鉴权。

对应原版 internal/server/handler.go 的 NewHandler（路由表部分）。

路由表与原版逐条对齐：

    POST   /v1/chat/completions           需 Bearer
    GET    /v1/models                     需 Bearer
    GET    /status                        需 Bearer
    GET    /healthz                       无鉴权
    GET    /                              302 → /admin
    GET    /admin                         面板 HTML
    GET    /admin/api/credits             只读
    GET    /admin/api/pools               只读
    POST   /admin/api/checkin             需 Bearer
    GET    /admin/api/models              只读
    POST   /admin/api/models/refresh      需 Bearer
    GET    /admin/api/usage               只读
    GET    /admin/api/function            只读
    POST   /admin/api/function            需 Bearer
    GET    /admin/api/accounts            只读
    POST   /admin/api/accounts/import     需 Bearer
    DELETE /admin/api/accounts/{uid}      需 Bearer
    PATCH  /admin/api/accounts/{uid}      需 Bearer
    POST   /admin/api/accounts/{uid}/refresh  需 Bearer
    GET    /admin/api/accounts/{uid}/json 只读（严格脱敏）
    POST   /admin/api/login               需 Bearer
    GET    /admin/api/login/result        只读
    POST   /admin/api/login/cancel        需 Bearer
    GET    /authorize                     无鉴权（TRAE 浏览器 302 不带 key）
    GET    /admin/api/key                 只读（脱敏）
    POST   /admin/api/key                 需 Bearer（在线修改密钥）
    DELETE /admin/api/key                 需 Bearer（清除密钥 → 不再鉴权）
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from .. import jsonutil
from . import accounts, admin, admin_ext, chat, checkin, keys, login
from .state import ServerState

log = logging.getLogger("traeapi.server")

_BEARER_PREFIX = "Bearer "


def _extract_bearer(request: Request) -> str | None:
    """取出 Bearer 后的密钥；前缀大小写不敏感（对齐 Go 的 EqualFold）。"""
    authz = request.headers.get("authorization") or ""
    if len(authz) < len(_BEARER_PREFIX):
        return None
    if authz[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX.lower():
        return None
    return authz[len(_BEARER_PREFIX) :]


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": "missing or invalid API key",
                "type": "api_error",
                "code": "invalid_api_key",
            }
        },
    )


def check_api_key(state: ServerState, request: Request) -> bool:
    """校验 Bearer = 当前生效密钥（常量时间比较）。密钥为空时不鉴权。"""
    expected = state.current_key()
    if not expected:
        return True
    key = _extract_bearer(request)
    if key is None:
        return False
    return hmac.compare_digest(key.encode("utf-8"), expected.encode("utf-8"))


def create_app(state: ServerState) -> FastAPI:
    """按 ServerState 构造 FastAPI 应用。"""
    app = FastAPI(
        title="traeapi",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.traeapi = state

    # ------------------------------------------------------------------
    # 鉴权装饰器
    # ------------------------------------------------------------------

    def with_auth(handler: Callable) -> Callable:
        """OpenAI API 鉴权（/v1/* 与 /status）。"""

        async def wrapper(request: Request):
            if not check_api_key(state, request):
                return _unauthorized()
            return await handler(request)

        wrapper.__name__ = getattr(handler, "__name__", "handler")
        return wrapper

    def with_admin_auth(handler: Callable) -> Callable:
        """面板写操作鉴权（Bearer = TW2A_API_KEY）。

        api_key 为空时（未配置 TW2A_API_KEY）退化为不鉴权 —— 本地无 key 场景仍可用。
        """

        async def wrapper(request: Request):
            if not check_api_key(state, request):
                return _unauthorized()
            return await handler(request)

        wrapper.__name__ = getattr(handler, "__name__", "handler")
        return wrapper

    # ------------------------------------------------------------------
    # OpenAI 兼容接口
    # ------------------------------------------------------------------

    @app.post("/v1/chat/completions")
    async def _chat(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await chat.chat_completions(request, state)

    @app.get("/v1/models")
    async def _models(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        listing = state.catalog.model_list(state.pick_account)
        return JSONResponse(status_code=200, content={"object": "list", "data": listing})

    @app.get("/status")
    async def _status(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return JSONResponse(
            status_code=200,
            content={"accounts": [s.to_dict() for s in state.pool.list()]},
        )

    @app.get("/healthz")
    async def _healthz():
        return PlainTextResponse(content="ok", status_code=200)

    # ------------------------------------------------------------------
    # 根路径 → 管理面板
    # ------------------------------------------------------------------

    @app.get("/")
    async def _root():
        # 对齐 Go 的 http.Redirect(..., 302)
        return RedirectResponse(url="/admin", status_code=302)

    # ------------------------------------------------------------------
    # 管理面板
    # ------------------------------------------------------------------

    @app.get("/admin")
    async def _admin_page():
        return admin.admin_page()

    @app.get("/admin/api/credits")
    async def _admin_credits():
        return admin.admin_credits(state)

    @app.get("/admin/api/pools")
    async def _admin_pools():
        return admin.admin_pools(state)

    @app.post("/admin/api/checkin")
    async def _admin_checkin(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await checkin.admin_checkin(state)

    @app.get("/admin/api/models")
    async def _admin_models():
        return admin_ext.admin_models(state)

    @app.post("/admin/api/models/refresh")
    async def _admin_refresh_models(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return admin_ext.admin_refresh_models(state)

    @app.get("/admin/api/usage")
    async def _admin_usage(request: Request):
        limit = 0
        raw_limit = request.query_params.get("limit") or ""
        if raw_limit:
            try:
                parsed = int(raw_limit)
                if parsed > 0:
                    limit = parsed
            except ValueError:
                limit = 0
        return admin_ext.admin_usage(state, limit)

    @app.get("/admin/api/function")
    async def _admin_function():
        return admin_ext.admin_function(state)

    @app.post("/admin/api/function")
    async def _admin_set_function(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await admin_ext.admin_set_function(request, state)

    # ------------------------------------------------------------------
    # 账号 CRUD
    # ------------------------------------------------------------------

    @app.get("/admin/api/accounts")
    async def _admin_accounts():
        return await accounts.admin_accounts(state)

    @app.post("/admin/api/accounts/import")
    async def _admin_import(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await accounts.admin_import_account(request, state)

    @app.delete("/admin/api/accounts/{uid}")
    async def _admin_delete(request: Request, uid: str):
        if not check_api_key(state, request):
            return _unauthorized()
        return await accounts.admin_delete_account(state, uid)

    @app.patch("/admin/api/accounts/{uid}")
    async def _admin_patch(request: Request, uid: str):
        if not check_api_key(state, request):
            return _unauthorized()
        return await accounts.admin_patch_account(request, state, uid)

    @app.post("/admin/api/accounts/{uid}/refresh")
    async def _admin_refresh(request: Request, uid: str):
        if not check_api_key(state, request):
            return _unauthorized()
        return await accounts.admin_refresh_account(state, uid)

    @app.get("/admin/api/accounts/{uid}/json")
    async def _admin_account_json(uid: str):
        return await accounts.admin_account_json(state, uid)

    # ------------------------------------------------------------------
    # Web 登录闭环
    # ------------------------------------------------------------------

    @app.post("/admin/api/login")
    async def _login_start(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await login.admin_login_start(request, state)

    @app.get("/admin/api/login/result")
    async def _login_result(request: Request):
        pending_id = request.query_params.get("pending_id") or ""
        return await login.admin_login_result(state, pending_id)

    @app.post("/admin/api/login/cancel")
    async def _login_cancel(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        pending_id = request.query_params.get("pending_id") or ""
        return await login.admin_login_cancel(request, state, pending_id)

    @app.get("/authorize")
    async def _authorize(request: Request):
        return await login.authorize_callback(request, state)

    # ------------------------------------------------------------------
    # API 密钥在线管理（面板自定义密码）
    # ------------------------------------------------------------------

    @app.get("/admin/api/key")
    async def _get_key():
        return await keys.admin_get_key(state)

    @app.post("/admin/api/key")
    async def _set_key(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await keys.admin_set_key(request, state)

    @app.delete("/admin/api/key")
    async def _clear_key(request: Request):
        if not check_api_key(state, request):
            return _unauthorized()
        return await keys.admin_clear_key(state)

    # ------------------------------------------------------------------
    # 兜底：未匹配路径返回 404（对齐 Go ServeMux）
    # ------------------------------------------------------------------

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Any):  # noqa: ARG001
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "not found",
                    "type": "api_error",
                    "code": "not_found",
                }
            },
        )

    return app


def dumps(value: Any) -> str:
    """便捷的 JSON 序列化（供外部调用）。"""
    return jsonutil.dumps(value)
