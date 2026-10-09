#!/usr/bin/env python3
"""credit — TRAE SOLO 积分查询（全部账号 + 指定账号 + 总计）。

对应原版 cmd/credit/main.go。

用法：

    python cmd/credit.py            # 原始 JSON
    python cmd/credit.py -pretty    # 人类可读日报
    python cmd/credit.py <uid>      # 指定账号
    python cmd/credit.py -pretty <uid>

数据源：POST {ug}/trae/api/v2/pay/ide_user_ent_usage，
聚合 user_entitlement_pay_list[].entitlement_base_info.quota.credits_limit。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

from traeapi.upstream import constants as C  # noqa: E402


def fetch_ent_usage(access_token: str, device_id: str, timeout: float = 20.0) -> tuple[int, int, int]:
    """查询单个账号的额度 → (remain, limit, packs)。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Cloud-IDE-JWT " + access_token,
        "X-User-Region": "CN",
    }
    # 这里沿用 auth 文件里的 deviceId（32 位十六进制）即可：实测积分接口
    # 对设备号形态不敏感；只有签到的 claim 会因非 16 位数字设备号被风控拒绝
    # （9074），故那份逻辑在 traeapi.upstream.headers 里。
    if device_id:
        headers["X-Device-Id"] = device_id

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(C.UgHost + C.EpEntUsage, headers=headers, content=b"{}")
    if resp.status_code >= 400:
        raise RuntimeError(f"http {resp.status_code}")

    try:
        env = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"invalid json: {exc}") from exc
    if not isinstance(env, dict):
        return 0, 0, 0

    packs_list = env.get("user_entitlement_pack_list")
    if not isinstance(packs_list, list):
        packs_list = []

    remain = limit = 0
    for pack in packs_list:
        if not isinstance(pack, dict):
            continue
        base = pack.get("entitlement_base_info")
        if not isinstance(base, dict):
            base = {}
        quota = base.get("quota")
        if not isinstance(quota, dict):
            quota = {}
        pack_limit = quota.get("credits_limit")
        if not isinstance(pack_limit, (int, float)) or pack_limit <= 0:
            continue
        usage = pack.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        used = usage.get("credits_amount")
        used = int(used) if isinstance(used, (int, float)) else 0
        limit += int(pack_limit)
        remain += int(pack_limit) - used
    return remain, limit, len(packs_list)


def parse_auth_file(path: Path) -> dict[str, Any] | None:
    """读取 auth 文件的嵌套形字段。"""
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
    account = doc.get("account") if isinstance(doc.get("account"), dict) else {}
    return {
        "access_token": auth.get("accessToken") or "",
        "device_id": auth.get("deviceId") or "",
        "uid": account.get("uid") or "",
        "nickname": account.get("nickname") or "",
    }


def print_pretty(accounts: list[dict], total_remain: int, ok_count: int) -> None:
    """人类可读日报。"""
    with_balance = 0
    failed: list[str] = []
    for account in accounts:
        if account["ok"] and account["remain"] is not None and account["remain"] > 0:
            with_balance += 1
        if not account["ok"]:
            name = account["nickname"]
            if not name and len(account["uid"]) >= 8:
                name = account["uid"][:8]
            failed.append(f"{name} {account['error']}")
    print("📊 TRAE SOLO 积分日报")
    print(f"账号: {with_balance}/{len(accounts)}")
    print(f"总计: {total_remain}")
    for line in failed:
        print(f"⚠️ {line}")


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    args = list(sys.argv[1:] if argv is None else argv)
    pretty = False
    want_uid = ""
    for arg in args:
        if arg == "-pretty":
            pretty = True
        elif arg == "-json":
            pass  # 默认 JSON 输出
        elif not arg.startswith("-"):
            want_uid = arg

    auth_dir = os.environ.get("TW2A_AUTH_DIR") or "./auths"
    base = Path(auth_dir)
    files = (
        sorted(p for p in base.iterdir() if p.is_file() and p.name.startswith("trae-") and p.name.endswith(".json"))
        if base.is_dir()
        else []
    )

    accounts: list[dict[str, Any]] = []
    for path in files:
        parsed = parse_auth_file(path)
        if parsed is None:
            continue
        if want_uid and parsed["uid"] != want_uid:
            continue

        result: dict[str, Any] = {
            "uid": parsed["uid"],
            "nickname": parsed["nickname"],
            "remain": None,
            "ok": False,
            "error": "",
        }
        if not parsed["access_token"]:
            result["error"] = "no accessToken"
            accounts.append(result)
            continue

        try:
            remain, limit, packs = fetch_ent_usage(parsed["access_token"], parsed["device_id"])
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        else:
            result["remain"] = remain
            result["limit"] = limit
            result["packages"] = packs
            result["ok"] = True
        accounts.append(result)
        time.sleep(0.2)

    total_remain = 0
    total_limit = 0
    ok_count = 0
    for account in accounts:
        if account["ok"] and account["remain"] is not None:
            ok_count += 1
            total_remain += account["remain"]
            if account.get("limit") is not None:
                total_limit += account["limit"]

    if pretty:
        print_pretty(accounts, total_remain, ok_count)
        return 0

    out = {
        "service": "traeapi",
        "ts": int(time.time()),
        "total": {
            "remain": total_remain,
            "limit": total_limit,
            "accounts": len(accounts),
            "ok": ok_count,
            "failed": len(accounts) - ok_count,
        },
        "accounts": accounts,
    }
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
