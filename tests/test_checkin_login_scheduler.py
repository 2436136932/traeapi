"""签到、Web 登录回调、调度器、配置的测试。

对应 Go 的 checkin_test.go / callback_test.go / scheduler_test.go / config_test.go /
isalready_test.go。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from traeapi.auth import Auth
from traeapi.config import (
    Config,
    ConfigError,
    default_config,
    load,
    parse_duration,
    parse_listen,
)
from traeapi.pool import CoolKind
from traeapi.scheduler import Scheduler, SchedulerConfig, next_fire
from traeapi.server.callback import (
    build_login_url,
    expire_at_from_exchange,
    fix_mojibake,
    machine_trace_id,
    normalize_expire,
    parse_callback,
)
from traeapi.upstream import constants as C
from traeapi.upstream.client import Client
from tests.conftest import (
    make_auth,
    make_pool,
    make_route_upstream,
    make_state,
    make_test_client,
)


# ---------------------------------------------------------------------------
# 一键签到
# ---------------------------------------------------------------------------


def _checkin_routes(**overrides) -> dict[str, str]:
    routes = {
        C.EpCheckinStatus: '{"checked_in":false,"credits":150,"enable":true}',
        C.EpCheckinClaim: '{"message":"ok"}',
        C.EpEntUsage: json.dumps(
            {
                "user_entitlement_pack_list": [
                    {
                        "entitlement_base_info": {"quota": {"credits_limit": 500}},
                        "usage": {"credits_amount": 0},
                    }
                ]
            }
        ),
    }
    routes.update(overrides)
    return routes


def post_checkin(client, key: str = ""):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/admin/api/checkin", json={}, headers=headers)


class TestAdminCheckin:
    """面板一键签到。"""

    def test_claims_and_updates_credits(self):
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(_checkin_routes()), api_key="test-key")
        client = make_test_client(state)
        resp = post_checkin(client, "test-key")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        assert body["claimed"] == 1
        assert body["failed"] == 0
        assert body["accounts"][0]["action"] == "claimed"
        assert body["accounts"][0]["checkin_credits"] == 150
        assert body["accounts"][0]["remain"] == 500
        # 签到后积分应回写池状态
        status, _ = state.pool.status("u1")
        assert status.credits == 500

    def test_already_checked_in(self):
        routes = _checkin_routes(
            **{C.EpCheckinStatus: '{"checked_in":true,"credits":150,"enable":true}',
               C.EpEntUsage: '{"user_entitlement_pack_list":[]}'}
        )
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(routes), api_key="test-key")
        client = make_test_client(state)
        resp = post_checkin(client, "test-key")
        assert resp.status_code == 200
        body = resp.json()
        assert body["already"] == 1
        assert body["claimed"] == 0
        # 不得泄漏 token
        assert "rt-u1" not in resp.text
        assert "at-u1" not in resp.text

    def test_requires_key(self):
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(_checkin_routes()), api_key="test-key")
        client = make_test_client(state)
        assert post_checkin(client, "").status_code == 401
        assert post_checkin(client, "wrong").status_code == 401
        assert post_checkin(client, "test-key").status_code == 200

    def test_skips_disabled_account(self):
        pool = make_pool(make_auth("u1"))
        pool.disable("u1", "session dead")
        state = make_state(pool, make_route_upstream(_checkin_routes()), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        assert body["accounts"][0]["action"] == "disabled"
        assert body["skipped"] == 1

    def test_skips_account_without_refresh_token(self):
        pool = make_pool(Auth(uid="u1", access_token="at", refresh_token="", expires_at=9999999999))
        state = make_state(pool, make_route_upstream(_checkin_routes()), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        assert body["accounts"][0]["action"] == "no_auth"

    def test_no_checkin_when_disabled(self):
        routes = _checkin_routes(
            **{C.EpCheckinStatus: '{"checked_in":false,"credits":0,"enable":false}',
               C.EpEntUsage: '{"user_entitlement_pack_list":[]}'}
        )
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(routes), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        assert body["accounts"][0]["action"] == "no_checkin"
        assert body["skipped"] == 1

    def test_retries_on_busy_code_then_fails(self):
        """所有设备号候选都被 9074 拒绝 → failed 且带 code 与上游中文提示。"""
        routes = _checkin_routes(
            **{
                C.EpCheckinClaim: '{"code":9074,"message":"当前参与用户太多，请稍后再试"}',
                C.EpEntUsage: '{"user_entitlement_pack_list":[]}',
            }
        )
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(routes), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        account = body["accounts"][0]
        assert account["action"] == "failed"
        assert account["code"] == 9074
        assert "当前参与用户太多" in account["error"]
        assert body["failed"] == 1

    def test_claim_failure_reported(self):
        routes = _checkin_routes(
            **{
                C.EpCheckinClaim: '{"code":9004,"message":"order parameters are incorrect"}',
                C.EpEntUsage: '{"user_entitlement_pack_list":[]}',
            }
        )
        state = make_state(make_pool(make_auth("u1")), make_route_upstream(routes), api_key="test-key")
        client = make_test_client(state)
        account = post_checkin(client, "test-key").json()["accounts"][0]
        assert account["action"] == "failed"
        assert account["code"] == 9004

    def test_empty_pool(self):
        state = make_state(make_pool(), make_route_upstream(_checkin_routes()), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        assert body["total"] == 0
        assert body["accounts"] == []

    def test_refreshes_expiring_token_first(self):
        """token 临近过期 → 先 ExchangeToken 再签到。"""
        routes = _checkin_routes()
        routes[C.EpExchange] = json.dumps({"Result": {"Token": "new-at", "RefreshToken": "new-rt"}})
        pool = make_pool(Auth(uid="u1", access_token="old", refresh_token="rt-u1", expires_at=1))
        state = make_state(pool, make_route_upstream(routes), api_key="test-key")
        client = make_test_client(state)
        body = post_checkin(client, "test-key").json()
        assert body["accounts"][0]["action"] == "claimed"
        assert state.pool.auth_by_uid("u1").access_token == "new-at"


# ---------------------------------------------------------------------------
# 登录回调解析
# ---------------------------------------------------------------------------


def _callback_url(refresh_token: str = "", user_info: str = "", user_jwt: str = "") -> str:
    values: dict[str, str] = {}
    if refresh_token:
        values["refreshToken"] = refresh_token
    if user_info:
        values["userInfo"] = user_info
    if user_jwt:
        values["userJwt"] = user_jwt
    return "http://127.0.0.1:18080/authorize?" + urlencode(values)


class TestParseCallback:
    """回调链接解析。"""

    def test_with_refresh_token(self):
        user_info = '{"UserID":"u123","ScreenName":"Alice","TenantID":"ent-1"}'
        user_jwt = '{"Token":"at-xyz","RefreshToken":"rt-fallback","TokenExpireAt":1786847930141}'
        info = parse_callback(_callback_url("rt-main", user_info, user_jwt))
        assert info.refresh_token == "rt-main"
        assert info.access_token == ""
        assert info.uid == "u123"
        assert info.nickname == "Alice"
        assert info.enterprise_id == "ent-1"

    def test_fallback_to_user_jwt_refresh(self):
        info = parse_callback(
            _callback_url("", '{"UserID":"u9","ScreenName":"Bob"}',
                          '{"Token":"at-from-jwt","RefreshToken":"rt-from-jwt"}')
        )
        assert info.refresh_token == "rt-from-jwt"
        assert info.access_token == ""
        assert info.expires_at == 0

    def test_no_refresh_but_has_jwt_token(self):
        info = parse_callback(
            _callback_url("", '{"UserID":"u1","ScreenName":"S"}',
                          '{"Token":"at-direct","TokenExpireAt":1786847930141}')
        )
        assert info.access_token == "at-direct"
        assert info.expires_at == 1786847930

    def test_missing_all_tokens(self):
        with pytest.raises(ValueError):
            parse_callback(_callback_url("", '{"UserID":"u1","ScreenName":"N"}', '{"RefreshToken":""}'))

    def test_empty(self):
        with pytest.raises(ValueError):
            parse_callback("")
        with pytest.raises(ValueError):
            parse_callback("   ")

    def test_garbled_user_info(self):
        info = parse_callback(_callback_url("rt", "not-a-json", ""))
        assert info.uid == ""
        assert info.nickname == ""
        assert info.refresh_token == "rt"

    def test_double_encoded_user_info(self):
        """userInfo 双层 percent-encoding 也要能解出（parseJSONParam 的 unquote 容错）。"""
        raw = '{"UserID":"ud","ScreenName":"Dan","TenantID":"t"}'
        doubled = urlencode({"x": raw})[2:]  # 再编码一层
        info = parse_callback("http://127.0.0.1:18080/authorize?refreshToken=rt&userInfo=" + doubled)
        assert info.uid == "ud"
        assert info.nickname == "Dan"

    def test_fix_mojibake_recovers_cjk(self):
        """双重编码的中文昵称应被修复（login.sh 有、Go 版漏掉的增强）。"""
        original = "肉肉"
        garbled = original.encode("utf-8").decode("latin-1")
        assert fix_mojibake(garbled, "3880644536968592") == original

    def test_fix_mojibake_falls_back_for_unrecoverable(self):
        assert fix_mojibake("Óû§8847309959", "3880644536968592") == "用户8592"

    def test_normalize_expire(self):
        assert normalize_expire(1786847930141) == 1786847930
        assert normalize_expire(1786847930) == 1786847930

    def test_expire_at_from_exchange(self):
        now = 1700000000
        assert expire_at_from_exchange(1786847930141, 0, now) == 1786847930
        assert expire_at_from_exchange(1000, 1209600, now) == now + 1209600
        assert expire_at_from_exchange(0, 0, now) == 0
        assert expire_at_from_exchange(now + 3600, 0, now) == now + 3600


class TestBuildLoginURL:
    """登录 URL 构造。"""

    def test_build_login_url(self):
        machine_id = "abcdef0123456789abcdef0123456789"
        device_id = "0123456789abcdef0123456789abcdef"
        callback = "http://127.0.0.1:7864/authorize"
        url = build_login_url(machine_id, device_id, callback)
        assert url.startswith("https://www.trae.cn/authorization?")

        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        checks = {
            "client_id": "en1oxy7wnw8j9n",
            "auth_from": "solo",
            "login_channel": "native_ide",
            "auth_type": "local",
            "machine_id": machine_id,
            "device_id": device_id,
            "x_machine_id": machine_id,
            "x_device_id": device_id,
            "x_device_brand": "PC",
            "x_device_type": "PC",
            "auth_callback_url": callback,
        }
        for key, want in checks.items():
            assert query[key][0] == want, key
        assert len(query["login_trace_id"][0]) == 16

    def test_machine_trace_id(self):
        assert len(machine_trace_id("a" * 32, "b" * 32)) == 16
        assert machine_trace_id("a" * 32, "b" * 32) == "b" * 16
        assert machine_trace_id("ab", "cd") == "0" * 12 + "abcd"


# ---------------------------------------------------------------------------
# Web 登录闭环
# ---------------------------------------------------------------------------


class TestLoginFlow:
    """pending 登录态与 /authorize 回调。"""

    def test_login_start(self):
        state = make_state(make_pool(), make_route_upstream({}))
        client = make_test_client(state)
        resp = client.post("/admin/api/login", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["login_url"].startswith("https://www.trae.cn/authorization?")
        assert body["callback_url"] == "http://127.0.0.1:18080/authorize"
        assert len(body["pending_id"]) == 16

        result = client.get(f"/admin/api/login/result?pending_id={body['pending_id']}")
        assert result.status_code == 200
        assert result.json()["state"] == "pending"

    def test_login_result_unknown_pending(self):
        state = make_state(make_pool(), make_route_upstream({}))
        client = make_test_client(state)
        assert client.get("/admin/api/login/result?pending_id=nope").status_code == 404

    def test_login_cancel(self):
        state = make_state(make_pool(), make_route_upstream({}))
        client = make_test_client(state)
        pending_id = client.post("/admin/api/login", json={}).json()["pending_id"]
        resp = client.post("/admin/api/login/cancel", json={"pending_id": pending_id})
        assert resp.status_code == 200
        assert resp.json()["canceled"] is True
        # 取消后 pending 被删除
        assert client.get(f"/admin/api/login/result?pending_id={pending_id}").status_code == 404

    def test_authorize_callback_imports_account(self, tmp_path: Path):
        """完整回调闭环：捕获 → ExchangeToken → GetUserInfo → 落盘 → pending 成功。"""
        routes = {
            C.EpExchange: json.dumps({"Result": {"Token": "new-at", "RefreshToken": "new-rt", "TokenExpireAt": 1786847930141}}),
            C.EpUserInfo: json.dumps({"Result": {"UserID": "u777", "ScreenName": "Tester", "EnterpriseID": "ent-7"}}),
        }
        state = make_state(make_pool(), make_route_upstream(routes), api_key="test-key")
        state.auth_dir = str(tmp_path)
        client = make_test_client(state)

        pending_id = client.post("/admin/api/login", json={}, headers={"Authorization": "Bearer test-key"}).json()["pending_id"]
        pending = state.logins[pending_id]
        trace = machine_trace_id(pending.machine_id, pending.device_id)

        callback = _callback_url(
            "rt-cb",
            '{"UserID":"u777","ScreenName":"Tester","TenantID":"ent-7"}',
            "",
        )
        resp = client.get(f"/authorize?{callback.split('?', 1)[1]}&loginTraceID={trace}")
        assert resp.status_code == 200
        assert "登录成功" in resp.text

        # 账号已入池
        status, ok = state.pool.status("u777")
        assert ok and status.nickname == "Tester"
        # 凭证已落盘
        saved = tmp_path / "trae-u777.json"
        assert saved.exists()
        doc = json.loads(saved.read_text(encoding="utf-8"))
        assert doc["auth"]["accessToken"] == "new-at"
        assert doc["auth"]["machineId"] == pending.machine_id
        assert doc["auth"]["deviceId"] == pending.device_id

        # pending 标记成功
        result = client.get(f"/admin/api/login/result?pending_id={pending_id}").json()
        assert result["state"] == "success"
        assert result["uid"] == "u777"

    def test_authorize_callback_bad_query(self):
        state = make_state(make_pool(), make_route_upstream({}))
        client = make_test_client(state)
        resp = client.get("/authorize")
        assert resp.status_code == 400
        assert "登录回调解析失败" in resp.text


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class TestScheduler:
    """定时任务的触发时间与签到/刷新行为。"""

    def test_next_fire(self):
        from datetime import datetime

        now = datetime(2026, 10, 9, 10, 30, 0)
        assert next_fire(now, [9]).hour == 9  # 已过 → 明天 9 点
        assert next_fire(now, [9]).day == 10
        assert next_fire(now, [12]).hour == 12  # 未到 → 今天 12 点
        assert next_fire(now, [12]).day == 9

    def test_next_fire_merges_schedules(self):
        from datetime import datetime

        now = datetime(2026, 10, 9, 2, 0, 0)
        # refresh_hours=[3] 与 checkin_hour=9 → 最近的是今天 3 点
        assert next_fire(now, [3, 9]).hour == 3

    def test_run_checkin_reenables_cooling_account(self):
        routes = {
            C.EpCheckinStatus: '{"checked_in":true,"credits":150,"enable":true}',
            C.EpEntUsage: json.dumps(
                {
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {"quota": {"credits_limit": 900}},
                            "usage": {"credits_amount": 100},
                        }
                    ]
                }
            ),
        }
        pool = make_pool(make_auth("u1"))
        pool.cooldown("u1", CoolKind.PLAN, 3600, "plan")
        scheduler = Scheduler(SchedulerConfig(pool=pool, upstream=make_route_upstream(routes)))
        assert scheduler.run_checkin_now() is True
        status, _ = pool.status("u1")
        assert status.cooling is False
        assert status.credits == 800

    def test_run_checkin_skips_disabled(self):
        """被禁用的账号不应发出任何上游请求。"""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200, headers={"Content-Type": "application/json"}, content=b"{}"
            )

        upstream = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        pool = make_pool(make_auth("u1"))
        pool.disable("u1", "session dead")
        scheduler = Scheduler(SchedulerConfig(pool=pool, upstream=upstream))
        assert scheduler.run_checkin_now() is True
        assert calls == []

    def test_run_checkin_reports_failure_on_busy(self):
        routes = {
            C.EpCheckinStatus: '{"checked_in":false,"credits":0,"enable":true}',
            C.EpCheckinClaim: '{"code":9074,"message":"busy"}',
            C.EpEntUsage: '{"user_entitlement_pack_list":[]}',
        }
        pool = make_pool(make_auth("u1"))
        scheduler = Scheduler(SchedulerConfig(pool=pool, upstream=make_route_upstream(routes)))
        assert scheduler.run_checkin_now() is False

    def test_run_refresh_refreshes_tokens(self):
        routes = {C.EpExchange: json.dumps({"Result": {"Token": "new-at", "RefreshToken": "new-rt"}})}
        pool = make_pool(Auth(uid="u1", access_token="old", refresh_token="rt", expires_at=1))
        scheduler = Scheduler(
            SchedulerConfig(pool=pool, upstream=make_route_upstream(routes), refresh_skew=3600)
        )
        scheduler.run_refresh_now()
        assert pool.auth_by_uid("u1").access_token == "new-at"

    def test_run_refresh_skips_fresh_token(self):
        routes = {C.EpExchange: json.dumps({"Result": {"Token": "new-at"}})}
        pool = make_pool(Auth(uid="u1", access_token="old", refresh_token="rt", expires_at=9999999999))
        scheduler = Scheduler(
            SchedulerConfig(pool=pool, upstream=make_route_upstream(routes), refresh_skew=3600)
        )
        scheduler.run_refresh_now()
        assert pool.auth_by_uid("u1").access_token == "old"

    def test_run_refresh_session_dead_disables(self):
        pool = make_pool(Auth(uid="u1", access_token="old", refresh_token="rt", expires_at=1))
        scheduler = Scheduler(
            SchedulerConfig(
                pool=pool,
                upstream=make_route_upstream({}),  # 404
                refresh_skew=3600,
            )
        )
        scheduler.run_refresh_now()
        status, _ = pool.status("u1")
        # 404 不是 session dead，账号只应保留原状（不禁用）
        assert status.disabled is False


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class TestConfig:
    """配置加载与 TW2A_* 覆盖。"""

    def test_default(self):
        cfg = default_config()
        assert cfg.listen == ":7864"
        assert cfg.callback_port == "18080"
        assert cfg.auth_dir == "./auths"
        assert cfg.state_file == "./data/state.json"
        assert cfg.default_model == "glm-5.2"
        assert cfg.cooldown.plan_credit == "12h"
        assert cfg.cooldown.soft_rate == "60s"
        assert cfg.cooldown.err_threshold == 3
        assert cfg.cooldown.err_cooldown == "10m"
        assert cfg.schedule.checkin_hour == 9
        assert cfg.schedule.refresh_hours == [3]
        assert cfg.schedule.checkin_retry_minutes == 30
        assert cfg.upstream.timeout_seconds == 120

    def test_load_file(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "listen": ":9999",
                    "auth_dir": "./my-auths",
                    "default_model": "kimi-k3",
                    "model_rates": {"glm-5.2": 1.5},
                    "hide_invisible_models": True,
                    "solo_function": "solo_work_remote",
                    "cooldown": {"plan_credit": "1h", "err_threshold": 5},
                    "schedule": {"checkin_hour": 7, "refresh_hours": [1, 4]},
                    "upstream": {"timeout_seconds": 30},
                }
            ),
            encoding="utf-8",
        )
        cfg = load(str(path))
        assert cfg.listen == ":9999"
        assert cfg.auth_dir == "./my-auths"
        assert cfg.default_model == "kimi-k3"
        assert cfg.model_rates == {"glm-5.2": 1.5}
        assert cfg.hide_invisible_models is True
        assert cfg.solo_function == "solo_work_remote"
        assert cfg.plan_credit_dur == 3600.0
        assert cfg.err_threshold == 5
        assert cfg.schedule.checkin_hour == 7
        assert cfg.schedule.refresh_hours == [1, 4]
        assert cfg.upstream.timeout_seconds == 30

    def test_load_missing_file_falls_back_to_defaults(self, tmp_path: Path):
        cfg = load(str(tmp_path / "nope.json"))
        assert cfg.listen == ":7864"
        assert cfg.default_model == "glm-5.2"

    def test_api_key_only_from_env(self, tmp_path: Path, monkeypatch):
        """config.json 里的 api_key 必须被忽略（对齐 Go 的 json:"-"）。"""
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"api_key": "from-json", "listen": ":1"}), encoding="utf-8")
        cfg = load(str(path))
        assert cfg.api_key == ""

        monkeypatch.setenv("TW2A_API_KEY", "from-env")
        cfg = load(str(path))
        assert cfg.api_key == "from-env"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TW2A_LISTEN", ":7777")
        monkeypatch.setenv("TW2A_AUTH_DIR", "/tmp/auths")
        monkeypatch.setenv("TW2A_DEFAULT_MODEL", "kimi-k3")
        monkeypatch.setenv("TW2A_PLAN_CREDIT", "2h")
        monkeypatch.setenv("TW2A_SOFT_RATE", "30s")
        monkeypatch.setenv("TW2A_ERR_THRESHOLD", "7")
        monkeypatch.setenv("TW2A_ERR_COOLDOWN", "20m")
        monkeypatch.setenv("TW2A_CHECKIN_HOUR", "6")
        monkeypatch.setenv("TW2A_CHECKIN_RETRY_MINUTES", "15")
        monkeypatch.setenv("TW2A_HIDE_INVISIBLE_MODELS", "true")
        monkeypatch.setenv("TW2A_FUNCTION", "solo_design_lite")
        monkeypatch.setenv("TW2A_TIMEOUT_SECONDS", "45")
        monkeypatch.setenv("TW2A_CALLBACK_PORT", "0")

        cfg = load(None)
        assert cfg.listen == ":7777"
        assert cfg.auth_dir == "/tmp/auths"
        assert cfg.default_model == "kimi-k3"
        assert cfg.plan_credit_dur == 7200.0
        assert cfg.soft_rate_dur == 30.0
        assert cfg.err_threshold == 7
        assert cfg.err_cooldown_dur == 1200.0
        assert cfg.schedule.checkin_hour == 6
        assert cfg.schedule.checkin_retry_minutes == 15
        assert cfg.hide_invisible_models is True
        assert cfg.solo_function == "solo_design_lite"
        assert cfg.upstream.timeout_seconds == 45
        assert cfg.callback_host_port() is None  # 显式 "0" → 不起回调 server

    def test_bad_duration(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"cooldown": {"plan_credit": "12 hours"}}), encoding="utf-8")
        with pytest.raises(ConfigError):
            load(str(path))

    def test_bad_json(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(ConfigError):
            load(str(path))

    def test_listen_normalization(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"listen": "8080"}), encoding="utf-8")
        cfg = load(str(path))
        assert cfg.listen == ":8080"
        assert cfg.host_port() == ("0.0.0.0", 8080)

    def test_callback_host_port(self):
        cfg = Config(callback_port="18080")
        assert cfg.callback_host_port() == ("127.0.0.1", 18080)
        cfg = Config(callback_port="0")
        assert cfg.callback_host_port() is None


class TestDurationParsing:
    """Go duration 解析。"""

    @pytest.mark.parametrize(
        "text,seconds",
        [
            ("12h", 43200.0),
            ("60s", 60.0),
            ("10m", 600.0),
            ("1h30m", 5400.0),
            ("1.5h", 5400.0),
            ("500ms", 0.5),
            ("0", 0.0),
            ("-5m", -300.0),
            ("1h0m0s", 3600.0),
        ],
    )
    def test_parse(self, text, seconds):
        assert parse_duration(text) == pytest.approx(seconds)

    @pytest.mark.parametrize("text", ["", "abc", "12", "12x", "h"])
    def test_invalid(self, text):
        with pytest.raises(ConfigError):
            parse_duration(text)

    @pytest.mark.parametrize(
        "text,expected",
        [
            (":7864", ("0.0.0.0", 7864)),
            ("0.0.0.0:8080", ("0.0.0.0", 8080)),
            ("127.0.0.1:9000", ("127.0.0.1", 9000)),
            ("7864", ("0.0.0.0", 7864)),
        ],
    )
    def test_parse_listen(self, text, expected):
        assert parse_listen(text) == expected


class TestSigninIsAlready:
    """cmd/signin 的「已签到」判定。"""

    def test_is_already(self):
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "signin_mod", Path(__file__).resolve().parent.parent / "cmd" / "signin.py"
        )
        module = importlib.util.module_from_spec(spec)
        # dataclass 解析注解时需要模块已在 sys.modules 里注册
        sys.modules["signin_mod"] = module
        try:
            assert spec.loader is not None
            spec.loader.exec_module(module)

            assert module.is_already("今日已签到") is True
            assert module.is_already("already checked in") is True
            assert module.is_already("ALREADY CHECK") is True
            # 无歧义标记之外的不算已签（避免 429/5xx body 含 checkin 被误判）
            assert module.is_already("429 too many requests checkin") is False
            assert module.is_already("") is False
        finally:
            sys.modules.pop("signin_mod", None)
