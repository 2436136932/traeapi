"""callback.py TRAE 登录回调解析 + 登录 URL 构造。

对应原版 internal/server/callback.go（移植自 login.sh 的内嵌 Python）。

额外补上了 login.sh 里有、而 Go 版漏掉的 `fix_mojibake`（昵称乱码修复）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..upstream.constants import ClientID, ConsoleHost, IdeVersion

# 与 login.sh 保持一致；如未来 IdeVersion 升级，这里同步即可。
LOGIN_APP_VERSION = IdeVersion

# login_trace_id 的 hex 长度（login.sh 用 secrets.token_hex(8) → 16 hex 字符）。
LOGIN_TRACE_ID_HEX_LEN = 16


def build_login_url(machine_id: str, device_id: str, callback_url: str) -> str:
    """构造 TRAE 登录 URL（复刻 login.sh 的参数集）。

    callback_url 是 TRAE 登录成功后的重定向落点（如 http://127.0.0.1:7864/authorize）。
    machine_id/device_id 必须与落盘 auth 文件共用同一对（hex32），
    保证登录态与凭证一致。
    """
    # 参数顺序对齐 login.sh 的 dict 字面量顺序
    params: list[tuple[str, str]] = [
        ("login_version", "1"),
        ("auth_from", "solo"),
        ("login_channel", "native_ide"),
        ("plugin_version", "2.3.62834"),
        ("auth_type", "local"),
        ("client_id", ClientID),
        ("redirect", "0"),
        ("login_trace_id", machine_trace_id(machine_id, device_id)),
        ("auth_callback_url", callback_url),
        ("machine_id", machine_id),
        ("device_id", device_id),
        ("x_device_id", device_id),
        ("x_machine_id", machine_id),
        ("x_device_brand", "PC"),
        ("x_device_type", "PC"),
        ("x_os_version", "1.0"),
        ("x_app_version", LOGIN_APP_VERSION),
        ("x_app_type", "stable"),
    ]
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params)
    return f"{ConsoleHost}/authorization?{query}"


def machine_trace_id(machine_id: str, device_id: str) -> str:
    """由 machine_id + device_id 派生一个稳定的 login_trace_id（hex16）。

    时间源不可用，用输入摘要的收尾 16 字符保证可复现且非空。
    """
    combined = f"{machine_id}{device_id}"
    if len(combined) >= LOGIN_TRACE_ID_HEX_LEN:
        return combined[-LOGIN_TRACE_ID_HEX_LEN:]
    return "0" * (LOGIN_TRACE_ID_HEX_LEN - len(combined)) + combined


@dataclass
class CallbackInfo:
    """回调链接解析结果（脱敏前的原始凭证，仅服务端内部使用）。"""

    refresh_token: str = ""  # 优先取 query.refreshToken，缺省回退 userJwt.RefreshToken
    access_token: str = ""  # 无 refreshToken 时回退 userJwt.Token（兜底）
    uid: str = ""  # userInfo.UserID
    nickname: str = ""  # userInfo.ScreenName
    enterprise_id: str = ""  # userInfo.TenantID（注意回调字段名是 TenantID）
    expires_at: int = 0  # userJwt 兜底路径会设


def parse_json_param(raw: str) -> dict | None:
    """解回调里 URL 编码的 JSON 参数。

    parse_qs 已解一层 percent-encoding，这里再容错解一层 unquote。
    """
    if not raw:
        return None
    candidates = [raw]
    decoded = unquote(raw)
    if decoded != raw:
        candidates.append(decoded)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def get_string(mapping: dict | None, key: str) -> str:
    """从 dict 取字符串（对齐 Go 的 getString：数字也转字符串）。"""
    if not mapping:
        return ""
    value = mapping.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def get_int64(mapping: dict | None, key: str) -> int:
    """从 dict 取 int64（宽松转换）。"""
    if not mapping:
        return 0
    value = mapping.get(key)
    if value is None:
        return 0
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


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def fix_mojibake(s: str, uid: str) -> str:
    """修复回调 userInfo 中文被双重编码导致的昵称乱码（如 'Óû§8847309959'）。

    尝试常见错误编码回转；无法修复时回退为「用户 + uid 末 4 位」。
    这段逻辑只存在于 login.sh，Go 版漏掉了，Python 版补上。
    """
    if not s:
        return s
    for encoding in ("latin-1", "cp1252"):
        try:
            fixed = s.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed and all(ch.isprintable() for ch in fixed):
            return fixed
    # 无 CJK 字符 → 判定为乱码，回退
    if not _CJK_RE.search(s):
        return "用户" + (uid[-4:] if uid else "")
    return s


def normalize_expire(value: int) -> int:
    """TokenExpireAt 毫秒 → Unix 秒（>1e12 视为毫秒）。"""
    if value > 1_000_000_000_000:
        return value // 1000
    return value


def expire_at_from_exchange(token_expire_at: int, token_expire_duration: int, now: float) -> int:
    """把 ExchangeToken 返回的 TokenExpireAt/TokenExpireDuration 归一化为 Unix 秒。

    复刻 login.sh：优先 TokenExpireAt，过期则用 now + TokenExpireDuration。
    """
    if token_expire_at > 0:
        exp = normalize_expire(token_expire_at)
        if exp > int(now):
            return exp
    if token_expire_duration > 0:
        return int(now) + token_expire_duration
    return 0


def parse_callback(raw_url: str) -> CallbackInfo:
    """解析 TRAE 登录回调链接，提取凭证字段。

    回调形如：

        http://127.0.0.1:18080/authorize?refreshToken=...&userInfo={...}&userJwt={...}

    refreshToken 优先；缺失时回退 userJwt.Token（login.sh 兜底分支）。
    仅做解析，不执行 ExchangeToken。
    """
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("empty callback url")

    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise ValueError(f"parse callback url: {exc}") from exc

    query = parse_qs(parsed.query, keep_blank_values=True)

    def first(key: str) -> str:
        values = query.get(key)
        return values[0] if values else ""

    info = CallbackInfo(refresh_token=first("refreshToken"))

    user_info = parse_json_param(first("userInfo"))
    info.uid = get_string(user_info, "UserID")
    info.nickname = get_string(user_info, "ScreenName")
    info.enterprise_id = get_string(user_info, "TenantID")
    # 修复中文昵称乱码（login.sh 有、Go 版漏掉的增强）
    if info.nickname:
        info.nickname = fix_mojibake(info.nickname, info.uid)

    user_jwt = parse_json_param(first("userJwt"))
    jwt_token = get_string(user_jwt, "Token")
    jwt_refresh = get_string(user_jwt, "RefreshToken")

    # 回调缺 refreshToken 时，回退 userJwt 的 RefreshToken
    if not info.refresh_token:
        info.refresh_token = jwt_refresh
    if not info.refresh_token:
        # 兜底：无 refreshToken 时直接用 userJwt.Token 作为 accessToken
        info.access_token = jwt_token
        if not jwt_token:
            raise ValueError("callback missing refreshToken and userJwt.Token")
        exp = get_int64(user_jwt, "TokenExpireAt")
        if exp > 0:
            info.expires_at = normalize_expire(exp)
    return info


def parse_callback_from_query_string(query_string: str) -> CallbackInfo:
    """从裸 query string（如 `/authorize?...` 的路径部分）解析回调。"""
    qs = query_string if query_string.startswith("?") else "?" + query_string
    return parse_callback("http://127.0.0.1" + qs)


def now_seconds() -> float:
    """当前 Unix 秒（便于测试注入）。"""
    return time.time()
