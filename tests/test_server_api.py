"""server 层测试（对应 Go 的 handler_test.go / admin_ext_test.go / checkin_test.go）。"""

from __future__ import annotations

import json

import httpx

from traeapi.auth import Auth
from traeapi.upstream import constants as C
from tests.conftest import (
    SOLO_SSE,
    make_auth,
    make_pool,
    make_primed_state,
    make_route_upstream,
    make_state,
    make_test_client,
    make_upstream,
)


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------


class TestChatCompletions:
    """对话接口的聚合、流式与轮换。"""

    def test_non_stream_aggregates(self):
        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            assert request.headers.get("Authorization") == "Cloud-IDE-JWT at1"
            return 200, SOLO_SSE, True

        state = make_primed_state(make_pool(Auth(uid="u1", access_token="at1", expires_at=9999999999)),
                           make_upstream(behavior))
        client = make_test_client(state)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "chat.completion"
        message = body["choices"][0]["message"]
        assert message["content"] == "你好"
        assert message["reasoning_content"] == "想一下"

    def test_stream_passthrough(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = resp.text
        assert "你好" in text
        assert "data: [DONE]" in text
        assert '"object":"chat.completion.chunk"' in text

    def test_rotates_on_plan_limit(self):
        calls: dict[str, int] = {}

        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            authz = request.headers.get("Authorization") or ""
            calls[authz] = calls.get(authz, 0) + 1
            if authz == "Cloud-IDE-JWT at-bad":
                return 200, 'event:error\ndata:{"code":1005,"message":"plan limit","extra":{"plan":2}}\n\n', True
            return 200, SOLO_SSE, True

        pool = make_pool(
            Auth(uid="bad", access_token="at-bad", expires_at=9999999999),
            Auth(uid="good", access_token="at-good", expires_at=9999999999),
        )
        pool.set_credits("bad", 2000)
        pool.set_credits("good", 1000)

        state = make_primed_state(pool, make_upstream(behavior))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        assert resp.status_code == 200, resp.text
        assert calls.get("Cloud-IDE-JWT at-bad") == 1
        assert calls.get("Cloud-IDE-JWT at-good") == 1
        # 坏账号被长冷却
        status, _ = pool.status("bad")
        assert status.cooling is True

    def test_stream_cooldown_on_stream_error(self):
        state = make_state(
            make_pool(make_auth("u1", "at1")),
            make_upstream(lambda r: (200, 'event:error\ndata:{"code":5001,"message":"upstream broke"}\n\n', True)),
        )
        client = make_test_client(state)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "glm-5.2", "stream": True, "messages": []},
        )
        text = resp.text
        assert "solo error" in text
        assert "data: [DONE]" in text
        status, _ = state.pool.status("u1")
        assert status.err_count == 1

    def test_all_unavailable_returns_503(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (429, "rate limited", False)))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        assert resp.status_code == 503
        assert "error" in resp.json()

    def test_session_dead_disables(self):
        state = make_state(
            make_pool(make_auth("u1", "at1")),
            make_upstream(lambda r: (401, '{"code":1001,"msg":"login required"}', False)),
        )
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        assert resp.status_code == 503
        status, _ = state.pool.status("u1")
        assert status.disabled is True

    def test_unknown_model_400(self):
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "does-not-exist-xyz", "messages": []})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_404_soft_cools_without_err_count(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (404, "nope", False)))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        assert resp.status_code == 503
        status, _ = state.pool.status("u1")
        assert status.cooling is True
        assert status.err_count == 0

    def test_err_threshold_cools_after_three(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (400, "bad", False)))
        client = make_test_client(state)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        status, _ = state.pool.status("u1")
        assert status.cooling is True

    def test_request_body_too_large(self):
        from traeapi.server.state import MAX_BODY_BYTES

        state = make_primed_state(make_pool(make_auth("u1", "at")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.post(
            "/v1/chat/completions",
            content=b'{"model":"glm-5.2","messages":[{"role":"user","content":"' + b"a" * (MAX_BODY_BYTES + 1) + b'"}]}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert "error" in resp.json()

    def test_auto_model_uses_default(self):
        seen: dict = {}

        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            seen["body"] = json.loads(request.content)
            return 200, SOLO_SSE, True

        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(behavior))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "auto", "messages": []})
        assert resp.status_code == 200
        assert seen["body"]["config_name"] == "glm-5.2"

    def test_internal_suffix_model_maps_to_base(self):
        seen: dict = {}

        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            seen["body"] = json.loads(request.content)
            return 200, SOLO_SSE, True

        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(behavior))
        client = make_test_client(state)
        resp = client.post("/v1/chat/completions", json={"model": "glm-5.2__dev", "messages": []})
        assert resp.status_code == 200
        assert seen["body"]["config_name"] == "glm-5.2"

    def test_usage_recorded(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        summary = state.stats.summary()
        assert summary["total_calls"] == 1
        assert summary["ok_calls"] == 1
        records = state.stats.recent(0)
        assert records[0].total_tokens == 7
        assert records[0].uid == "u1"


# ---------------------------------------------------------------------------
# 鉴权与基础端点
# ---------------------------------------------------------------------------


class TestAuthAndBasics:
    """Bearer 鉴权与健康检查。"""

    def test_api_key_auth(self):
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_upstream(lambda r: (200, SOLO_SSE, True)), api_key="test-key")
        client = make_test_client(state)

        assert client.post("/v1/chat/completions", json={}).status_code == 401
        assert client.post(
            "/v1/chat/completions", json={}, headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert client.get("/v1/models", headers={"Authorization": "Bearer test-key"}).status_code == 200

    def test_api_key_case_insensitive_prefix(self):
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_upstream(lambda r: (200, SOLO_SSE, True)), api_key="test-key")
        client = make_test_client(state)
        assert client.get("/v1/models", headers={"Authorization": "bearer test-key"}).status_code == 200
        assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_no_key_configured_allows_all(self):
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        assert client.get("/v1/models").status_code == 200

    def test_models_endpoint_static_fallback(self):
        """上游 404 → 回退静态表，只剩 17 个面向用户的官方模型。"""
        state = make_state(
            make_pool(make_auth("u1", "at")),
            make_route_upstream({}),  # 所有端点 404
        )
        client = make_test_client(state)
        resp = client.get("/v1/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        data = body["data"]
        assert len(data) == 17, f"want 17 official models, got {len(data)}"
        ids = {m["id"] for m in data}
        assert "glm-5.2" in ids
        assert "glm-5" in ids  # 默认保留不可见旧模型
        assert not any(i.startswith("custom_model_") for i in ids)
        assert "summary" not in ids
        assert "browser_use_subagent" not in ids

    def test_status_endpoint_no_token_leak(self):
        pool = make_pool(Auth(uid="u1", nickname="nick", access_token="SECRET-TOKEN", expires_at=9999999999))
        pool.set_credits("u1", 42)
        state = make_state(pool, make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/status")
        assert resp.status_code == 200
        text = resp.text
        assert '"uid":"u1"' in text
        assert '"credits":42' in text
        assert "SECRET-TOKEN" not in text

    def test_healthz(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_root_redirects_to_admin(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/admin"


# ---------------------------------------------------------------------------
# 管理面板只读接口
# ---------------------------------------------------------------------------


class TestAdminReadOnly:
    """面板只读数据源。"""

    def test_admin_page(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "traeapi 控制台" in resp.text
        assert "账号管理" in resp.text

    def test_admin_accounts(self):
        pool = make_pool(Auth(uid="u1", nickname="Nick", access_token="SECRET", expires_at=9999999999))
        state = make_state(pool, make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/admin/api/accounts")
        assert resp.status_code == 200
        accounts = resp.json()["accounts"]
        assert len(accounts) == 1
        assert accounts[0]["uid"] == "u1"
        assert accounts[0]["has_auth"] is True
        assert "SECRET" not in resp.text

    def test_admin_account_json_masks_tokens(self):
        auth = Auth(
            uid="u1",
            access_token="A" * 40,
            refresh_token="B" * 40,
            machine_id="m" * 32,
            device_id="d" * 32,
            expires_at=9999999999,
        )
        state = make_primed_state(make_pool(auth), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/admin/api/accounts/u1/json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"].startswith("A" * 12)
        assert body["access_token"].endswith("chars)")
        assert "A" * 40 not in resp.text
        assert body["machine_id"] == "m" * 8 + "…"

    def test_admin_account_json_missing(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        assert client.get("/admin/api/accounts/nope/json").status_code == 404


class TestAdminModels:
    """面板模型列表与倍率。"""

    DETAIL = json.dumps(
        {
            "config_info_list": [
                {
                    "config_name": "glm-5.2",
                    "context_window_tokens": {"dev": 256000},
                    "display_config": {"display_name": "GLM-5.2", "fee_model_level": 2},
                    "display_contact_config": json.dumps(
                        {
                            "consumption_rate": {"enable": True, "data": {"rate": 0.78}},
                            "discount": {
                                "enable": True,
                                "data": {
                                    "original_consumption_rate": 0.78,
                                    "consumption_rate": 0.39,
                                    "member_discount": 50,
                                    "is_discount_matched": True,
                                },
                            },
                        }
                    ),
                },
                {
                    "config_name": "no-rate",
                    "context_window_tokens": {"dev": 128000},
                    "display_config": {"display_name": "NoRate"},
                    "display_contact_config": json.dumps(
                        {"consumption_rate": {"enable": False, "data": {"rate": 0.5}}}
                    ),
                },
                {
                    "config_name": "my-custom-model",
                    "context_window_tokens": {"dev": 64000},
                    "display_config": {"display_name": "MyCustom", "is_custom_model": True},
                },
                {
                    "config_name": "browser_use_subagent",
                    "context_window_tokens": {"dev": 131000},
                    "display_config": {"display_name": ""},
                },
                {
                    "config_name": "sagitta",
                    "context_window_tokens": {"dev": 200000},
                    "display_config": {"display_name": "-"},
                    "is_invisible_to_user": True,
                },
            ]
        }
    )

    def _state(self, **kwargs):
        upstream = make_route_upstream({C.EpModels: self.DETAIL})
        return make_state(make_pool(make_auth("u1", "at")), upstream, **kwargs)

    def test_admin_models_upstream_rate(self):
        state = self._state()
        client = make_test_client(state)
        resp = client.get("/admin/api/models")
        assert resp.status_code == 200
        body = resp.json()
        # 自定义模型 1 个、内部/不可见 2 个被隐藏
        assert body["hidden_custom"] == 1
        assert body["hidden_internal"] == 2
        assert len(body["data"]) == 2

        first = body["data"][0]
        assert first["id"] == "glm-5.2"
        assert first["name"] == "GLM-5.2"
        assert first["fee_level"] == 2
        assert first["rate"] == 0.39
        assert first["rate_source"] == "upstream"
        assert first["original_rate"] == 0.78
        assert first["discount_percent"] == 50
        assert first["discount_matched"] is True
        assert first["context_length"] == 256000
        assert first["context_from_upstream"] is True

        second = body["data"][1]
        assert second["rate"] == 1
        assert second["rate_source"] == "default"
        assert second["context_length"] == 128000

    def test_admin_models_hide_invisible_models(self):
        state = self._state(hide_invisible_models=True)
        state.catalog.hide_invisible_models = True
        client = make_test_client(state)
        resp = client.get("/admin/api/models")
        assert resp.status_code == 200
        body = resp.json()
        # sagitta 也被隐藏 → 只剩 glm-5.2 与 no-rate
        assert len(body["data"]) == 2
        assert body["hidden_internal"] == 2

    def test_admin_models_config_rates_fallback(self):
        upstream = make_route_upstream({})  # 全部 404 → 静态表
        state = make_state(
            make_pool(make_auth("u1", "at")),
            upstream,
            model_rates={"Doubao-Seed-2.1-Pro": 2.5},
        )
        client = make_test_client(state)
        resp = client.get("/admin/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 17
        by_id = {m["id"]: m for m in body["data"]}
        assert by_id["Doubao-Seed-2.1-Pro"]["rate"] == 2.5
        assert by_id["Doubao-Seed-2.1-Pro"]["rate_source"] == "config"
        assert by_id["glm-5.2"]["rate_source"] == "default"
        assert by_id["glm-5.2"]["rate"] == 1

    def test_admin_refresh_models(self):
        state = self._state()
        client = make_test_client(state)
        resp = client.post("/admin/api/models/refresh", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["upstream_total"] == 5

    def test_admin_refresh_models_no_account(self):
        state = make_state(make_pool(), make_route_upstream({C.EpModels: self.DETAIL}))
        client = make_test_client(state)
        resp = client.post("/admin/api/models/refresh", json={})
        assert resp.status_code == 502

    def test_admin_refresh_models_requires_key(self):
        state = self._state(api_key="test-key")
        client = make_test_client(state)
        assert client.post("/admin/api/models/refresh", json={}).status_code == 401
        assert client.post(
            "/admin/api/models/refresh", json={}, headers={"Authorization": "Bearer test-key"}
        ).status_code == 200


class TestAdminFunction:
    """对话通道读取与切换。"""

    def test_admin_function_read(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.get("/admin/api/function")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current"] == "solo_work_lite"
        assert body["default"] == "solo_work_lite"
        assert len(body["options"]) == 4

    def test_admin_set_function(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.post("/admin/api/function", json={"function": "solo_design_remote"})
        assert resp.status_code == 200
        assert resp.json()["current"] == "solo_design_remote"
        assert client.get("/admin/api/function").json()["current"] == "solo_design_remote"

    def test_admin_set_function_unknown(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        resp = client.post("/admin/api/function", json={"function": "bogus"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unknown_function"

    def test_admin_set_function_empty_restores_default(self):
        state = make_state(make_pool(), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        client.post("/admin/api/function", json={"function": "solo_work_remote"})
        resp = client.post("/admin/api/function", json={"function": ""})
        assert resp.status_code == 200
        assert resp.json()["current"] == "solo_work_lite"


class TestAdminUsage:
    """调用记录接口。"""

    def test_admin_usage_records_non_stream(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        resp = client.get("/admin/api/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["total_calls"] == 1
        assert body["summary"]["ok_calls"] == 1
        assert body["records"][0]["total_tokens"] == 7
        assert body["records"][0]["ok"] is True

    def test_admin_usage_records_stream_tokens(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        client.post("/v1/chat/completions", json={"model": "glm-5.2", "stream": True, "messages": []})
        body = client.get("/admin/api/usage").json()
        assert body["summary"]["total_calls"] == 1
        assert body["records"][0]["stream"] is True
        assert body["records"][0]["total_tokens"] == 7

    def test_admin_usage_records_failure(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (500, "boom", False)))
        client = make_test_client(state)
        client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        body = client.get("/admin/api/usage").json()
        assert body["summary"]["total_calls"] == 1
        assert body["summary"]["ok_calls"] == 0
        assert body["records"][0]["ok"] is False
        assert body["records"][0]["err_code"] == "no_healthy_account"

    def test_admin_usage_limit(self):
        state = make_primed_state(make_pool(make_auth("u1", "at1")), make_upstream(lambda r: (200, SOLO_SSE, True)))
        client = make_test_client(state)
        for _ in range(3):
            client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": []})
        assert len(client.get("/admin/api/usage?limit=2").json()["records"]) == 2
        assert len(client.get("/admin/api/usage").json()["records"]) == 3


class TestAdminCreditsPools:
    """额度监控接口。"""

    def test_admin_credits(self):
        routes = {
            C.EpEntUsage: json.dumps(
                {
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {"quota": {"credits_limit": 1000}},
                            "usage": {"credits_amount": 250},
                        }
                    ]
                }
            ),
            C.EpCheckinStatus: '{"checked_in":false,"credits":150,"enable":true}',
        }
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_route_upstream(routes))
        client = make_test_client(state)
        resp = client.get("/admin/api/credits")
        assert resp.status_code == 200
        account = resp.json()["accounts"][0]
        assert account["remain"] == 750
        assert account["limit"] == 1000
        assert account["used"] == 250
        assert account["packs"] == 1
        assert account["checked_in"] is False
        assert account["checkin_credits"] == 150

    def test_admin_credits_reports_error(self):
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_route_upstream({}))
        client = make_test_client(state)
        account = client.get("/admin/api/credits").json()["accounts"][0]
        assert "error" in account

    def test_admin_credits_reports_enabled_flag(self):
        """credits 接口必须返回 enabled，否则前端无法显示「已停用」。

        早先漏了该字段，「额度监控」卡片只显示冷却中/已禁用，
        被手动停用的账号看起来像在冷却，用户干等也不会恢复。
        """
        pool = make_pool(make_auth("u1"))
        pool.set_enabled("u1", False, "user disabled")
        state = make_state(pool, make_route_upstream({}))
        client = make_test_client(state)

        account = client.get("/admin/api/credits").json()["accounts"][0]
        assert account["enabled"] is False

        pool.set_enabled("u1", True, "")
        account = client.get("/admin/api/credits").json()["accounts"][0]
        assert account["enabled"] is True

    def test_admin_pools(self):
        routes = {
            C.EpEntUsage: json.dumps(
                {
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {
                                "entitlement_id": "checkin_daily",
                                "quota": {"credits_limit": 100},
                            },
                            "usage": {"credits_amount": 10},
                            "group_name": "每日签到",
                        },
                        {
                            "entitlement_base_info": {
                                "entitlement_id": "358204062466",
                                "quota": {"credits_limit": 5000},
                            },
                            "usage": {"credits_amount": 0},
                            "group_name": "Work",
                        },
                    ]
                }
            )
        }
        state = make_primed_state(make_pool(make_auth("u1", "at")), make_route_upstream(routes))
        client = make_test_client(state)
        resp = client.get("/admin/api/pools")
        assert resp.status_code == 200
        body = resp.json()
        assert body["note"]
        pools = body["accounts"][0]["pools"]
        assert len(pools) == 2
        assert pools[0]["id"] == "checkin_daily"
        assert pools[0]["numeric_id"] is False
        assert pools[1]["numeric_id"] is True
