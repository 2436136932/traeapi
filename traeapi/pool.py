"""pool.py 账号池：内存索引 + 冷却/禁用状态机 + state.json 持久化。

对应原版 internal/pool/pool.go。
挑选策略：healthy 账号中剩余积分最多者。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

from .auth import Auth

# Go time.Time 的零值 JSON 表示（state.json 里未冷却时写的就是它）
GO_ZERO_TIME = "0001-01-01T00:00:00Z"
_GO_ZERO_DT = datetime(1, 1, 1, tzinfo=timezone.utc)


class CoolKind(IntEnum):
    """冷却类型。"""

    PLAN = 0  # 1005 plan 权益不足 → 12h 长冷却
    SOFT = 1  # 429/404 → 60s 短冷却（404 不累计 errCount）
    ERR = 2  # 连续错误 → 10m 中冷却

    def __str__(self) -> str:  # noqa: D105
        return {
            CoolKind.PLAN: "plan_limit",
            CoolKind.SOFT: "soft_rate",
            CoolKind.ERR: "error_threshold",
        }[self]


# ---------------------------------------------------------------------------
# 时间编解码（Go time.Time <-> RFC3339 字符串）
# ---------------------------------------------------------------------------


def format_go_time(dt: datetime | None) -> str:
    """把 datetime 序列化成 Go time.Time 的 RFC3339 形态（UTC，秒精度，Z 结尾）。

    零值（None / 1-01-01）输出 Go 的零值字面量，保证 state.json 与原版可互读。
    """
    if dt is None:
        return GO_ZERO_TIME
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year <= 1:
        return GO_ZERO_TIME
    if dt.microsecond:
        # RFC3339Nano 形态（去掉末尾多余的 0）
        frac = f"{dt.microsecond:06d}".rstrip("0")
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{frac}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_go_time(value: Any) -> datetime | None:
    """解析 Go time.Time 的 JSON 形态；零值/非法值返回 None（视作未冷却）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("0001-01-01"):
            return None
        candidate = text
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year <= 1:
        return None
    return dt


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 对外状态
# ---------------------------------------------------------------------------


@dataclass
class Status:
    """单个账号对外暴露的状态（脱敏，不含 token）。"""

    uid: str
    nickname: str = ""
    credits: int = 0
    cooling: bool = False
    until: datetime | None = None
    reason: str = ""
    # Disabled = session 失效硬禁用（需重登或换文件恢复）；
    # Enabled = 软开关（用户可逆启停）。
    # 对外暴露：Disabled 与 Enabled 都为 false 才算可被 Pick（healthy）。
    disabled: bool = False
    enabled: bool = True
    err_count: int = 0

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "uid": self.uid,
            "credits": self.credits,
            "cooling": self.cooling,
            "disabled": self.disabled,
            "enabled": self.enabled,
        }
        if self.nickname:
            out["nickname"] = self.nickname
        if self.cooling and self.until is not None:
            out["until"] = format_go_time(self.until)
        if self.reason:
            out["reason"] = self.reason
        if self.err_count:
            out["err_count"] = self.err_count
        return out


@dataclass
class _Entry:
    """池内条目。"""

    auth: Auth
    credits: int = 0
    disabled: bool = False  # session dead 硬禁用
    enabled: bool = True  # 用户软开关（默认 true），false 时 Pick 跳过
    reason: str = ""
    until: datetime | None = None
    err_count: int = 0

    def healthy(self, now: datetime) -> bool:
        if self.disabled or not self.enabled:
            return False
        if self.until is not None and now < self.until:
            return False
        return True


