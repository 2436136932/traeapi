"""headers.py SOLO 三类请求头：对话（solo_headers）/ ug（ug_headers）/ oauth（oauth_headers）。

对应原版 internal/upstream/headers.go。
"""

from __future__ import annotations

import hashlib

from ..auth import Auth
from .constants import (
    AppID,
    DeviceBrand,
    IdeVersion,
    IdeVersionCode,
    OSVersion,
)

CLIENT_UA = "Trae/" + IdeVersion

# 派生设备号的域分隔前缀，避免与其他 sha256 用途撞语义。
UG_DEVICE_SEED_PREFIX = "traeapi-checkin-device:"


def solo_headers(auth: Auth, stream: bool) -> dict[str, str]:
    """设置 llm_utils_chat / get_detail_param 所需的 SOLO 专属头（实测必须）。"""
    at = auth.jwt()  # 读锁快照，防与 refresh 写并发竞态
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": CLIENT_UA,
        "Authorization": "Cloud-IDE-JWT " + at,
        "X-Cloudide-Token": at,
        "X-Ide-Token": at,
        "X-App-Id": AppID,
        "X-App-Version": "default",
        "X-Ide-Version": IdeVersion,
        "X-Ide-Version-Code": IdeVersionCode,
        "X-App-Version-Code": IdeVersionCode,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "windows",
        "X-OS-Version": OSVersion,
        "X-Device-Brand": DeviceBrand,
        "Request-Traffic-Type": "prod",
    }
    if auth.uid:
        headers["X-Uid"] = auth.uid
    if auth.machine_id:
        headers["X-Machine-Id"] = auth.machine_id
    if auth.device_id:
        headers["X-Device-Id"] = auth.device_id
    return headers


def ug_headers(auth: Auth) -> dict[str, str]:
    """设置签到/积分（api.trae.cn）所需头。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLIENT_UA,
        "Authorization": "Cloud-IDE-JWT " + auth.jwt(),  # 读锁快照
        "X-User-Region": "CN",
        # 设备号：签到时上游风控按它判定，必须提供（缺失 → 9004）
        "X-Device-Id": ug_device_id(auth),
    }


def oauth_headers() -> dict[str, str]:
    """设置 ExchangeToken / GetUserInfo 所需头（无签名，仅 UA）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLIENT_UA,
    }


def num16(s: str) -> bool:
    """判断是否为 16 位纯数字（TRAE 风控设备号的形态）。"""
    return len(s) == 16 and s.isdigit() and s.isascii()


def derived_ug_device_id(auth: Auth) -> str:
    """由账号 id 派生一个稳定的 16 位数字设备号（兜底用）。"""
    seed = auth.uid
    if not seed:
        seed = auth.jwt()  # 无 uid 时退化用 token 派生，仍保证稳定
    digest = hashlib.sha256((UG_DEVICE_SEED_PREFIX + seed).encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") % 10000000000000000
    return f"{value:016d}"


def ug_device_id(auth: Auth) -> str:
    """返回 ug 接口应使用的 x-device-id（首选值）。

    实测：账号当天**首次** claim 时，9074 与设备号取值强相关 ——
    x-device-id = uid → code 0 成功；32 位十六进制 GUID、sha256 派生 16 位、
    随机 16 位数字 → 9074；完全不发该头 → 9004 参数错误。
    账号当天已签到后，claim 幂等返回 code 0，不再校验设备号。

    因此首选 uid（TRAE 账号 id 本身即 16 位数字）；auth 文件里若是人工写的
    16 位数字 deviceId 则优先（便于换绑）；再不行才用派生值兜底。
    """
    if num16(auth.device_id):
        return auth.device_id
    if num16(auth.uid):
        return auth.uid
    return derived_ug_device_id(auth)


def uid_if16(auth: Auth) -> str:
    """返回 16 位数字的 uid，否则空串（非 16 位时不能当设备号用）。"""
    return auth.uid if num16(auth.uid) else ""


def checkin_device_plan(auth: Auth) -> list[str]:
    """返回 claim 时依次尝试的 x-device-id 候选（去重、非空）。

    首选 uid（实测唯一稳定通过的取值），随后是账号登录时的 GUID 与派生值兜底。
    """
    out: list[str] = []

    def add(value: str) -> None:
        if not value:
            return
        if value in out:
            return
        out.append(value)

    add(ug_device_id(auth))  # 首选：人工指定值 / uid
    add(uid_if16(auth))  # uid 始终保留为候选
    add(auth.device_id)  # 账号登录时记录的设备号（32 位十六进制）
    add(derived_ug_device_id(auth))  # 派生兜底（稳定且各账号互异）
    return out
