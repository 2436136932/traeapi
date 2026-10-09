"""pidfile.py 服务自注册的 PID 文件。

动机（踩坑记录）：最初 PID 文件由 `start.ps1` 在启动后写入，因此**只有经脚本
启动的实例才可被追踪**。一旦服务以别的方式拉起（手动 `python -m traeapi`、
WMI/计划任务、`start.sh`），`start.ps1 -Stop` 就找不到它，而它又占着端口，
于是 `start.ps1` 报「端口已被占用」却无法自行解决 —— 用户被卡住。

改为**由服务自己**在启动时写、退出时删：无论谁拉起，状态都一致可追踪。

另外用**端口占用**作为兜底探测（见 scripts 里的说明）：即使 PID 文件丢失
（例如进程被强杀），也能通过端口找到并清理孤儿实例。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("traeapi.pidfile")

# PID 文件默认位置（与 state.json 同目录）
PID_FILE_NAME = "server.pid"


def pid_file_for(state_file: str) -> str:
    """由 state_file 推导 PID 文件路径（与 state.json 同目录）。"""
    text = (state_file or "").strip()
    if not text:
        return str(Path("data") / PID_FILE_NAME)
    parent = Path(text).parent
    if str(parent) in ("", "."):
        return str(Path("data") / PID_FILE_NAME)
    return str(parent / PID_FILE_NAME)


def write_pid_file(path: str) -> None:
    """写入当前进程 PID（原子写）。失败不致命，仅告警。"""
    if not path:
        return
    try:
        target = Path(path)
        if str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(target) + ".tmp")
        tmp.write_text(f"{os.getpid()}\n", encoding="ascii")
        os.replace(str(tmp), str(target))
    except OSError as exc:
        log.warning("写入 PID 文件失败 (%s): %s", path, exc)


def remove_pid_file(path: str) -> None:
    """删除 PID 文件；仅当它记录的正是当前进程时才删。

    避免「旧实例退出时误删新实例的 PID 文件」。
    """
    if not path:
        return
    try:
        target = Path(path)
        if not target.exists():
            return
        recorded = target.read_text(encoding="ascii", errors="replace").strip()
        if recorded and recorded != str(os.getpid()):
            return
        target.unlink()
    except OSError as exc:
        log.warning("删除 PID 文件失败 (%s): %s", path, exc)


def read_pid_file(path: str) -> int:
    """读取 PID 文件记录的进程号；不存在/损坏返回 0。"""
    try:
        text = Path(path).read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0
