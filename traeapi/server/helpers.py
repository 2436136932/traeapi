"""helpers.py admin 子包共用的小工具。

对应原版 internal/server/helpers.go。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from .. import jsonutil


async def decode_body_optional(request: Request) -> dict:
    """读 body（限 1MB）并 JSON 解码；body 为空时返回空 dict 不报错。

    用于 login start/cancel 等可选 body 的接口。
    """
    body = await request.body()
    if len(body) > (1 << 20):
        body = body[: 1 << 20]
    if not body:
        return {}
    try:
        parsed = jsonutil.loads(body)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def mkdir_all(directory: str) -> None:
    """递归创建目录。"""
    if not directory:
        return
    Path(directory).mkdir(parents=True, exist_ok=True)


def authorize_render(status: int, title: str, detail: str) -> HTMLResponse:
    """渲染 /authorize 回调后的浏览器可见页面（非 JSON）。

    复用 admin.html 的深色基调，但极简：一个标题 + 一段说明 + 自动关闭尝试。
    """
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>traeapi 登录</title><style>"
        'body{background:#0f1115;color:#e6e9ef;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;'
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".box{max-width:480px;padding:32px;text-align:center}"
        "h1{font-size:20px;font-weight:600;margin:0 0 12px}"
        "p{color:#8a93a6;line-height:1.6;margin:0 0 8px;word-break:break-all}"
        f'</style></head><body><div class="box"><h1>{title}</h1><p>{detail}</p>'
        '<p style="margin-top:20px;font-size:12px">窗口可关闭并返回 traeapi 控制台。</p>'
        "<script>try{setTimeout(function(){window.close()},3000);}catch(e){}</script>"
        "</div></body></html>"
    )
    return HTMLResponse(
        content=html,
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def prefix(s: str, n: int) -> str:
    """返回 s 前 n 字符（脱敏 machine/device id 展示）。"""
    if len(s) <= n:
        return s
    return s[:n] + "…"


def is_session_dead_err(err: Exception | None) -> bool:
    """粗判 refresh 失败是否 session 失效（含 401/invalid token 标记）。"""
    if err is None:
        return False
    msg = str(err).lower()
    for marker in ("session", "unauthorized", "401", "invalid token", "token 失效"):
        if marker in msg:
            return True
    return False
