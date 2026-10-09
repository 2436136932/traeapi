"""面板在线密钥管理（GET/POST/DELETE /admin/api/key）的测试。

这是 Python 版相对 Go 版新增的能力：原版密钥只能来自环境变量、必须重启；
这里验证在线修改后立即生效、持久化、以及优先级回退。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traeapi import apikey as apikey_mod
from traeapi.config import Config
from traeapi.server.state import build_state, resolve_api_key
from tests.conftest import make_pool, make_route_upstream, make_state, make_test_client


class TestApiKeyModule:
    """密钥存储模块本身。"""

    def test_key_file_for(self):
        assert apikey_mod.key_file_for("./data/state.json").replace("\\", "/") == "data/admin_key.json"
        assert apikey_mod.key_file_for("state.json").replace("\\", "/") == "data/admin_key.json"
        assert apikey_mod.key_file_for("").replace("\\", "/") == "data/admin_key.json"
        assert apikey_mod.key_file_for("/tmp/x/state.json").replace("\\", "/") == "/tmp/x/admin_key.json"

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        apikey_mod.save_stored_key(state_file, "my-secret-key")
        assert apikey_mod.load_stored_key(state_file) == "my-secret-key"
        # 落盘文件不含多余信息，且没有 .tmp 残留
        doc = json.loads((tmp_path / "admin_key.json").read_text(encoding="utf-8"))
        assert doc == {"api_key": "my-secret-key"}
        assert not (tmp_path / "admin_key.json.tmp").exists()

    def test_load_missing_or_corrupt(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        assert apikey_mod.load_stored_key(state_file) == ""
        (tmp_path / "admin_key.json").write_text("{broken", encoding="utf-8")
        assert apikey_mod.load_stored_key(state_file) == ""
        (tmp_path / "admin_key.json").write_text('{"api_key": ""}', encoding="utf-8")
        assert apikey_mod.load_stored_key(state_file) == ""

    def test_clear(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        apikey_mod.save_stored_key(state_file, "abc123")
        assert apikey_mod.clear_stored_key(state_file) is True
        assert apikey_mod.load_stored_key(state_file) == ""
        assert apikey_mod.clear_stored_key(state_file) is False  # 已不存在

    @pytest.mark.parametrize("value", ["", "   ", "abc", "12345"])
    def test_validate_too_short(self, value):
        with pytest.raises(apikey_mod.ApiKeyError):
            apikey_mod.validate_key(value)

    def test_validate_rejects_whitespace(self):
        with pytest.raises(apikey_mod.ApiKeyError):
            apikey_mod.validate_key("abc 123456")

    def test_validate_trims(self):
        assert apikey_mod.validate_key("  abcdef123  ") == "abcdef123"

    def test_mask_key(self):
        assert apikey_mod.mask_key("") == ""
        assert apikey_mod.mask_key("abcd") == "…"
        assert apikey_mod.mask_key("abcdefgh") == "ab…gh"
        assert apikey_mod.mask_key("abcdefghijkl") == "abcd…ijkl"


class TestResolveApiKey:
    """启动时的密钥优先级。"""

    def test_stored_beats_env(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        apikey_mod.save_stored_key(state_file, "stored-key")
        cfg = Config(api_key="env-key", state_file=state_file)
        key, source = resolve_api_key(cfg, state_file)
        assert (key, source) == ("stored-key", "stored")

    def test_env_when_no_stored(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        cfg = Config(api_key="env-key", state_file=state_file)
        key, source = resolve_api_key(cfg, state_file)
        assert (key, source) == ("env-key", "env")

    def test_none_when_nothing(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        cfg = Config(api_key="", state_file=state_file)
        key, source = resolve_api_key(cfg, state_file)
        assert (key, source) == ("", "none")

    def test_build_state_uses_stored_key(self, tmp_path: Path):
        state_file = str(tmp_path / "state.json")
        apikey_mod.save_stored_key(state_file, "stored-key")
        cfg = Config(api_key="env-key", state_file=state_file)
        state = build_state(cfg, make_pool(), make_route_upstream({}))
        assert state.current_key() == "stored-key"
        assert state.key_source == "stored"
        assert state.key_status()["masked"] == "stor…-key"


class TestKeyEndpoints:
    """HTTP 层的密钥管理接口。"""

    def test_get_key_status_masks(self, tmp_path: Path):
        state = make_state(
            make_pool(),
            make_route_upstream({}),
            api_key="super-secret-key",
        )
        state.key_file = str(tmp_path / "admin_key.json")
        state.key_source = "env"
        client = make_test_client(state)

        resp = client.get("/admin/api/key")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["source"] == "env"
        assert body["length"] == len("super-secret-key")
        assert body["min_length"] == 6
        # 绝不泄漏明文
        assert "super-secret-key" not in resp.text

    def test_get_key_status_when_unset(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}))
        state.key_file = str(tmp_path / "admin_key.json")
        state.set_key("", "none")
        client = make_test_client(state)
        body = client.get("/admin/api/key").json()
        assert body["configured"] is False
        assert body["masked"] == ""

    def test_set_key_requires_old_key(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        client = make_test_client(state)

        # 无 Key → 401
        assert client.post("/admin/api/key", json={"api_key": "new-key-456"}).status_code == 401
        # 错 Key → 401
        assert client.post(
            "/admin/api/key",
            json={"api_key": "new-key-456"},
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        # 旧 Key 正确 → 200
        resp = client.post(
            "/admin/api/key",
            json={"api_key": "new-key-456"},
            headers={"Authorization": "Bearer old-key-123"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

    def test_set_key_takes_effect_immediately(self, tmp_path: Path):
        """改密钥后：旧 Key 失效、新 Key 可用，且无需重启。"""
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        client = make_test_client(state)

        client.post(
            "/admin/api/key",
            json={"api_key": "new-key-456"},
            headers={"Authorization": "Bearer old-key-123"},
        )
        # 旧 Key 立即失效
        assert client.get(
            "/v1/models", headers={"Authorization": "Bearer old-key-123"}
        ).status_code == 401
        # 新 Key 立即可用
        assert client.get(
            "/v1/models", headers={"Authorization": "Bearer new-key-456"}
        ).status_code == 200

    def test_set_key_persists_to_file(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        key_file = tmp_path / "admin_key.json"
        state.key_file = str(key_file)
        client = make_test_client(state)

        client.post(
            "/admin/api/key",
            json={"api_key": "persisted-key-789"},
            headers={"Authorization": "Bearer old-key-123"},
        )
        assert json.loads(key_file.read_text(encoding="utf-8")) == {
            "api_key": "persisted-key-789"
        }

    def test_set_key_validates_length(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        client = make_test_client(state)
        resp = client.post(
            "/admin/api/key",
            json={"api_key": "abc"},
            headers={"Authorization": "Bearer old-key-123"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_api_key_value"
        # 密钥未被改动
        assert state.current_key() == "old-key-123"

    def test_set_key_missing_field(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        client = make_test_client(state)
        resp = client.post(
            "/admin/api/key", json={}, headers={"Authorization": "Bearer old-key-123"}
        )
        assert resp.status_code == 400

    def test_set_same_key_is_idempotent(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="same-key-123")
        key_file = tmp_path / "admin_key.json"
        state.key_file = str(key_file)
        client = make_test_client(state)
        resp = client.post(
            "/admin/api/key",
            json={"api_key": "same-key-123"},
            headers={"Authorization": "Bearer same-key-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["changed"] is False
        assert not key_file.exists()  # 未重复落盘

    def test_clear_key_disables_auth(self, tmp_path: Path):
        """清除密钥后服务不再鉴权（写操作免 Key）。"""
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        key_file = tmp_path / "admin_key.json"
        state.key_file = str(key_file)
        client = make_test_client(state)

        resp = client.delete("/admin/api/key", headers={"Authorization": "Bearer old-key-123"})
        assert resp.status_code == 200
        assert resp.json()["removed"] is False  # 本来就没有持久化文件
        assert state.current_key() == ""

        # 现在无需 Key 也能读
        assert client.get("/v1/models").status_code == 200
        # 也无需 Key 就能改密钥
        assert client.post("/admin/api/key", json={"api_key": "fresh-key-999"}).status_code == 200

    def test_clear_key_requires_current_key(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        client = make_test_client(state)
        assert client.delete("/admin/api/key").status_code == 401
        assert client.delete(
            "/admin/api/key", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401

    def test_clear_then_reload_returns_to_env_key(self, tmp_path: Path):
        """清除持久化密钥后，重启应回退到环境变量里的密钥。"""
        state_file = str(tmp_path / "state.json")
        apikey_mod.save_stored_key(state_file, "stored-key-1")

        cfg = Config(api_key="env-key-999", state_file=state_file)
        state = build_state(cfg, make_pool(), make_route_upstream({}))
        assert state.current_key() == "stored-key-1"

        client = make_test_client(state)
        client.delete("/admin/api/key", headers={"Authorization": "Bearer stored-key-1"})

        # 模拟重启：重新 resolve
        key, source = resolve_api_key(cfg, state_file)
        assert (key, source) == ("env-key-999", "env")

    def test_key_status_reflects_change(self, tmp_path: Path):
        state = make_state(make_pool(), make_route_upstream({}), api_key="old-key-123")
        state.key_file = str(tmp_path / "admin_key.json")
        state.key_source = "env"
        client = make_test_client(state)
        client.post(
            "/admin/api/key",
            json={"api_key": "brand-new-key-1"},
            headers={"Authorization": "Bearer old-key-123"},
        )
        body = client.get("/admin/api/key").json()
        assert body["source"] == "stored"
        assert body["length"] == len("brand-new-key-1")
        assert "brand-new-key-1" not in json.dumps(body)
