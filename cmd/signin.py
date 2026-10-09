#!/usr/bin/env python3
"""signin — 一次性批量签到工具。

对应原版 cmd/signin/main.go：遍历 ./auths/trae-*.json 全部账号，
自动 refresh_token（过期时），逐个签到，顺手查积分。

用法：

    python cmd/signin.py            # 遍历 ./auths
    python cmd/signin.py auths      # 指定账号目录
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# 允许直接以脚本方式运行（把项目根加进 sys.path）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from traeapi import auth as auth_mod  # noqa: E402
from traeapi.upstream.client import Client, ErrKind, UpstreamError  # noqa: E402


@dataclass
class Row:
    """单账号签到结果行。"""

    file: str = ""
    uid: str = ""
    nick: str = ""
    status: str = ""  # OK | ALREADY | FAIL | AUTH_INVALID | LOAD_ERR
    detail: str = ""
    remain: int = 0
    has_rem: bool = False


def is_already(msg: str) -> bool:
    """已签判定：仅匹配明确表示「今日已签到」的业务错误。

    只用无歧义标记，避免 429/5xx body 含 "checkin" 字样被误判为已签。
    """
    s = (msg or "").lower()
    return "已签到" in s or "already check" in s or "already checked" in s


def trunc(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s


def short(s: str) -> str:
    s = (s or "").replace("\n", " ")
    return s[:60] if len(s) > 60 else s


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    args = list(sys.argv[1:] if argv is None else argv)
    directory = args[0] if args else "auths"

    base = Path(directory)
    if not base.is_dir():
        print(f"no auth files in {directory}", file=sys.stderr)
        return 1
    files = sorted(p for p in base.iterdir() if p.is_file() and p.name.startswith("trae-") and p.name.endswith(".json"))
    if not files:
        print(f"no auth files in {directory}", file=sys.stderr)
        return 1

    upstream = Client()

    rows: list[Row] = []
    ok_n = already_n = fail_n = 0

    try:
        for path in files:
            row = Row(file=path.name)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                row.status, row.detail = "LOAD_ERR", str(exc)
                rows.append(row)
                fail_n += 1
                continue
            try:
                account = auth_mod.Auth.parse(raw)
            except auth_mod.AuthParseError as exc:
                row.status, row.detail = "LOAD_ERR", str(exc)
                rows.append(row)
                fail_n += 1
                continue
            account.file_path = str(path)
            row.uid, row.nick = account.uid, account.nickname

            # refresh 过期 token
            if account.needs_refresh(2 * 3600):
                try:
                    upstream.refresh_token(account)
                except UpstreamError as exc:
                    row.status = "AUTH_INVALID" if exc.kind == ErrKind.SESSION_DEAD else "FAIL"
                    row.detail = "refresh: " + short(str(exc))
                    rows.append(row)
                    fail_n += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    row.status = "FAIL"
                    row.detail = "refresh: " + short(str(exc))
                    rows.append(row)
                    fail_n += 1
                    continue
                try:
                    account.save_atomic()
                except OSError:
                    pass

            # 签到
            try:
                checked_in, _, enable = upstream.checkin_status(account)
                status_err: Exception | None = None
            except Exception as exc:  # noqa: BLE001
                checked_in = False
                enable = False
                status_err = exc

            if status_err is not None:
                if is_already(str(status_err)):
                    row.status = "ALREADY"
                    row.detail = short(str(status_err))
                    already_n += 1
                else:
                    row.status = "FAIL"
                    row.detail = short(str(status_err))
                    fail_n += 1
            elif checked_in:
                row.status = "ALREADY"
                row.detail = "already checked in"
                already_n += 1
            elif not enable:
                row.status = "FAIL"
                row.detail = "checkin disabled"
                fail_n += 1
            else:
                try:
                    upstream.checkin_claim(account)
                    row.status = "OK"
                    ok_n += 1
                except Exception as exc:  # noqa: BLE001
                    row.status = "FAIL"
                    row.detail = short(str(exc))
                    fail_n += 1

            # 查积分
            try:
                row.remain = upstream.user_ent_usage(account)
                row.has_rem = True
            except Exception:  # noqa: BLE001
                pass

            rows.append(row)
    finally:
        upstream.close()

    # 报告
    print("uid                                  | nick        | status       | remain | detail")
    print("-------------------------------------+-------------+--------------+--------+------------------------------")
    for row in rows:
        remain = str(row.remain) if row.has_rem else "-"
        print(
            f"{trunc(row.uid, 36):<36} | {trunc(row.nick, 11):<11} | {row.status:<12} | "
            f"{remain:<6} | {row.detail}"
        )
    print(f"\ntotal={len(rows)} ok={ok_n} already={already_n} fail={fail_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
