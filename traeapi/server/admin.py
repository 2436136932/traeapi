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
                "error": "no auth found",
            }
            return
        item: dict[str, Any] = {
            "uid": status.uid,
            "nickname": status.nickname,
            "cooling": status.cooling,
            "disabled": status.disabled,
        }
        error = ""
        try:
            remain, limit, used, packs = state.upstream.ent_usage(auth)
            item["remain"] = remain
            item["limit"] = limit
            item["used"] = used
            item["packs"] = packs
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
)


def admin_pools(state: ServerState) -> JSONResponse:
    """GET /admin/api/pools：各账号的积分包明细（只读）。

    用途：TRAE 额度由多个包组成、按包顺序扣费，所以「本次扣的是通用积分还是
    Work 专属积分」只能通过各包 usage 的变化看出来。本接口把每个包摊开返回，
    前端对比两次快照即可显示「自上次刷新以来哪个包被扣了多少」。
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
