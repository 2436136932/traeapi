"""PID 文件自注册的测试。

背景（真实踩坑）：PID 文件原本只由 start.ps1 在启动后写入，导致以其他方式
拉起的实例（手动 / WMI / start.sh）无法被 -Stop 追踪，而它又占着端口，
用户会卡在「端口被占用却找不到进程」。
"""

from __future__ import annotations

import os
from pathlib import Path

from traeapi import pidfile


class TestPidFileFor:
    """PID 文件路径推导（与 state.json 同目录）。"""

    def test_from_state_file(self):
        assert pidfile.pid_file_for("./data/state.json").replace("\\", "/") == "data/server.pid"

    def test_from_bare_filename(self):
        assert pidfile.pid_file_for("state.json").replace("\\", "/") == "data/server.pid"

    def test_from_empty(self):
        assert pidfile.pid_file_for("").replace("\\", "/") == "data/server.pid"

    def test_from_absolute(self):
        got = pidfile.pid_file_for("/tmp/x/state.json").replace("\\", "/")
        assert got == "/tmp/x/server.pid"

    def test_custom_dir(self):
        got = pidfile.pid_file_for("/srv/traeapi/data/state.json").replace("\\", "/")
        assert got == "/srv/traeapi/data/server.pid"


class TestWriteReadRemove:
    """写入 / 读取 / 删除。"""

    def test_write_records_current_pid(self, tmp_path: Path):
        path = str(tmp_path / "server.pid")
        pidfile.write_pid_file(path)
        assert Path(path).exists()
        assert pidfile.read_pid_file(path) == os.getpid()
        # 无 .tmp 残留
        assert not (tmp_path / "server.pid.tmp").exists()

    def test_write_creates_parent_dir(self, tmp_path: Path):
        path = str(tmp_path / "nested" / "deep" / "server.pid")
        pidfile.write_pid_file(path)
        assert Path(path).exists()

    def test_write_empty_path_is_noop(self):
        pidfile.write_pid_file("")  # 不应抛异常

    def test_read_missing_returns_zero(self, tmp_path: Path):
        assert pidfile.read_pid_file(str(tmp_path / "nope.pid")) == 0

    def test_read_corrupt_returns_zero(self, tmp_path: Path):
        path = tmp_path / "server.pid"
        path.write_text("not-a-number", encoding="ascii")
        assert pidfile.read_pid_file(str(path)) == 0

    def test_read_tolerates_whitespace(self, tmp_path: Path):
        path = tmp_path / "server.pid"
        path.write_text("  12345\n", encoding="ascii")
        assert pidfile.read_pid_file(str(path)) == 12345

    def test_remove_own_pid(self, tmp_path: Path):
        path = str(tmp_path / "server.pid")
        pidfile.write_pid_file(path)
        pidfile.remove_pid_file(path)
        assert not Path(path).exists()

    def test_remove_missing_is_noop(self, tmp_path: Path):
        pidfile.remove_pid_file(str(tmp_path / "nope.pid"))  # 不应抛异常

    def test_remove_skips_foreign_pid(self, tmp_path: Path):
        """旧实例退出时不得误删新实例的 PID 文件。"""
        path = tmp_path / "server.pid"
        path.write_text("999999\n", encoding="ascii")
        pidfile.remove_pid_file(str(path))
        assert path.exists(), "属于其他进程的 PID 文件不应被删除"
        assert pidfile.read_pid_file(str(path)) == 999999

    def test_remove_empty_path_is_noop(self):
        pidfile.remove_pid_file("")  # 不应抛异常

    def test_write_overwrites_stale_content(self, tmp_path: Path):
        path = tmp_path / "server.pid"
        path.write_text("111111\n", encoding="ascii")
        pidfile.write_pid_file(str(path))
        assert pidfile.read_pid_file(str(path)) == os.getpid()

    def test_roundtrip_stable(self, tmp_path: Path):
        path = str(tmp_path / "server.pid")
        pidfile.write_pid_file(path)
        first = pidfile.read_pid_file(path)
        pidfile.write_pid_file(path)
        assert pidfile.read_pid_file(path) == first
