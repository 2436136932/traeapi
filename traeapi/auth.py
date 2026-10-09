"""auth.py 解析 TRAE SOLO auth 文件（嵌套形：auth + account），提供原子写回与目录扫描。

对应原版 internal/auth/auth.go。

并发模型：每把 Auth 自带一把 RLock 保护可变字段（access_token / refresh_token /
expires_at）；写路径（upstream.refresh_token）持写锁整段执行 ExchangeToken，
读路径（needs_refresh / jwt / refresh_token_value）持读锁读取快照。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

# trae-{uid}.json
AUTH_FILE_RE = re.compile(r"^trae-.*\.json$")


class AuthParseError(ValueError):
    """凭证解析失败（对齐原版的 storage_parse_error / parse_error）。"""


def _lookup(raw: dict, *names: str) -> Any:
    """按键取字段：先精确匹配，再大小写不敏感兜底（对齐 Go encoding/json 的行为）。"""
    for name in names:
        if name in raw:
            return raw[name]
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Go 的 getString 用 FormatFloat(-1)，即最短往返表示
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _as_int(value: Any) -> int:
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


class Auth:
    """归一化后的账号凭证。

    其余字段（domain/api_host/machine_id/device_id/uid/...）加载后不变，直接读。
    """

    __slots__ = (
        "_mu",
        "access_token",
        "refresh_token",
        "expires_at",
        "domain",
        "api_host",
        "machine_id",
        "device_id",
        "uid",
        "enterprise_id",
        "nickname",
        "file_path",
    )

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        expires_at: int = 0,
        domain: str = "",
        api_host: str = "",
        machine_id: str = "",
        device_id: str = "",
        uid: str = "",
        enterprise_id: str = "",
        nickname: str = "",
        file_path: str = "",
    ) -> None:
        # 可重入锁：refresh_token() 持写锁期间可能调用 save_atomic()（同样要锁）
        self._mu = threading.RLock()
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.domain = domain
        self.api_host = api_host
        self.machine_id = machine_id
        self.device_id = device_id
        self.uid = uid
        self.enterprise_id = enterprise_id
        self.nickname = nickname
        self.file_path = file_path

    # ------------------------------------------------------------------
    # 锁外壳（供 upstream / pool 复用）
    # ------------------------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        """返回底层可重入锁，便于 `with a.lock:` 使用。"""
        return self._mu

    # ------------------------------------------------------------------
    # 读路径（持读锁快照，防与 refresh 写并发竞态）
    # ------------------------------------------------------------------

    def jwt(self) -> str:
        """返回当前 access_token 的快照。"""
        with self._mu:
            return self.access_token

    def refresh_token_value(self) -> str:
        """返回当前 refresh_token 的快照。"""
        with self._mu:
            return self.refresh_token

    def expires_at_value(self) -> int:
        """返回当前 expires_at 的快照。"""
        with self._mu:
            return self.expires_at

    def needs_refresh(self, within: float) -> bool:
        """报告 token 是否将在 within 秒内过期（或已过期/无 expiry）。"""
        with self._mu:
            return self.needs_refresh_locked(within)

    def needs_refresh_locked(self, within: float) -> bool:
        """needs_refresh 的持锁内部版本；调用方必须已持有 self._mu。

        对齐 Go：`time.Now().Add(within).Unix() >= a.ExpiresAt`
        （Unix() 截断到秒，故此处同样取 int）。
        """
        if self.expires_at <= 0:
            return True
        return int(time.time() + within) >= self.expires_at

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    @staticmethod
    def _from_mapping(auth_raw: dict, account_raw: dict) -> "Auth":
        return Auth(
            access_token=_as_str(_lookup(auth_raw, "accessToken")),
            refresh_token=_as_str(_lookup(auth_raw, "refreshToken")),
            expires_at=_as_int(_lookup(auth_raw, "expiresAt")),
            domain=_as_str(_lookup(auth_raw, "domain")),
            api_host=_as_str(_lookup(auth_raw, "apiHost")),
            machine_id=_as_str(_lookup(auth_raw, "machineId")),
            device_id=_as_str(_lookup(auth_raw, "deviceId")),
            uid=_as_str(_lookup(account_raw, "uid")),
            enterprise_id=_as_str(_lookup(account_raw, "enterpriseId")),
            nickname=_as_str(_lookup(account_raw, "nickname")),
        )

    @staticmethod
    def parse_nested(raw: dict) -> "Auth":
        """兼容现有 trae-*.json 嵌套形：`{"account":{...},"auth":{...}}`。"""
        auth_raw = raw.get("auth")
        account_raw = raw.get("account")
        return Auth._from_mapping(
            auth_raw if isinstance(auth_raw, dict) else {},
            account_raw if isinstance(account_raw, dict) else {},
        )

    @staticmethod
    def parse_flat(raw: dict) -> "Auth":
        """兼容扁平形（面板手建等）：`{"accessToken":...,"uid":...}`。"""
        return Auth._from_mapping(raw, raw)

    @staticmethod
    def parse(raw: bytes | str | dict) -> "Auth":
        """兼容两种磁盘形态：嵌套形（登录脚本产出）与扁平形（手建）。"""
        if isinstance(raw, dict):
            doc: Any = raw
        else:
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            else:
                text = raw
            if not text or not text.strip():
                raise AuthParseError("empty auth storage")
            try:
                doc = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AuthParseError(f"storage_parse_error: {exc}") from exc

        if not isinstance(doc, dict):
            raise AuthParseError("storage_parse_error: top-level value must be an object")

        if "auth" in doc:
            auth = Auth.parse_nested(doc)
        else:
            auth = Auth.parse_flat(doc)

        if not auth.access_token.strip():
            raise AuthParseError("parse_error: missing accessToken")
        return auth

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def to_document(self) -> dict:
        """构造嵌套形文档（与原版 SaveAtomic 的输出字段一致）。"""
        with self._mu:
            return {
                "auth": {
                    "accessToken": self.access_token,
                    "refreshToken": self.refresh_token,
                    "expiresAt": self.expires_at,
                    "domain": self.domain,
                    "apiHost": self.api_host,
                    "machineId": self.machine_id,
                    "deviceId": self.device_id,
                },
                "account": {
                    "uid": self.uid,
                    "enterpriseId": self.enterprise_id,
                    "nickname": self.nickname,
                },
            }

    def save_atomic(self) -> None:
        """以嵌套形原子写回 file_path（tmp + rename，0600）。

        加锁外壳：防止与 refresh_token 并发读写 token 字段导致写回半更新。
        保持登录脚本可读格式（键按字典序，对齐 Go map 的序列化顺序）。
        """
        with self._mu:
            self._save_atomic_locked()

    def _save_atomic_locked(self) -> None:
        if not self.file_path:
            raise AuthParseError("no FilePath set")
        raw = json.dumps(
            self.to_document(), indent=2, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        path = Path(self.file_path)
        if path.parent and str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        # 先建文件再写入，确保 0600（Windows 上 chmod 语义有限，仍照做以对齐语义）
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(path))


def file_path_for(auth_dir: str, uid: str) -> str:
    """在已知 auth_dir 时构造 trae-{uid}.json 落盘路径。"""
    return str(Path(auth_dir) / f"trae-{uid}.json")


def mask_token(s: str, n: int) -> str:
    """保留前 n 字符 + 省略号；不足则全显示。用于面板 JSON 预览脱敏。"""
    if len(s) <= n:
        return s
    return s[:n] + f"…({len(s)} chars)"


def load_dir(directory: str) -> list[Auth]:
    """扫描 dir 下 trae-*.json。解析失败的文件静默跳过（启动日志由调用方统计）。"""
    out: list[Auth] = []
    d = Path(directory)
    if not d.is_dir():
        return out
    for entry in sorted(d.iterdir()):
        if not entry.is_file() or not AUTH_FILE_RE.match(entry.name):
            continue
        try:
            raw = entry.read_bytes()
        except OSError:
            continue
        try:
            auth = Auth.parse(raw)
        except AuthParseError:
            continue
        auth.file_path = str(entry)
        out.append(auth)
    return out
