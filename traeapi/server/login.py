"""login.py Web 登录闭环：生成登录 URL → pending 态 → /authorize 回调捕获 →

ExchangeToken + GetUserInfo + 落盘 → 面板轮询 result。
对应原版 internal/server/login.go。

回调端口策略（双端口 18080）：
  - TRAE 登录页回调到 http://127.0.0.1:18080/authorize
  - __main__ 起第二个 ASGI 服务监听 18080，复用同一 app（/authorize 已注册）
  - 回调捕获后直接在服务端完成导入（用户无需再粘贴）

pending 态只在内存（重启丢失），符合「登录态瞬时」语义。
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from ..auth import Auth, file_path_for
from .callback import build_login_url, machine_trace_id, parse_callback
from .helpers import authorize_render, decode_body_optional, mkdir_all
from .state import PendingLogin, ServerState

log = logging.getLogger("traeapi.login")

PENDING_ACTIVE = "pending"
PENDING_SUCCESS = "success"
PENDING_FAILED = "failed"
PENDING_CANCELED = "canceled"

# pending TTL（秒）
PENDING_TTL = 600.0


def random_hex(n_bytes: int) -> str:
    """生成 n 字节随机 hex（2n hex 字符）。"""
    return secrets.token_hex(n_bytes)


async def admin_login_start(request: Request, state: ServerState) -> JSONResponse:
    """POST /admin/api/login：生成登录 URL + pending 态。

    body 可选 {callback_port: "18080"}；默认用 127.0.0.1:18080/authorize。
    """
    payload = await decode_body_optional(request)
    callback_port = payload.get("callback_port")
    callback_port = str(callback_port) if callback_port else "18080"
    callback_url = f"http://127.0.0.1:{callback_port}/authorize"

    machine_id = random_hex(16)  # hex32
    device_id = random_hex(16)
    login_url = build_login_url(machine_id, device_id, callback_url)
    pending_id = random_hex(8)  # hex16

    pending = PendingLogin(
        state=PENDING_ACTIVE,
        machine_id=machine_id,
        device_id=device_id,
        callback_url=callback_url,
        created_at=time.time(),
    )
    with state.login_mu:
        state.logins[pending_id] = pending

    return JSONResponse(
        status_code=200,
        content={
            "login_url": login_url,
            "pending_id": pending_id,
            "callback_url": callback_url,
        },
    )


async def admin_login_result(state: ServerState, pending_id: str) -> JSONResponse:
    """GET /admin/api/login/result?pending_id=..."""
    pending = _get_pending(state, pending_id)
    if pending is None:
        return _err(404, "not_found", "pending login not found (expired or invalid)")
    content = {"pending_id": pending_id, "state": pending.state}
    if pending.state == PENDING_SUCCESS:
        content["uid"] = pending.uid
        content["nickname"] = pending.nickname
    elif pending.state == PENDING_FAILED:
        content["error"] = pending.err_msg
    return JSONResponse(status_code=200, content=content)


async def admin_login_cancel(request: Request, state: ServerState, pending_id: str = "") -> JSONResponse:
    """POST /admin/api/login/cancel（body {pending_id} 或 query ?pending_id=）"""
    if not pending_id:
        payload = await decode_body_optional(request)
        pending_id = str(payload.get("pending_id") or "")
    with state.login_mu:
        pending = state.logins.get(pending_id)
        if pending is not None and pending.state == PENDING_ACTIVE:
            pending.state = PENDING_CANCELED
        # 直接删 pending（取消后不再保留）
        found = state.logins.pop(pending_id, None) is not None
    return JSONResponse(
        status_code=200,
        content={"pending_id": pending_id, "canceled": found},
    )


async def authorize_callback(request: Request, state: ServerState):
    """GET /authorize：TRAE 回调落点。

    捕获 query → 解析 → ExchangeToken → GetUserInfo → 落盘 → 标记 pending 成功。
    pending_id 通过 TRAE 无法回传（回调 URL 固定），用 machine_id / loginTraceID
    反查 pending。
    """
    query = request.url.query or ""
    raw_url = f"http://127.0.0.1/authorize?{query}" if query else "http://127.0.0.1/authorize"

    try:
        info = parse_callback(raw_url)
    except ValueError as exc:
        # 回调解析失败：展示一个友好错误页（非 JSON，因为是浏览器跳转）
        return authorize_render(400, "登录回调解析失败", str(exc))

    params = request.query_params
    machine_id = params.get("machine_id") or ""
    device_id = params.get("device_id") or ""
    trace_id = params.get("loginTraceID") or ""
    # TRAE 回调不回传 machine_id/device_id，但回传 loginTraceID
    # （= machine_trace_id(machine, device) 派生）→ 用它反查 pending 拿回登录时
    # 生成的那一对 id，保证凭证与登录态一致
    if not machine_id or not device_id:
        if trace_id:
            pending = _get_pending_by_trace(state, trace_id)
            if pending is not None:
                machine_id, device_id = pending.machine_id, pending.device_id

    auth = Auth(
        access_token=info.access_token,
        refresh_token=info.refresh_token,
        uid=info.uid,
        nickname=info.nickname,
        enterprise_id=info.enterprise_id,
        domain="trae.cn",
        api_host="https://api.trae.com.cn",
        machine_id=machine_id,
        device_id=device_id,
        expires_at=info.expires_at,
    )

    # 有 refreshToken → ExchangeToken 换新 access + 轮换 refreshToken
    if auth.refresh_token:
        try:
            state.upstream.refresh_token(auth)
        except Exception as exc:  # noqa: BLE001
            # 失败也继续（可能 refreshToken 已被轮换），但标记错误
            _mark_pending_by_machine(state, machine_id, trace_id, PENDING_FAILED, "", "", str(exc))
            return authorize_render(502, "ExchangeToken 失败", str(exc))

    # GetUserInfo 补全 uid/nickname
    try:
        uid, nickname, enterprise = state.upstream.get_user_info(auth)
    except Exception:  # noqa: BLE001
        uid = nickname = enterprise = ""
    if uid:
        auth.uid = uid
        if nickname:
            auth.nickname = nickname
        if enterprise:
            auth.enterprise_id = enterprise

    if not auth.uid:
        msg = "cannot determine uid from callback or GetUserInfo"
        _mark_pending_by_machine(state, machine_id, trace_id, PENDING_FAILED, "", "", msg)
        return authorize_render(400, "登录失败", msg)
    if not auth.access_token:
        msg = "no access token after exchange"
        _mark_pending_by_machine(state, machine_id, trace_id, PENDING_FAILED, "", "", msg)
        return authorize_render(400, "登录失败", msg)

    # 落盘
    if not auth.file_path:
        auth.file_path = file_path_for(state.auth_dir, auth.uid)
    try:
        mkdir_all(state.auth_dir)
    except OSError as exc:
        _mark_pending_by_machine(
            state, machine_id, trace_id, PENDING_FAILED, auth.uid, auth.nickname, str(exc)
        )
        return authorize_render(500, "落盘失败", str(exc))

    try:
        auth.save_atomic()
    except OSError as exc:
        _mark_pending_by_machine(
            state, machine_id, trace_id, PENDING_FAILED, auth.uid, auth.nickname, str(exc)
        )
        return authorize_render(500, "落盘失败", str(exc))

    state.pool.add(auth)
    _mark_pending_by_machine(
        state, machine_id, trace_id, PENDING_SUCCESS, auth.uid, auth.nickname, ""
    )
    return authorize_render(
        200,
        "登录成功",
        f"账号 {auth.uid}（{auth.nickname}）已添加，可关闭此窗口返回面板。",
    )


# ---------------------------------------------------------------------------
# pending 辅助
# ---------------------------------------------------------------------------


def _mark_pending_by_machine(
    state: ServerState,
    machine_id: str,
    trace_id: str,
    new_state: str,
    uid: str,
    nickname: str,
    err_msg: str,
) -> None:
    """用 machine_id（或 loginTraceID 派生匹配）反查 pending 并标记状态。"""
    if not machine_id and not trace_id:
        return
    with state.login_mu:
        for pending in state.logins.values():
            if pending.machine_id == machine_id or (
                trace_id and machine_trace_id(pending.machine_id, pending.device_id) == trace_id
            ):
                pending.state = new_state
                if uid:
                    pending.uid = uid
                if nickname:
                    pending.nickname = nickname
                if err_msg:
                    pending.err_msg = err_msg
                return


def _get_pending_by_trace(state: ServerState, trace_id: str) -> PendingLogin | None:
    """用 loginTraceID（= machine_trace_id(machine, device)）反查 pending。"""
    if not trace_id:
        return None
    with state.login_mu:
        for pending in state.logins.values():
            if machine_trace_id(pending.machine_id, pending.device_id) == trace_id:
                return pending
    return None


def _get_pending(state: ServerState, pending_id: str) -> PendingLogin | None:
    """取 pending（不存在或已过期返回 None）。pending TTL 10 分钟。"""
    if not pending_id:
        return None
    with state.login_mu:
        pending = state.logins.get(pending_id)
        if pending is None:
            return None
        if time.time() - pending.created_at > PENDING_TTL:
            del state.logins[pending_id]
            return None
        return pending


def _err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": msg, "type": "api_error", "code": code}},
    )
