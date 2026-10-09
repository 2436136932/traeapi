"""accounts.py /admin/api/accounts 全套：列表 / 导入 / 删除 / PATCH 开关 / 刷新 / JSON 脱敏预览。

对应原版 internal/server/accounts.go。

安全纪律：
  - 列表与 JSON 预览绝不返回完整 token，只给前缀 + 长度
  - 写操作经 require_admin（Bearer 校验）
  - 回调链接只在服务端解析，前端不经手敏感数据
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import jsonutil
from ..auth import Auth, file_path_for, mask_token
from .callback import parse_callback
from .helpers import is_session_dead_err, prefix
from .state import ServerState

log = logging.getLogger("traeapi.accounts")


def _account_summary(state: ServerState, status) -> dict:
    """构造脱敏的账号摘要。"""
    summary: dict[str, Any] = {
        "uid": status.uid,
        "enabled": status.enabled,
        "disabled": status.disabled,
        "cooling": status.cooling,
        "credits": status.credits,
        "has_auth": False,
    }
    if status.nickname:
        summary["nickname"] = status.nickname
    if status.reason:
        summary["reason"] = status.reason
    if status.err_count:
        summary["err_count"] = status.err_count

    auth = state.pool.auth_by_uid(status.uid)
    if auth is not None:
        summary["has_auth"] = True
        summary["expires_at"] = auth.expires_at
        if auth.expires_at > 0 and auth.needs_refresh(24 * 3600):
            summary["expired_soon"] = True
        if auth.enterprise_id:
            summary["enterprise_id"] = auth.enterprise_id
        if auth.machine_id:
            summary["machine_id"] = prefix(auth.machine_id, 8)
        if auth.device_id:
            summary["device_id"] = prefix(auth.device_id, 8)
    return summary


async def admin_accounts(state: ServerState) -> JSONResponse:
    """GET /admin/api/accounts：列表（无鉴权，只读）。"""
    statuses = state.pool.list()
    return JSONResponse(
        status_code=200,
        content={"accounts": [_account_summary(state, s) for s in statuses]},
    )


async def admin_account_json(state: ServerState, uid: str) -> JSONResponse:
    """GET /admin/api/accounts/{uid}/json：脱敏预览。"""
    auth = state.pool.auth_by_uid(uid)
    if auth is None:
        return _err(404, "not_found", "no auth for uid")
    return JSONResponse(
        status_code=200,
        content={
            "uid": auth.uid,
            "nickname": auth.nickname,
            "enterprise_id": auth.enterprise_id,
            "domain": auth.domain,
            "api_host": auth.api_host,
            "machine_id": prefix(auth.machine_id, 8),
            "device_id": prefix(auth.device_id, 8),
            "access_token": mask_token(auth.jwt(), 12),
            "refresh_token": mask_token(auth.refresh_token_value(), 12),
            "expires_at": auth.expires_at,
            "file_path": auth.file_path,
        },
    )


async def admin_import_account(request: Request, state: ServerState) -> JSONResponse:
    """POST /admin/api/accounts/import：导入凭证。

    body 三选一：回调链接 / 嵌套 JSON / 扁平 JSON。
    """
    raw = await request.body()
    if len(raw) > (1 << 20):
        raw = raw[: 1 << 20]
    text = raw.decode("utf-8", errors="replace")
    trimmed = text.strip()

    callback_url = ""
    json_text = ""
    machine_id = ""
    device_id = ""

    if trimmed.startswith("{"):
        try:
            parsed = jsonutil.loads(trimmed)
        except (ValueError, TypeError) as exc:
            return _err(400, "invalid_request", f"parse json: {exc}")
        if not isinstance(parsed, dict):
            return _err(400, "invalid_request", "parse json: top-level must be object")
        callback_url = str(parsed.get("callback_url") or "")
        json_text = str(parsed.get("json") or "")
        machine_id = str(parsed.get("machine_id") or "")
        device_id = str(parsed.get("device_id") or "")
    else:
        callback_url = trimmed

    if not state.auth_dir:
        return _err(500, "no_auth_dir", "server AuthDir not configured")

    try:
        if callback_url:
            auth = _import_from_callback(state, callback_url, machine_id, device_id)
        elif json_text:
            auth = _import_from_json(json_text, machine_id, device_id)
        else:
            return _err(400, "invalid_request", "need callback_url or json")
    except Exception as exc:  # noqa: BLE001
        return _err(400, "import_failed", str(exc))

    if not auth.file_path:
        auth.file_path = file_path_for(state.auth_dir, auth.uid)

    Path(state.auth_dir).mkdir(parents=True, exist_ok=True)
    existed = Path(auth.file_path).exists()
    try:
        auth.save_atomic()
    except OSError as exc:
        return _err(500, "save_failed", str(exc))

    action = "updated" if existed else "created"
    state.pool.add(auth)
    return JSONResponse(
        status_code=200,
        content={
            "uid": auth.uid,
            "nickname": auth.nickname,
            "action": action,
            "needs_check": True,
        },
    )


def _import_from_callback(
    state: ServerState, callback_url: str, machine_id: str, device_id: str
) -> Auth:
    """解析回调链接 → ExchangeToken → GetUserInfo → 构造 Auth。"""
    info = parse_callback(callback_url)
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
    # 有 refreshToken → ExchangeToken 换新 access token（轮换 refreshToken）
    if auth.refresh_token:
        try:
            state.upstream.refresh_token(auth)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"exchange_token: {exc}") from exc

    # GetUserInfo 补全 uid/nickname/enterpriseId（回调 userInfo 可能缺）
    try:
        uid, nickname, enterprise = state.upstream.get_user_info(auth)
    except Exception:  # noqa: BLE001 - 失败不阻塞，用回调里的值
        uid = nickname = enterprise = ""
    if uid:
        auth.uid = uid
        if nickname:
            auth.nickname = nickname
        if enterprise:
            auth.enterprise_id = enterprise

    if not auth.uid:
        raise RuntimeError("cannot determine uid from callback or GetUserInfo")
    if not auth.access_token:
        raise RuntimeError("no access token after exchange")
    return auth


def _import_from_json(json_text: str, machine_id: str, device_id: str) -> Auth:
    """解析嵌套/扁平 JSON → 构造 Auth（凭证已完整，不 ExchangeToken）。"""
    auth = Auth.parse(json_text)
    # 可选覆盖 machine/device（扁平 JSON 手建场景）
    if machine_id:
        auth.machine_id = machine_id
    if device_id:
        auth.device_id = device_id
    # 缺省 host/domain 补默认
    if not auth.domain:
        auth.domain = "trae.cn"
    if not auth.api_host:
        auth.api_host = "https://api.trae.com.cn"
    return auth


async def admin_delete_account(state: ServerState, uid: str) -> JSONResponse:
    """DELETE /admin/api/accounts/{uid}：删池条目 + 删 auths 文件。"""
    if not uid:
        return _err(400, "invalid_request", "missing uid")
    path = file_path_for(state.auth_dir, uid)
    removed = state.pool.remove(uid)
    if not removed:
        # 池中无但有残留文件 → 也清掉文件（幂等）
        try:
            Path(path).unlink()
        except OSError:
            pass
        return _err(404, "not_found", "account not in pool")
    try:
        Path(path).unlink()  # 池条目已删，文件尽量删（不存在不报错）
    except OSError:
        pass
    return JSONResponse(status_code=200, content={"uid": uid, "deleted": True})


async def admin_patch_account(request: Request, state: ServerState, uid: str) -> JSONResponse:
    """PATCH /admin/api/accounts/{uid}：软开关 / nickname。

    token 字段一律拒绝修改（脱敏预览不返回真值，PATCH 不接受 token）。
    """
    raw = await request.body()
    try:
        parsed = jsonutil.loads(raw) if raw else {}
    except (ValueError, TypeError) as exc:
        return _err(400, "invalid_request", f"parse json: {exc}")
    if not isinstance(parsed, dict):
        parsed = {}

    _, found = state.pool.status(uid)
    if not found:
        return _err(404, "not_found", "account not found")

    enabled = parsed.get("enabled")
    if isinstance(enabled, bool):
        reason = "user enabled" if enabled else "user disabled"
        if not state.pool.set_enabled(uid, enabled, reason):
            return _err(404, "not_found", "account vanished")

    nickname = parsed.get("nickname")
    if isinstance(nickname, str) and nickname:
        auth = state.pool.auth_by_uid(uid)
        if auth is None:
            return _err(404, "not_found", "no auth for uid")
        auth.nickname = nickname
        if not auth.file_path:
            auth.file_path = file_path_for(state.auth_dir, uid)
        try:
            auth.save_atomic()
        except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断（对齐 Go 的 _ =）
            log.warning("save nickname for %s failed: %s", uid, exc)

    status, _ = state.pool.status(uid)
    return JSONResponse(status_code=200, content=status.to_dict())


async def admin_refresh_account(state: ServerState, uid: str) -> JSONResponse:
    """POST /admin/api/accounts/{uid}/refresh：手动 ExchangeToken + 落盘。"""
    auth = state.pool.auth_by_uid(uid)
    if auth is None:
        return _err(404, "not_found", "no auth for uid")
    try:
        state.upstream.refresh_token(auth)
    except Exception as exc:  # noqa: BLE001
        # session 失效 → 硬禁用（与 chat 路径一致）
        if is_session_dead_err(exc):
            state.pool.disable(uid, "manual refresh: session dead")
        return _err(502, "refresh_failed", str(exc))

    if not auth.file_path:
        auth.file_path = file_path_for(state.auth_dir, uid)
    try:
        auth.save_atomic()
    except OSError as exc:
        return _err(500, "save_failed", str(exc))

    status, _ = state.pool.status(uid)
    return JSONResponse(
        status_code=200,
        content={
            "uid": uid,
            "expires_at": auth.expires_at,
            "status": status.to_dict(),
        },
    )


def _err(status: int, code: str, msg: str) -> JSONResponse:
    """OpenAI 风格错误信封（面板也复用同一形态，前端 apiErr 靠 error.code 判断）。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": msg, "type": "api_error", "code": code}},
    )
