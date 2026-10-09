"""admin.py 管理面板（/admin）：页面 + 只读查询。

对应原版 internal/server/admin.go。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse

from .state import ServerState

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# 积分即将过期的预警窗口（天）。剩余积分在此窗口内到期时，前端会高亮提醒。
EXPIRING_SOON_DAYS = 7


def _expiry_summary(pools: list) -> dict:
    """汇总积分包的到期情况。

    返回：
      - expiring_soon_days：预警窗口（天）
      - expiring_soon_credits：窗口内将过期的剩余积分合计
      - expired_credits：已过期但仍有剩余的积分合计
      - next_expire_at：最近一个到期时间（Unix 秒，0 = 无）
    """
    now = int(time.time())
    window = EXPIRING_SOON_DAYS * 86400
    soon_credits = 0.0
    expired_credits = 0.0
    next_expire = 0
    for pool in pools:
        expire_at = getattr(pool, "expire_at", 0) or 0
        remain = getattr(pool, "remain", 0.0) or 0.0
        if not expire_at:
            continue
        if expire_at <= now:
            if remain > 0:
                expired_credits += remain
            continue
        if remain > 0 and expire_at - now <= window:
            soon_credits += remain
        if next_expire == 0 or expire_at < next_expire:
            next_expire = expire_at
    return {
        "expiring_soon_days": EXPIRING_SOON_DAYS,
        "expiring_soon_credits": round(soon_credits, 2),
        "expired_credits": round(expired_credits, 2),
        "next_expire_at": next_expire,
    }


def admin_page() -> HTMLResponse:
    """返回内嵌 HTML 面板（深色简洁风，无外部依赖）。"""
    html = (_TEMPLATE_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


def admin_credits(state: ServerState) -> JSONResponse:
    """查询全部账号的实时额度 + 签到状态（并发拉取上游）。"""
    statuses = state.pool.list()
    out: list[dict[str, Any]] = [{} for _ in statuses]

    def work(idx: int, status) -> None:
        auth = state.pool.auth_by_uid(status.uid)
        if auth is None:
            out[idx] = {
                "uid": status.uid,
                "nickname": status.nickname,
                "cooling": status.cooling,
                "disabled": status.disabled,
                "enabled": status.enabled,
                "error": "no auth found",
            }
            return
        item: dict[str, Any] = {
            "uid": status.uid,
            "nickname": status.nickname,
            "cooling": status.cooling,
            "disabled": status.disabled,
            # enabled 必须一并返回：否则前端无法区分「已停用（用户手动关闭）」。
            # 早先这里漏了该字段，导致「额度监控」页只显示冷却中/已禁用，
            # 被手动停用的账号看起来像在冷却，用户干等冷却到期也不会恢复。
            "enabled": status.enabled,
        }
        error = ""
        try:
            # ent_pools 与 ent_usage 打同一个端点，这里取明细后自行聚合，
            # 顺带拿到各包到期时间（避免为到期信息多发一次上游请求）。
            pools = state.upstream.ent_pools(auth)
            item["remain"] = int(sum(p.remain for p in pools))
            item["limit"] = int(sum(p.limit for p in pools))
            item["used"] = int(sum(p.used for p in pools))
            item["packs"] = len(pools)
            item.update(_expiry_summary(pools))
        except Exception as exc:  # noqa: BLE001
            error = f"ent_usage: {exc}"
        try:
            checked_in, credits, enable = state.upstream.checkin_status(auth)
            item["checked_in"] = checked_in
            item["checkin_credits"] = credits
            item["checkin_enable"] = enable
        except Exception as exc:  # noqa: BLE001
            error = (error + "; " if error else "") + f"checkin: {exc}"
        if error:
            item["error"] = error
        out[idx] = item

    if statuses:
        with ThreadPoolExecutor(max_workers=min(8, len(statuses))) as executor:
            futures = [
                executor.submit(work, idx, status) for idx, status in enumerate(statuses)
            ]
            for future in futures:
                future.result()

    return JSONResponse(
        status_code=200,
        content={
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": out,
        },
    )


# 面板提示：TRAE 额度由多个包组成、按包顺序扣费。
POOLS_NOTE = (
    "TRAE 额度由多个包组成、按包顺序扣费：通用积分（免费 / 每月赠送 / 签到等）用完后，"
    "才会动用 Work 专属积分。对比两次刷新就能看出当前扣的是哪个包；"
    "ID 为纯数字的包（如 358204062466）按上游特征视为 Work 专属积分。"
    "到期时间取自上游 expire_time（实测每个包各不相同：签到积分按天顺延约 31 天，"
    "月度赠送到当月底）；同类型包合并成一行时显示其中最早的到期时间，点「明细」看逐包时间。"
)


def admin_pools(state: ServerState) -> JSONResponse:
    """GET /admin/api/pools：各账号的积分包明细（只读）。

    用途：TRAE 额度由多个包组成、按包顺序扣费，所以「本次扣的是通用积分还是
    Work 专属积分」只能通过各包 usage 的变化看出来。本接口把每个包摊开返回，
    前端对比两次快照即可显示「自上次刷新以来哪个包被扣了多少」，
    并按 expire_at 显示各包的剩余到期时间。
    """
    statuses = state.pool.list()
    out: list[dict[str, Any]] = [{} for _ in statuses]

    def work(idx: int, status) -> None:
        auth = state.pool.auth_by_uid(status.uid)
        if auth is None:
            out[idx] = {
                "uid": status.uid,
                "nickname": status.nickname,
                "pools": [],
                "error": "no auth found",
            }
            return
        item: dict[str, Any] = {"uid": status.uid, "nickname": status.nickname, "pools": []}
        try:
            pools = state.upstream.ent_pools(auth)
            item["pools"] = [p.to_dict() for p in pools]
            # 到期汇总：窗口内将过期的积分、已过期未用完的积分、最近到期时间
            item.update(_expiry_summary(pools))
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)
        out[idx] = item

    if statuses:
        with ThreadPoolExecutor(max_workers=min(8, len(statuses))) as executor:
            futures = [
                executor.submit(work, idx, status) for idx, status in enumerate(statuses)
            ]
            for future in futures:
                future.result()

    return JSONResponse(
        status_code=200,
        content={"accounts": out, "note": POOLS_NOTE},
    )
