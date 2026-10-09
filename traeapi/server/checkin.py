"""checkin.py /admin/api/checkin：一键签到（全账号并发执行）。

对应原版 internal/server/checkin.go。

行为对齐 cmd/signin 与 scheduler.run_checkin_now，区别是按需由面板触发：
 1. token 临近过期（<2h）先 refresh_token 并原子落盘，避免签到因 401 失败
 2. checkin_status 查询今日签到状态与可领积分
 3. 未签到且签到开关开启 → checkin_claim 领取（风控/限流类错误退避后重试）
 4. 重新查积分，remain > 0 且处于冷却的账号自动解冻（pool.reenable_if_credits）

安全纪律：写操作，经 require_admin（Bearer = TW2A_API_KEY）校验；
响应只含 uid/nickname/积分等脱敏信息，绝不返回 token。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse

from ..pool import Status
from ..upstream.client import CheckinError
from .state import ServerState

log = logging.getLogger("traeapi.checkin")

# 同时进行的签到账号数上限。上游对签到有限流，全量并发反而容易自伤，故做节流。
CHECKIN_MAX_PARALLEL = 4


@dataclass
class CheckinResult:
    """单账号签到结果。

    Action 取值：
      claimed    本次签到成功
      already    今日已签到
      no_checkin 签到功能未开启（enable=false）
      disabled   账号已被禁用（session dead 等）
      no_auth    缺少凭证或 refreshToken 为空
      failed     请求失败，原因见 error
    """

    uid: str
    nickname: str = ""
    action: str = ""
    checkin_credits: int = 0
    remain: int = 0
    # 上游业务错误码（如 9074「当前参与用户太多」），仅失败时有值。
    code: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "uid": self.uid,
            "action": self.action,
            "remain": self.remain,
        }
        if self.nickname:
            out["nickname"] = self.nickname
        if self.checkin_credits:
            out["checkin_credits"] = self.checkin_credits
        if self.code:
            out["code"] = self.code
        if self.error:
            out["error"] = self.error
        return out


async def admin_checkin(state: ServerState) -> JSONResponse:
    """POST /admin/api/checkin：对所有启用账号执行一次签到 + 积分刷新 + 解冻。"""
    statuses = state.pool.list()
    results: list[CheckinResult | None] = [None] * len(statuses)

    # 并发执行（单账号需 2~3 次上游请求），但限制同时在跑的账号数。
    if statuses:
        with ThreadPoolExecutor(max_workers=min(CHECKIN_MAX_PARALLEL, len(statuses))) as pool:
            futures = {
                pool.submit(checkin_one, state, status): idx
                for idx, status in enumerate(statuses)
            }
            for future, idx in futures.items():
                try:
                    results[idx] = future.result()
                except Exception as exc:  # noqa: BLE001 - 单账号异常不影响整体
                    results[idx] = CheckinResult(
                        uid=statuses[idx].uid,
                        nickname=statuses[idx].nickname,
                        action="failed",
                        error=str(exc),
                    )

    out = [r for r in results if r is not None]
    claimed = already = skipped = failed = 0
    for res in out:
        if res.action == "claimed":
            claimed += 1
        elif res.action == "already":
            already += 1
        elif res.action == "failed":
            failed += 1
        else:
            skipped += 1

    return JSONResponse(
        status_code=200,
        content={
            "total": len(out),
            "claimed": claimed,
            "already": already,
            "failed": failed,
            "skipped": skipped,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": [r.to_dict() for r in out],
        },
    )


def checkin_one(state: ServerState, status: Status) -> CheckinResult:
    """执行单个账号的签到流程，返回逐账号结果。"""
    res = CheckinResult(uid=status.uid, nickname=status.nickname)

    # 禁用的账号跳过（需人工重登恢复，签到救不回来）
    if status.disabled:
        res.action = "disabled"
        return res

    auth = state.pool.auth_by_uid(status.uid)
    if auth is None or not auth.refresh_token_value():
        res.action = "no_auth"
        res.error = "缺少凭证或 refreshToken 为空"
        return res

    # 1. token 临近过期先刷新（与 cmd/signin 一致）；刷新失败则本次签到直接判失败。
    if auth.needs_refresh(2 * 3600):
        try:
            state.upstream.refresh_token(auth)
        except Exception as exc:  # noqa: BLE001
            res.action = "failed"
            res.error = f"refresh: {exc}"
            return res
        try:
            auth.save_atomic()
        except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断签到（对齐 Go 的 _ =）
            log.warning("save refreshed token for %s failed: %s", status.uid, exc)

    # 2. 查询今日签到状态
    try:
        checked_in, credits, enable = state.upstream.checkin_status(auth)
        res.checkin_credits = credits
    except Exception as exc:  # noqa: BLE001
        res.action = "failed"
        res.error = f"checkin status: {exc}"
    else:
        if checked_in:
            res.action = "already"
        elif not enable:
            res.action = "no_checkin"
        else:
            # 3. 领取签到积分。设备号候选与 9074 退避重试都在 checkin_claim 内部完成。
            try:
                state.upstream.checkin_claim(auth)
                res.action = "claimed"
            except CheckinError as exc:
                res.action = "failed"
                # 业务错误直接用上游中文提示（如 9074「当前参与用户太多，请稍后再试」），
                # 比裸错误码更易读；code 单独暴露便于前端区分限流场景。
                res.code = exc.code
                res.error = exc.message
            except Exception as exc:  # noqa: BLE001
                res.action = "failed"
                res.error = f"checkin claim: {exc}"

    # 4. 刷新积分并在有额度时解冻冷却账号（与 scheduler 行为保持一致）
    try:
        remain = state.upstream.user_ent_usage(auth)
        res.remain = remain
        state.pool.reenable_if_credits(status.uid, remain)
    except Exception as exc:  # noqa: BLE001
        if res.action != "failed":
            res.error = f"ent_usage: {exc}"
    return res
