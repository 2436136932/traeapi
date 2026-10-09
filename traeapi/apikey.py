"""apikey.py 面板可自定义的 API 密钥存储。

设计取舍（与原版 Go 的差异）：
  - 原版 Go 的密钥**只**从环境变量 TW2A_API_KEY 读取（`json:"-"`），
    想换密钥必须改 `.env` 再重启。
  - 本模块让面板可以在线修改密钥，并持久化到 `data/admin_key.json`
    （`data/` 已在 .gitignore 中，密钥不会进 git）。

优先级（启动时决定服务用哪个密钥）：

    1. data/admin_key.json  ← 面板在线设置的（用户显式意图，最高优先）
    2. TW2A_API_KEY 环境变量（start.ps1 / start.sh 从 .env 注入）
    3. 空 → 不鉴权（写操作也不需要 Key）

锁定恢复：删掉 `data/admin_key.json` 并重启即可回到环境变量 / 无鉴权状态。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# 密钥文件名（与 state.json 同目录）
KEY_FILE_NAME = "admin_key.json"

# 最短密钥长度。原版示例用过 123456（弱口令），这里给一个下限但不强制复杂度。
MIN_KEY_LENGTH = 6

# 并发写保护（面板可能被并发点击）
_write_lock = threading.Lock()


class ApiKeyError(ValueError):
    """密钥校验失败。"""


def key_file_for(state_file: str) -> str:
    """由 state_file 推导密钥文件路径（与 state.json 同目录）。

    state_file 为空或只有文件名时，退回 `data/admin_key.json`。
    """
    text = (state_file or "").strip()
    if not text:
        return str(Path("data") / KEY_FILE_NAME)
    parent = Path(text).parent
    if str(parent) in ("", "."):
        return str(Path("data") / KEY_FILE_NAME)
    return str(parent / KEY_FILE_NAME)


def load_stored_key(state_file: str) -> str:
    """读取面板持久化的密钥；不存在/损坏/为空时返回空串。"""
    path = Path(key_file_for(state_file))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    value = doc.get("api_key")
    if not isinstance(value, str):
        return ""
    return value.strip()


def save_stored_key(state_file: str, key: str) -> None:
    """原子写入密钥文件（tmp + rename，0600）。"""
    path = Path(key_file_for(state_file))
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"api_key": key}, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = Path(str(path) + ".tmp")
    with _write_lock:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(path))


def clear_stored_key(state_file: str) -> bool:
    """删除持久化密钥；删掉了返回 True。"""
    path = Path(key_file_for(state_file))
    with _write_lock:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def validate_key(raw: str) -> str:
    """校验并归一化新密钥，失败抛 ApiKeyError。"""
    key = (raw or "").strip()
    if not key:
        raise ApiKeyError("密钥不能为空")
    if len(key) < MIN_KEY_LENGTH:
        raise ApiKeyError(f"密钥至少 {MIN_KEY_LENGTH} 位")
    if any(ch.isspace() for ch in key):
        # Bearer 头里出现空白会导致请求无法解析，提前拦掉
        raise ApiKeyError("密钥不能包含空白字符")
    return key


def mask_key(key: str) -> str:
    """脱敏展示：最多露出首尾各 2~4 位，中间用省略号。"""
    if not key:
        return ""
    length = len(key)
    if length <= 4:
        return "…"
    if length <= 8:
        return f"{key[:2]}…{key[-2:]}"
    return f"{key[:4]}…{key[-4:]}"
