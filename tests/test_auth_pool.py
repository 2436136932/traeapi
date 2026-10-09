"""auth 与 pool 的测试（对应 Go 的 auth_test.go / pool_test.go）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from traeapi.auth import Auth, AuthParseError, file_path_for, load_dir, mask_token
from traeapi.pool import CoolKind, Pool, format_go_time, parse_go_time

NESTED = {
    "account": {"uid": "u1", "enterpriseId": "ent-1", "nickname": "Alice"},
    "auth": {
        "accessToken": "at-1",
        "refreshToken": "rt-1",
        "expiresAt": 1792634143,
        "domain": "trae.cn",
        "apiHost": "https://api.trae.com.cn",
        "machineId": "m" * 32,
        "deviceId": "d" * 32,
    },
}

FLAT = {
    "accessToken": "at-flat",
    "refreshToken": "rt-flat",
    "expiresAt": 1792634143,
    "uid": "u-flat",
    "nickname": "Flat",
    "machineId": "m" * 32,
    "deviceId": "d" * 32,
}


class TestAuthParse:
    """Auth.parse 的两种磁盘形态。"""

    def test_parse_existing_nested_format(self):
        auth = Auth.parse(json.dumps(NESTED))
        assert auth.access_token == "at-1"
        assert auth.refresh_token == "rt-1"
        assert auth.expires_at == 1792634143
        assert auth.domain == "trae.cn"
        assert auth.api_host == "https://api.trae.com.cn"
        assert auth.machine_id == "m" * 32
        assert auth.device_id == "d" * 32
        assert auth.uid == "u1"
        assert auth.enterprise_id == "ent-1"
        assert auth.nickname == "Alice"

    def test_parse_flat(self):
        auth = Auth.parse(json.dumps(FLAT))
        assert auth.access_token == "at-flat"
        assert auth.uid == "u-flat"
        assert auth.nickname == "Flat"

    def test_parse_missing_token(self):
        with pytest.raises(AuthParseError):
            Auth.parse(json.dumps({"account": {"uid": "u1"}, "auth": {}}))

    def test_parse_empty(self):
        with pytest.raises(AuthParseError):
            Auth.parse("")

    def test_parse_bad_json(self):
        with pytest.raises(AuthParseError):
            Auth.parse("{broken")


class TestAuthPersistence:
    """原子写回与目录扫描。"""

    def test_save_atomic_roundtrip_preserves_solo_fields(self, tmp_path: Path):
        path = tmp_path / "trae-u1.json"
        auth = Auth(
            access_token="at",
            refresh_token="rt",
            expires_at=123,
            domain="trae.cn",
            api_host="https://api.trae.com.cn",
            machine_id="m" * 32,
            device_id="d" * 32,
            uid="u1",
            enterprise_id="e1",
            nickname="Nick",
            file_path=str(path),
        )
        auth.save_atomic()
        assert path.exists()

        reloaded = Auth.parse(path.read_bytes())
        assert reloaded.access_token == "at"
        assert reloaded.refresh_token == "rt"
        assert reloaded.expires_at == 123
        assert reloaded.machine_id == "m" * 32
        assert reloaded.device_id == "d" * 32
        assert reloaded.uid == "u1"
        assert reloaded.enterprise_id == "e1"
        assert reloaded.nickname == "Nick"

        # 落盘形态是嵌套形，且没有 .tmp 残留
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert "auth" in doc and "account" in doc
        assert not (tmp_path / "trae-u1.json.tmp").exists()

    def test_save_atomic_requires_file_path(self):
        with pytest.raises(AuthParseError):
            Auth(access_token="at", uid="u1").save_atomic()

    def test_load_dir(self, tmp_path: Path):
        (tmp_path / "trae-u1.json").write_text(json.dumps(NESTED), encoding="utf-8")
        (tmp_path / "trae-bad.json").write_text("{broken", encoding="utf-8")
        (tmp_path / "other.json").write_text(json.dumps(NESTED), encoding="utf-8")

        auths = load_dir(str(tmp_path))
        assert len(auths) == 1
        assert auths[0].uid == "u1"
        assert auths[0].file_path.endswith("trae-u1.json")

    def test_load_dir_missing(self, tmp_path: Path):
        assert load_dir(str(tmp_path / "nope")) == []

    def test_file_path_for(self):
        assert file_path_for("./auths", "u1").replace("\\", "/").endswith("auths/trae-u1.json")

    def test_mask_token(self):
        assert mask_token("abcdefghijklmnop", 12) == "abcdefghijkl…(16 chars)"
        assert mask_token("short", 12) == "short"


class TestAuthRefresh:
    """needs_refresh 的边界与并发安全。"""

    def test_needs_refresh(self):
        assert Auth(access_token="t", expires_at=0).needs_refresh(3600) is True
        assert Auth(access_token="t", expires_at=9999999999).needs_refresh(3600) is False
        # 已过期
        assert Auth(access_token="t", expires_at=1).needs_refresh(3600) is True

    def test_needs_refresh_locked(self):
        auth = Auth(access_token="t", expires_at=0)
        with auth.lock:
            assert auth.needs_refresh_locked(60) is True

    def test_concurrent_refresh_and_reads(self):
        """并发读 token 与写 token 不应抛异常或读到撕裂值。"""
        auth = Auth(access_token="a" * 100, refresh_token="r", expires_at=9999999999)
        errors: list[BaseException] = []

        def writer():
            try:
                for _ in range(200):
                    with auth.lock:
                        auth.access_token = "b" * 100
                        auth.access_token = "a" * 100
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(200):
                    value = auth.jwt()
                    assert value in ("a" * 100, "b" * 100)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestPool:
    """账号池挑选与状态机。"""

    def _pool(self) -> Pool:
        pool = Pool("")
        for uid in ("u1", "u2", "u3"):
            pool.add(Auth(uid=uid, access_token=f"at-{uid}"))
        pool.set_credits("u1", 100)
        pool.set_credits("u2", 500)
        pool.set_credits("u3", 300)
        return pool

    def test_pick_highest_credits(self):
        assert self._pool().pick().uid == "u2"

    def test_pick_skips_cooling(self):
        pool = self._pool()
        pool.cooldown("u2", CoolKind.SOFT, 60, "rate")
        assert pool.pick().uid == "u3"

    def test_pick_expired_cooldown_returns_to_healthy(self):
        pool = self._pool()
        pool.cooldown("u2", CoolKind.SOFT, -1, "rate")  # 已过期
        assert pool.pick().uid == "u2"

    def test_pick_nil_when_all_cooling(self):
        pool = self._pool()
        for uid in ("u1", "u2", "u3"):
            pool.cooldown(uid, CoolKind.PLAN, 3600, "plan")
        assert pool.pick() is None

    def test_pick_excluding(self):
        pool = self._pool()
        assert pool.pick_excluding({"u2"}).uid == "u3"
        assert pool.pick_excluding({"u2", "u3"}).uid == "u1"
        assert pool.pick_excluding({"u1", "u2", "u3"}) is None

    def test_cooldown_persists(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        pool = Pool(str(state_file))
        pool.add(Auth(uid="u1", access_token="t"))
        pool.cooldown("u1", CoolKind.PLAN, 3600, "plan 权益不足")

        reloaded = Pool(str(state_file))
        reloaded.add(Auth(uid="u1", access_token="t"))
        status, ok = reloaded.status("u1")
        assert ok and status.cooling is True
        assert status.reason == "plan 权益不足"

    def test_disable_persists(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        pool = Pool(str(state_file))
        pool.add(Auth(uid="u1", access_token="t"))
        pool.disable("u1", "session dead")

        reloaded = Pool(str(state_file))
        reloaded.add(Auth(uid="u1", access_token="t"))
        status, _ = reloaded.status("u1")
        assert status.disabled is True
        assert reloaded.pick() is None

    def test_reenable_if_credits(self):
        pool = self._pool()
        pool.cooldown("u2", CoolKind.PLAN, 3600, "plan")
        pool.reenable_if_credits("u2", 42)
        status, _ = pool.status("u2")
        assert status.cooling is False
        assert status.credits == 42
        assert status.reason == ""

    def test_reenable_zero_credits_keeps_cooling(self):
        pool = self._pool()
        pool.cooldown("u2", CoolKind.PLAN, 3600, "plan")
        pool.reenable_if_credits("u2", 0)
        status, _ = pool.status("u2")
        assert status.cooling is True
        assert status.credits == 0

    def test_reenable_does_not_touch_disabled(self):
        pool = self._pool()
        pool.disable("u2", "session dead")
        pool.reenable_if_credits("u2", 999)
        status, _ = pool.status("u2")
        assert status.disabled is True
        assert status.cooling is False

    def test_note_error_threshold(self):
        pool = self._pool()
        for _ in range(2):
            pool.note_error("u1", 3, 600)
        status, _ = pool.status("u1")
        assert status.err_count == 2 and status.cooling is False

        pool.note_error("u1", 3, 600)
        status, _ = pool.status("u1")
        assert status.cooling is True
        assert status.reason == "consecutive errors"
        assert status.err_count == 0

    def test_note_success_resets_counter(self):
        pool = self._pool()
        pool.note_error("u1", 3, 600)
        pool.note_success("u1")
        status, _ = pool.status("u1")
        assert status.err_count == 0

    def test_list_sorted_by_uid(self):
        pool = self._pool()
        assert [s.uid for s in pool.list()] == ["u1", "u2", "u3"]

    def test_sync_to_dir_removes_missing(self):
        pool = self._pool()
        pool.sync_to_dir([Auth(uid="u2", access_token="t")])
        assert [s.uid for s in pool.list()] == ["u2"]

    def test_sync_to_dir_keeps_state(self):
        pool = self._pool()
        pool.set_credits("u2", 777)
        pool.sync_to_dir([Auth(uid="u2", access_token="new")])
        status, _ = pool.status("u2")
        assert status.credits == 777

    def test_remove(self):
        pool = self._pool()
        assert pool.remove("u2") is True
        assert pool.remove("u2") is False
        assert [s.uid for s in pool.list()] == ["u1", "u3"]

    def test_remove_clears_state_entry(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        pool = Pool(str(state_file))
        pool.add(Auth(uid="u1", access_token="t"))
        pool.add(Auth(uid="u2", access_token="t"))
        pool.remove("u1")

        doc = json.loads(state_file.read_text(encoding="utf-8"))
        assert "u1" not in doc["accounts"]
        assert "u2" in doc["accounts"]

    def test_set_enabled_soft_switch(self):
        pool = self._pool()
        assert pool.set_enabled("u2", False, "user disabled") is True
        status, _ = pool.status("u2")
        assert status.enabled is False
        assert status.reason == "user disabled"
        # 被软关闭的账号不参与挑选
        assert pool.pick().uid == "u3"

        pool.set_enabled("u2", True, "")
        status, _ = pool.status("u2")
        assert status.enabled is True
        assert status.reason == ""
        assert pool.pick().uid == "u2"

    def test_set_enabled_does_not_affect_disabled(self):
        pool = self._pool()
        pool.disable("u2", "session dead")
        pool.set_enabled("u2", True, "")
        status, _ = pool.status("u2")
        assert status.disabled is True
        assert status.enabled is True
        assert pool.pick().uid == "u3"

    def test_set_enabled_on_missing(self):
        assert self._pool().set_enabled("nope", False, "x") is False

    def test_set_enabled_clears_reason_when_no_cooldown(self):
        """无冷却时重新启用 → reason 被清掉。"""
        pool = self._pool()
        pool.set_enabled("u1", False, "user disabled")
        pool.set_enabled("u1", True, "")
        status, _ = pool.status("u1")
        assert status.enabled is True
        assert status.reason == ""

    def test_set_enabled_clears_stale_reason_after_cooldown_expired(self):
        """冷却过期后重新启用 → 必须清掉残留的 reason（真实踩坑）。

        until 在冷却到期后不会被清理（仍是过去的时间戳），若用
        `until is None` 作条件，就会漏掉这种情况，面板显示
        「已启用」却挂着 "user disabled" 的理由，自相矛盾。
        """
        pool = self._pool()
        # 设一个必然已过期的冷却
        pool.cooldown("u1", CoolKind.SOFT, -1, "429 rate limit")
        status, _ = pool.status("u1")
        assert status.cooling is False  # 已过期
        assert status.until is not None  # 但时间戳仍在

        pool.set_enabled("u1", False, "user disabled")
        pool.set_enabled("u1", True, "")

        status, _ = pool.status("u1")
        assert status.enabled is True
        assert status.cooling is False
        assert status.reason == "", f"过期冷却的 reason 未清理: {status.reason!r}"
        # 账号实际可用（排除积分更高的 u2/u3 单独验证 u1）
        assert pool.pick_excluding({"u2", "u3"}).uid == "u1"

    def test_set_enabled_keeps_reason_while_still_cooling(self):
        """仍在冷却中时重新启用 → reason 不被清掉（避免丢失状态说明）。"""
        pool = self._pool()
        pool.cooldown("u1", CoolKind.PLAN, 3600, "plan 权益不足")
        pool.set_enabled("u1", False, "user disabled")
        pool.set_enabled("u1", True, "")
        status, _ = pool.status("u1")
        assert status.cooling is True
        assert status.enabled is True
        assert status.reason != ""

    def test_set_enabled_keeps_reason_when_hard_disabled(self):
        """硬禁用（session dead）时重新启用 → reason 保留。"""
        pool = self._pool()
        pool.disable("u2", "session dead")
        pool.set_enabled("u2", True, "")
        status, _ = pool.status("u2")
        assert status.disabled is True
        assert status.reason == "session dead"

    def test_set_enabled_persists_across_reload(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        pool = Pool(str(state_file))
        pool.add(Auth(uid="u1", access_token="t"))
        pool.set_enabled("u1", False, "user disabled")

        reloaded = Pool(str(state_file))
        reloaded.add(Auth(uid="u1", access_token="t"))
        status, _ = reloaded.status("u1")
        assert status.enabled is False

    def test_state_file_backward_compat(self, tmp_path: Path):
        """旧 state.json 无 enabled 字段 → 默认启用；until 为 Go 零值 → 不冷却。"""
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "accounts": {
                        "u1": {
                            "credits": 2574,
                            "disabled": False,
                            "until": "0001-01-01T00:00:00Z",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        pool = Pool(str(state_file))
        pool.add(Auth(uid="u1", access_token="t"))
        status, ok = pool.status("u1")
        assert ok
        assert status.enabled is True
        assert status.cooling is False
        assert status.credits == 2574
        assert pool.pick().uid == "u1"


class TestGoTime:
    """Go time.Time 的 JSON 编解码兼容。"""

    def test_zero_time(self):
        assert format_go_time(None) == "0001-01-01T00:00:00Z"
        assert parse_go_time("0001-01-01T00:00:00Z") is None

    def test_roundtrip(self):
        text = "2026-10-09T02:34:05Z"
        parsed = parse_go_time(text)
        assert parsed is not None
        assert format_go_time(parsed) == text

    def test_invalid(self):
        assert parse_go_time("not-a-time") is None
        assert parse_go_time("") is None
        assert parse_go_time(None) is None