@dataclass
class _StateEntry:
    """state.json 单账号持久化条目。"""

    credits: int = 0
    disabled: bool = False
    # 指针语义：旧文件缺省时按 true 处理，且不写回脏值
    enabled: bool | None = None
    reason: str = ""
    until: datetime | None = None


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class Pool:
    """账号池。"""

    def __init__(self, state_fp: str = "") -> None:
        self._mu = threading.RLock()
        self._by_uid: dict[str, _Entry] = {}
        self.state_fp = state_fp or ""
        if self.state_fp:
            self._load()

    # ------------------------------------------------------------------
    # 增删对齐
    # ------------------------------------------------------------------

    def add(self, auth: Auth) -> None:
        """加入账号；已存在则保留原状态、更新凭证。"""
        with self._mu:
            entry = self._by_uid.get(auth.uid)
            if entry is not None:
                entry.auth = auth  # 保留 credits/cooling/enabled 状态
                return
            self._by_uid[auth.uid] = _Entry(auth=auth, enabled=True)

    def sync_to_dir(self, auths: list[Auth]) -> None:
        """用最新扫描结果对齐池：新账号加入、消失的账号剔除（状态保留）。"""
        with self._mu:
            seen: set[str] = set()
            for auth in auths:
                seen.add(auth.uid)
                entry = self._by_uid.get(auth.uid)
                if entry is not None:
                    entry.auth = auth
                else:
                    self._by_uid[auth.uid] = _Entry(auth=auth, enabled=True)
            for uid in list(self._by_uid):
                if uid not in seen:
                    del self._by_uid[uid]

    def remove(self, uid: str) -> bool:
        """删除账号（仅清内存索引与 state.json 条目；auths 文件由调用方删）。

        不存在返回 False，调用方据此决定 404。
        """
        with self._mu:
            if uid not in self._by_uid:
                return False
            del self._by_uid[uid]
            self._save_locked()
            return True

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def set_enabled(self, uid: str, enabled: bool, reason: str = "") -> bool:
        """切换账号软开关；不影响 disabled（session dead）状态。

        reason 仅在关闭时记录。不存在返回 False。
        """
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is None:
                return False
            entry.enabled = enabled
            if not enabled and reason:
                entry.reason = reason
            if enabled:
                # 重新启用时清掉软关闭的 reason；disabled/cooling 不动
                if entry.reason and not entry.disabled and entry.until is None:
                    entry.reason = ""
            self._save_locked()
            return True

    def pick(self) -> Auth | None:
        """返回 healthy 中积分最高的账号；无可用返回 None。"""
        return self.pick_excluding(None)

    def pick_excluding(self, tried: set[str] | None) -> Auth | None:
        """同 pick，但跳过 tried 中的 uid（请求级轮换）。"""
        with self._mu:
            now = _now()
            best: _Entry | None = None
            for uid, entry in self._by_uid.items():
                if tried is not None and uid in tried:
                    continue
                if not entry.healthy(now):
                    continue
                if best is None or entry.credits > best.credits:
                    best = entry
            return best.auth if best is not None else None

    def set_credits(self, uid: str, credits: int) -> None:
        """更新账号积分。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.credits = int(credits)
            self._save_locked()

    def cooldown(self, uid: str, kind: CoolKind, duration: float, reason: str = "") -> None:
        """冷却账号至 now + duration 秒。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.until = _now() + timedelta(seconds=duration)
                entry.reason = reason
                entry.err_count = 0
            self._save_locked()

    def disable(self, uid: str, reason: str = "") -> None:
        """永久禁用（session 失效），需人工重登后手工恢复或文件替换。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.disabled = True
                entry.reason = reason
            self._save_locked()

    def reenable_if_credits(self, uid: str, remain: int) -> None:
        """签到后解冻：仅当 remain > 0 且账号处于冷却（非禁用）时恢复。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.credits = int(remain)
                if remain > 0 and not entry.disabled:
                    entry.until = None
                    entry.reason = ""
                    entry.err_count = 0
            self._save_locked()

    def note_error(self, uid: str, threshold: int, duration: float) -> None:
        """记录一次错误；达到 threshold 自动冷却 duration 秒。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.err_count += 1
                if entry.err_count >= threshold:
                    entry.until = _now() + timedelta(seconds=duration)
                    entry.reason = "consecutive errors"
                    entry.err_count = 0
            self._save_locked()

    def note_success(self, uid: str) -> None:
        """成功请求重置错误计数。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is not None:
                entry.err_count = 0
            self._save_locked()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def status(self, uid: str) -> tuple[Status, bool]:
        """查询单账号状态。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            if entry is None:
                return Status(uid=uid), False
            return self._status_of(uid, entry), True

    def auth_by_uid(self, uid: str) -> Auth | None:
        """返回账号的完整凭证（给调度器/运维接口用）。"""
        with self._mu:
            entry = self._by_uid.get(uid)
            return entry.auth if entry is not None else None

    def list(self) -> list[Status]:
        """返回所有账号状态（按 UID 排序，稳定输出）。"""
        with self._mu:
            uids = sorted(self._by_uid.keys())
            return [self._status_of(uid, self._by_uid[uid]) for uid in uids]

    def _status_of(self, uid: str, entry: _Entry) -> Status:
        now = _now()
        return Status(
            uid=uid,
            nickname=entry.auth.nickname,
            credits=entry.credits,
            cooling=entry.until is not None and now < entry.until,
            until=entry.until,
            reason=entry.reason,
            disabled=entry.disabled,
            enabled=entry.enabled,
            err_count=entry.err_count,
        )

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = Path(self.state_fp).read_text(encoding="utf-8")
        except OSError:
            return
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(doc, dict):
            return
        accounts = doc.get("accounts")
        if not isinstance(accounts, dict):
            return
        for uid, item in accounts.items():
            if not isinstance(item, dict):
                continue
            enabled = True  # 旧文件无 enabled 字段 → 默认启用，向后兼容
            if "enabled" in item and item["enabled"] is not None:
                enabled = bool(item["enabled"])
            self._by_uid[str(uid)] = _Entry(
                # placeholder，add/sync 时会换成完整凭证
                auth=Auth(uid=str(uid)),
                credits=int(item.get("credits") or 0),
                disabled=bool(item.get("disabled") or False),
                enabled=enabled,
                reason=str(item.get("reason") or ""),
                until=parse_go_time(item.get("until")),
            )

    def _save_locked(self) -> None:
        if not self.state_fp:
            return
        accounts: dict[str, dict] = {}
        for uid, entry in self._by_uid.items():
            item: dict[str, Any] = {
                "credits": entry.credits,
                "disabled": entry.disabled,
            }
            # 仅在软关闭时写 enabled=false；默认 true 省略，旧版本读为 true
            if not entry.enabled:
                item["enabled"] = False
            if entry.reason:
                item["reason"] = entry.reason
            item["until"] = format_go_time(entry.until)
            accounts[uid] = item

        raw = json.dumps({"accounts": accounts}, indent=2, ensure_ascii=False).encode("utf-8")
        path = Path(self.state_fp)
        if path.parent and str(path.parent) not in ("", "."):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        tmp = Path(str(path) + ".tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            Path(str(tmp)).replace(path)
        except OSError:
            return
