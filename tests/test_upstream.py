"""upstream 层测试（对应 Go 的 client_test.go / sse_test.go / function_test.go / pools_test.go）。"""

from __future__ import annotations

import json

import httpx
import pytest

from traeapi.auth import Auth
from traeapi.upstream import constants as C
from traeapi.upstream.client import (
    CheckinError,
    Client,
    ErrKind,
    UpstreamError,
    classify,
    normalize_expires_at,
    parse_rate_info,
    pick_context_window,
)
from traeapi.upstream.headers import (
    checkin_device_plan,
    derived_ug_device_id,
    num16,
    oauth_headers,
    solo_headers,
    ug_device_id,
    ug_headers,
)
from traeapi.upstream.payload import prepare_body
from traeapi.upstream import sse
from tests.conftest import SOLO_SSE_FIXTURE, make_upstream


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    """错误分类。"""

    def test_plan_limit(self):
        assert classify(400, '{"code":1005,"msg":"plan limit"}') == ErrKind.PLAN_LIMIT

    def test_plan_limit_with_plan_word(self):
        assert classify(403, "1005 plan insufficient") == ErrKind.PLAN_LIMIT

    def test_session_dead(self):
        assert classify(401, '{"msg":"login required"}') == ErrKind.SESSION_DEAD
        assert classify(401, "whatever") == ErrKind.SESSION_DEAD

    def test_soft_rate(self):
        assert classify(429, "rate limited") == ErrKind.SOFT_RATE

    def test_not_found(self):
        assert classify(404, "nope") == ErrKind.NOT_FOUND

    def test_server(self):
        assert classify(500, "boom") == ErrKind.SERVER
        assert classify(503, "unavailable") == ErrKind.SERVER

    def test_client(self):
        assert classify(400, "bad param") == ErrKind.CLIENT

    def test_none(self):
        assert classify(200, "ok") == ErrKind.NONE


class TestExpireNormalization:
    """TokenExpireAt 毫秒/秒归一化。"""

    def test_milliseconds(self):
        assert normalize_expires_at(1786847930141) == 1786847930

    def test_seconds(self):
        assert normalize_expires_at(1786847930) == 1786847930


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


class TestHeaders:
    """三类请求头与设备号策略。"""

    def test_solo_headers(self):
        auth = Auth(uid="u1", access_token="at", machine_id="m" * 32, device_id="d" * 32)
        headers = solo_headers(auth, True)
        assert headers["Authorization"] == "Cloud-IDE-JWT at"
        assert headers["X-Cloudide-Token"] == "at"
        assert headers["X-Ide-Token"] == "at"
        assert headers["Accept"] == "text/event-stream"
        assert headers["X-Uid"] == "u1"
        assert headers["X-Machine-Id"] == "m" * 32
        assert headers["X-Device-Id"] == "d" * 32
        assert headers["User-Agent"] == f"Trae/{C.IdeVersion}"
        assert headers["X-App-Id"] == C.AppID

    def test_solo_headers_non_stream(self):
        auth = Auth(uid="u1", access_token="at")
        assert solo_headers(auth, False)["Accept"] == "application/json"

    def test_oauth_headers(self):
        headers = oauth_headers()
        assert headers["User-Agent"] == f"Trae/{C.IdeVersion}"
        assert "Authorization" not in headers

    def test_ug_headers_sends_device_id(self):
        auth = Auth(uid="2666736248956905", access_token="at")
        headers = ug_headers(auth)
        assert headers["Authorization"] == "Cloud-IDE-JWT at"
        assert headers["X-User-Region"] == "CN"
        assert headers["X-Device-Id"] == "2666736248956905"

    def test_num16(self):
        assert num16("1234567890123456") is True
        assert num16("123456789012345") is False
        assert num16("123456789012345a") is False
        assert num16("") is False

    def test_ug_device_id_prefers_manual_16_digit_device_id(self):
        auth = Auth(uid="2666736248956905", device_id="1234567890123456")
        assert ug_device_id(auth) == "1234567890123456"

    def test_ug_device_id_falls_back_to_uid(self):
        auth = Auth(uid="2666736248956905", device_id="d" * 32)
        assert ug_device_id(auth) == "2666736248956905"

    def test_ug_device_id_derived_when_uid_not_16(self):
        auth = Auth(uid="not-16-digits", device_id="d" * 32)
        derived = ug_device_id(auth)
        assert num16(derived) is True

    def test_checkin_device_plan_order(self):
        auth = Auth(uid="2666736248956905", device_id="07583986225ddd987138de476e6ae588")
        plan = checkin_device_plan(auth)
        assert plan[0] == "2666736248956905"  # 首选 uid
        assert plan[1] == "07583986225ddd987138de476e6ae588"  # 账号 deviceId
        assert len(plan) == 3
        assert num16(plan[2]) is True

    def test_checkin_device_plan_dedup(self):
        auth = Auth(uid="1234567890123456", device_id="1234567890123456")
        plan = checkin_device_plan(auth)
        assert len(plan) == len(set(plan))

    def test_derived_device_id_stable_and_unique(self):
        a1 = Auth(uid="u1", access_token="t")
        a2 = Auth(uid="u2", access_token="t")
        assert derived_ug_device_id(a1) == derived_ug_device_id(a1)
        assert derived_ug_device_id(a1) != derived_ug_device_id(a2)
        assert num16(derived_ug_device_id(a1)) is True


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


class TestPrepareBody:
    """OpenAI → SOLO 请求体改写。"""

    def test_forces_stream_and_function(self):
        out = json.loads(prepare_body('{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}'))
        assert out["stream"] is True
        assert out["function"] == "solo_work_lite"
        assert out["config_name"] == "glm-5.2"
        assert out["model"] == "glm-5.2"
        assert out["messages"][0]["content"] == [{"type": "text", "text": "hi"}]

    def test_keeps_array_content(self):
        out = json.loads(
            prepare_body('{"model":"glm-5.2","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}')
        )
        assert len(out["messages"][0]["content"]) == 1

    def test_uses_active_function(self):
        C.set_function(C.FunctionWorkRemote)
        out = json.loads(prepare_body('{"model":"glm-5.2","messages":[]}'))
        assert out["function"] == "solo_work_remote"

    def test_tool_choice_function_object(self):
        src = (
            '{"model":"glm-5.2","tool_choice":{"type":"function","function":{"name":"get_weather"}},'
            '"tools":[{"type":"function","function":{"name":"get_weather"}}]}'
        )
        out = json.loads(prepare_body(src))
        assert out["tool_choice"] == "get_weather"
        assert "tools" in out

    def test_tool_choice_none(self):
        out = json.loads(
            prepare_body('{"model":"glm-5.2","tool_choice":"none","tools":[{}],"functions":[{}]}')
        )
        assert "tool_choice" not in out
        assert "tools" not in out
        assert "functions" not in out

    def test_tool_choice_auto(self):
        out = json.loads(prepare_body('{"model":"glm-5.2","tool_choice":{"type":"auto"}}'))
        assert out["tool_choice"] == "auto"

    def test_invalid_json_passthrough(self):
        assert prepare_body(b"{broken") == b"{broken"

    def test_tools_parameters_stringified(self):
        src = (
            '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],'
            '"tools":[{"type":"function","function":{"name":"get_weather",'
            '"parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
        )
        out = json.loads(prepare_body(src))
        params = out["tools"][0]["function"]["parameters"]
        assert isinstance(params, str)
        assert '"city"' in params

    def test_tools_invalid_entries_dropped(self):
        src = (
            '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],'
            '"tools":[{"type":"function","function":{"name":"ok","parameters":{"type":"object"}}},{"bad":1}]}'
        )
        out = json.loads(prepare_body(src))
        assert len(out["tools"]) == 1

    def test_assistant_tool_calls_to_function_call(self):
        src = (
            '{"model":"glm-5.2","messages":['
            '{"role":"user","content":"hi"},'
            '{"role":"assistant","content":null,"tool_calls":[{"id":"call_x","type":"function",'
            '"function":{"name":"skill_view","arguments":"{\\"name\\":\\"hermes-agent\\"}"}}]},'
            '{"role":"tool","tool_call_id":"call_x","content":"skill content"}],'
            '"tools":[{"type":"function","function":{"name":"skill_view"}}]}'
        )
        out = json.loads(prepare_body(src))
        call = out["messages"][1]["tool_calls"][0]
        assert "function" not in call
        assert call["function_call"]["name"] == "skill_view"

    def test_tool_call_without_name_dropped(self):
        src = (
            '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"},'
            '{"role":"assistant","tool_calls":[{"id":"call_bad","type":"function",'
            '"function":{"arguments":"{}"}}]}]}'
        )
        out = json.loads(prepare_body(src))
        assert "tool_calls" not in out["messages"][1]

    def test_default_model_when_missing(self):
        out = json.loads(prepare_body('{"messages":[]}'))
        assert out["config_name"] == "glm-5.2"


class TestFunctionSwitch:
    """SOLO function 白名单与切换。"""

    def test_set_function_switch(self):
        assert C.set_function(C.FunctionDesignRemote) is True
        assert C.active_function() == C.FunctionDesignRemote
        assert C.set_function("") is True
        assert C.active_function() == C.Function

    def test_set_function_rejects_unknown(self):
        assert C.set_function("bogus") is False
        assert C.active_function() == C.Function

    def test_known_functions_covers_traework_values(self):
        known = C.known_functions()
        assert known == [
            "solo_work_lite",
            "solo_work_remote",
            "solo_design_lite",
            "solo_design_remote",
        ]

    def test_function_options(self):
        options = C.function_options()
        assert len(options) == 4
        assert options[0]["value"] == "solo_work_lite"
        assert "轻量" in options[0]["desc"]


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


class TestSSE:
    """SOLO SSE 解析与转换。"""

    def test_parse_solo_line(self):
        ev = sse.parse_solo_line("output", '{"response":"hi","reasoning_content":"think","tool_calls":null}')
        assert ev is not None
        assert ev.response == "hi"
        assert ev.reasoning == "think"
        assert ev.tool_calls is None

        ev = sse.parse_solo_line("done", '{"finish_reason":"stop"}')
        assert ev is not None and ev.finish_reason == "stop"

    def test_parse_solo_line_bad_json(self):
        assert sse.parse_solo_line("output", "{broken") is None

    def test_aggregate(self):
        resp = sse.aggregate([SOLO_SSE_FIXTURE.encode("utf-8")])
        assert resp["object"] == "chat.completion"
        choice = resp["choices"][0]
        assert choice["message"]["content"] == "中国的首都是北京。"
        assert choice["message"]["reasoning_content"] == "让我想想"
        assert choice["finish_reason"] == "stop"
        assert resp["usage"]["total_tokens"] == 163

    def test_aggregate_error(self):
        raw = (
            'event:error\ndata:{"code":4001,"message":"We\'re sorry, the param is invalid.","extra":null}\n\n'
            'event:done\ndata:{"finish_reason":"stop"}\n\n'
        ).encode("utf-8")
        with pytest.raises(sse.SOLOStreamError) as exc:
            sse.aggregate([raw])
        assert exc.value.code == 4001
        assert exc.value.kind() == "client"

    def test_aggregate_error_1005_is_plan_limit(self):
        raw = 'event:error\ndata:{"code":1005,"message":"plan limit"}\n\n'.encode("utf-8")
        with pytest.raises(sse.SOLOStreamError) as exc:
            sse.aggregate([raw])
        assert exc.value.kind() == "plan_limit"

    def test_aggregate_tool_calls(self):
        raw = (
            'event:output\ndata:{"response":"","reasoning_content":"","tool_calls":'
            '[{"id":"call_a","type":"function","function":{"name":"get_weather",'
            '"arguments":"{\\"city\\":\\"北京\\"}"},"index":0}]}\n\n'
            'event:done\ndata:{"finish_reason":"tool_calls"}\n\n'
        ).encode("utf-8")
        resp = sse.aggregate([raw])
        choice = resp["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        calls = choice["message"]["tool_calls"]
        assert calls[0]["id"] == "call_a"
        assert calls[0]["function"]["name"] == "get_weather"
        assert calls[0]["function"]["arguments"] == '{"city":"北京"}'

    def test_merge_tool_call_solo_function_call(self):
        tool_calls: dict = {}
        order: list[int] = []
        sse.merge_tool_call_json(
            tool_calls,
            order,
            [
                {
                    "index": 0,
                    "id": "call_x",
                    "type": "function",
                    "function_call": {"name": "get_weather", "arguments": '{"city":"北京"'},
                }
            ],
        )
        sse.merge_tool_call_json(
            tool_calls,
            order,
            [{"index": 0, "id": "", "type": "function", "function_call": {"name": "", "arguments": "}"}}],
        )
        assert order == [0]
        merged = tool_calls[0]
        assert merged["id"] == "call_x"
        assert merged["function"]["name"] == "get_weather"
        assert merged["function"]["arguments"] == '{"city":"北京"}'

    def test_merge_tool_call_strips_solo_fields(self):
        tool_calls: dict = {}
        order: list[int] = []
        sse.merge_tool_call_json(
            tool_calls,
            order,
            [
                {
                    "index": 0,
                    "id": "call_y",
                    "type": "function",
                    "function_call": {
                        "name": "skill_view",
                        "arguments": '{"name":"x"}',
                        "namespace": "trae",
                        "partial_arguments": None,
                    },
                }
            ],
        )
        fn = tool_calls[0]["function"]
        assert "namespace" not in fn
        assert "partial_arguments" not in fn
        assert fn["name"] == "skill_view"

    def test_stream_converts_to_openai_chunks(self):
        chunks = list(sse.iter_openai_sse([SOLO_SSE_FIXTURE.encode("utf-8")]))
        body = "".join(chunks)
        assert '"object":"chat.completion.chunk"' in body
        assert '"content":"中国"' in body
        assert '"reasoning_content"' in body
        assert "data: [DONE]" in body

    def test_stream_guarantees_done(self):
        raw = 'event:output\ndata:{"response":"x","reasoning_content":"","tool_calls":null}\n\n'
        body = "".join(sse.iter_openai_sse([raw.encode("utf-8")]))
        assert "data: [DONE]" in body

    def test_stream_error_event(self):
        raw = 'event:error\ndata:{"code":5001,"message":"upstream broke"}\n\n'
        seen: list = []
        body = "".join(sse.iter_openai_sse([raw.encode("utf-8")], on_error=seen.append))
        assert "solo error" in body
        assert "data: [DONE]" in body
        assert len(seen) == 1 and seen[0].code == 5001

    def test_stream_usage_callback(self):
        usages: list = []
        list(sse.iter_openai_sse([SOLO_SSE_FIXTURE.encode("utf-8")], on_usage=usages.append))
        assert usages and usages[0]["total_tokens"] == 163

    def test_iter_lines_handles_split_chunks(self):
        """跨字节块切分的行应被正确重组。"""
        raw = SOLO_SSE_FIXTURE.encode("utf-8")
        chunks = [raw[i : i + 7] for i in range(0, len(raw), 7)]
        resp = sse.aggregate(chunks)
        assert resp["choices"][0]["message"]["content"] == "中国的首都是北京。"

    def test_iter_lines_trailing_without_newline(self):
        lines = list(sse.iter_lines([b"a\nb"]))
        assert lines == ["a", "b"]


# ---------------------------------------------------------------------------
# rate / context / pools
# ---------------------------------------------------------------------------


class TestRateParsing:
    """display_contact_config 解析。"""

    def test_parse_rate_with_matched_discount(self):
        raw = json.dumps(
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
        )
        info = parse_rate_info(raw)
        assert info.ok is True
        assert info.rate == 0.39
        assert info.original_rate == 0.78
        assert info.discount_percent == 50
        assert info.discount_matched is True

    def test_parse_rate_unmatched_discount_keeps_original(self):
        raw = json.dumps(
            {
                "consumption_rate": {"enable": True, "data": {"rate": 0.78}},
                "discount": {
                    "enable": True,
                    "data": {
                        "original_consumption_rate": 0.78,
                        "consumption_rate": 0.39,
                        "member_discount": 50,
                        "is_discount_matched": False,
                    },
                },
            }
        )
        info = parse_rate_info(raw)
        assert info.rate == 0.78
        assert info.discount_percent == 50
        assert info.discount_matched is False

    def test_parse_rate_disabled(self):
        info = parse_rate_info(json.dumps({"consumption_rate": {"enable": False, "data": {"rate": 0.5}}}))
        assert info.ok is False

    def test_parse_rate_empty_or_invalid(self):
        assert parse_rate_info("").ok is False
        assert parse_rate_info("{broken").ok is False

    def test_pick_context_window(self):
        assert pick_context_window({"dev": 256000}) == 256000
        assert pick_context_window({"dev": 0, "prod": 128000}) == 128000
        assert pick_context_window({}) == 0
        assert pick_context_window(None) == 0


class TestEntPools:
    """权益包明细解析（通过 fake 上游）。"""

    def test_ent_pools(self):
        payload = json.dumps(
            {
                "user_entitlement_pack_list": [
                    {
                        "entitlement_base_info": {
                            "entitlement_id": "free_utc_2026",
                            "quota": {"credits_limit": 1000},
                        },
                        "usage": {"credits_amount": 250.5},
                        "group_name": "通用",
                        "display_desc": "免费额度",
                    },
                    {
                        "entitlement_base_info": {
                            "entitlement_id": "358204062466",
                            "quota": {"credits_limit": 5000},
                        },
                        "usage": {"credits_amount": 0},
                        "group_name": "Work",
                        "display_desc": "Work 专属",
                    },
                    {
                        "entitlement_base_info": {
                            "entitlement_id": "zero",
                            "quota": {"credits_limit": 0},
                        },
                        "usage": {"credits_amount": 0},
                    },
                ]
            }
        )
        client = make_upstream(lambda req: (200, payload, False))
        pools = client.ent_pools(Auth(uid="u1", access_token="at"))
        assert len(pools) == 2  # 额度为 0 的包被跳过
        assert pools[0].id == "free_utc_2026"
        assert pools[0].remain == 749.5
        assert pools[0].numeric_id is False
        assert pools[1].id == "358204062466"
        assert pools[1].numeric_id is True

    def test_ent_usage_aggregation(self):
        payload = json.dumps(
            {
                "user_entitlement_pack_list": [
                    {
                        "entitlement_base_info": {"quota": {"credits_limit": 1000}},
                        "usage": {"credits_amount": 200},
                    },
                    {
                        "entitlement_base_info": {"quota": {"credits_limit": 500}},
                        "usage": {"credits_amount": 100},
                    },
                ]
            }
        )
        client = make_upstream(lambda req: (200, payload, False))
        remain, limit, used, packs = client.ent_usage(Auth(uid="u1", access_token="at"))
        assert (remain, limit, used, packs) == (1200, 1500, 300, 2)


# ---------------------------------------------------------------------------
# client 请求行为
# ---------------------------------------------------------------------------


class TestClientRequests:
    """客户端请求头与响应处理。"""

    def test_chat_stream_sends_headers_and_rewrites_body(self):
        captured: dict = {}

        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content)
            captured["path"] = request.url.path
            return 200, SOLO_SSE_FIXTURE, True

        client = make_upstream(behavior)
        auth = Auth(uid="u1", access_token="at1", machine_id="m" * 32, device_id="d" * 32)
        resp, status, body, err = client.chat_stream(
            auth, b'{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}'
        )
        try:
            assert err is None and status == 200 and resp is not None
            assert captured["auth"] == "Cloud-IDE-JWT at1"
            assert captured["path"] == C.EpChat
            assert captured["body"]["stream"] is True
            assert captured["body"]["function"] == "solo_work_lite"
            assert captured["body"]["config_name"] == "glm-5.2"
        finally:
            if resp is not None:
                resp.close()

    def test_chat_stream_http_error(self):
        client = make_upstream(lambda req: (429, "rate limited", False))
        resp, status, body, err = client.chat_stream(Auth(uid="u1", access_token="at"), b"{}")
        assert resp is None
        assert status == 429
        assert b"rate limited" in body
        assert err is None

    def test_chat_stream_transport_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = Client(
            agent_host="https://fake.example",
            transport=httpx.MockTransport(handler),
        )
        resp, status, body, err = client.chat_stream(Auth(uid="u1", access_token="at"), b"{}")
        assert resp is None and status == 0 and err is not None


class TestRefreshToken:
    """ExchangeToken 刷新。"""

    def _client(self, payload: str, captured: dict | None = None) -> Client:
        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            if captured is not None:
                captured["path"] = request.url.path
                captured["body"] = json.loads(request.content)
                captured["host"] = request.url.host
                captured["ua"] = request.headers.get("User-Agent")
            return 200, payload, False

        return make_upstream(behavior)

    def test_refresh_token_exchange(self):
        captured: dict = {}
        payload = json.dumps(
            {
                "Result": {
                    "Token": "new-at",
                    "RefreshToken": "new-rt",
                    "TokenExpireAt": 1786847930141,
                }
            }
        )
        client = self._client(payload, captured)
        auth = Auth(uid="u1", access_token="old", refresh_token="old-rt")
        client.refresh_token(auth)
        assert auth.access_token == "new-at"
        assert auth.refresh_token == "new-rt"
        assert auth.expires_at == 1786847930  # 毫秒 → 秒
        assert captured["path"] == C.EpExchange
        assert captured["body"]["ClientID"] == C.ClientID
        assert captured["body"]["RefreshToken"] == "old-rt"
        assert captured["body"]["ClientSecret"] == "-"

    def test_refresh_token_uses_auth_api_host(self):
        captured: dict = {}
        client = self._client(json.dumps({"Result": {"Token": "t"}}), captured)
        auth = Auth(uid="u1", access_token="a", refresh_token="r", api_host="https://custom.example")
        client.refresh_token(auth)
        assert captured["host"] == "custom.example"

    def test_refresh_token_duration_fallback(self):
        client = self._client(json.dumps({"Result": {"Token": "t", "TokenExpireDuration": 3600}}))
        auth = Auth(uid="u1", access_token="a", refresh_token="r")
        client.refresh_token(auth)
        assert auth.expires_at > 0

    def test_refresh_failure_keeps_old_fields(self):
        client = self._client(json.dumps({"Result": {}}))
        auth = Auth(uid="u1", access_token="old", refresh_token="old-rt")
        with pytest.raises(UpstreamError):
            client.refresh_token(auth)
        assert auth.access_token == "old"
        assert auth.refresh_token == "old-rt"

    def test_refresh_no_refresh_token(self):
        client = self._client("{}")
        with pytest.raises(UpstreamError):
            client.refresh_token(Auth(uid="u1", access_token="a"))

    def test_refresh_if_needed_skips_fresh(self):
        client = self._client(json.dumps({"Result": {"Token": "new"}}))
        auth = Auth(uid="u1", access_token="old", refresh_token="r", expires_at=9999999999)
        assert client.refresh_token_if_needed(auth, 3600) is False
        assert auth.access_token == "old"

    def test_refresh_if_needed_refreshes_expired(self):
        client = self._client(json.dumps({"Result": {"Token": "new"}}))
        auth = Auth(uid="u1", access_token="old", refresh_token="r", expires_at=1)
        assert client.refresh_token_if_needed(auth, 3600) is True
        assert auth.access_token == "new"


class TestCheckin:
    """签到状态与领取（含 9074 设备号重试）。"""

    def test_checkin_status_and_claim(self):
        routes = {
            C.EpCheckinStatus: '{"checked_in":false,"credits":150,"enable":true}',
            C.EpCheckinClaim: '{"message":"ok"}',
        }
        from tests.conftest import make_route_upstream

        client = make_route_upstream(routes)
        auth = Auth(uid="2666736248956905", access_token="at")
        checked_in, credits, enable = client.checkin_status(auth)
        assert (checked_in, credits, enable) == (False, 150, True)
        client.checkin_claim(auth)  # 不抛异常即成功

    def test_checkin_claim_sends_uid_device_id(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("X-Device-Id") or "")
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b'{"message":"ok"}',
            )

        client = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        client.checkin_claim(Auth(uid="2666736248956905", access_token="at"))
        assert seen[0] == "2666736248956905"

    def test_checkin_claim_falls_back_to_stored_device_id(self):
        """uid 非 16 位数字时，首选回退到 auth 文件里的 deviceId。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("X-Device-Id") or "")
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b'{"message":"ok"}',
            )

        client = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        client.checkin_claim(Auth(uid="not-16", device_id="1234567890123456", access_token="at"))
        assert seen[0] == "1234567890123456"

    def test_checkin_claim_business_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content='{"code":9004,"message":"order parameters are incorrect"}'.encode("utf-8"),
            )

        client = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        with pytest.raises(CheckinError) as exc:
            client.checkin_claim(Auth(uid="2666736248956905", access_token="at"))
        assert exc.value.code == 9004
        assert exc.value.retryable() is False

    def test_checkin_claim_retries_on_9074(self):
        """9074 → 换设备号重试；第二个候选成功。"""
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            device = request.headers.get("X-Device-Id") or ""
            attempts.append(device)
            # 首选值（uid）连续两次被拒，第二次换值后成功
            if len(attempts) <= 2:
                body = '{"code":9074,"message":"当前参与用户太多，请稍后再试"}'
            else:
                body = '{"code":0,"message":"ok"}'
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=body.encode("utf-8"),
            )

        client = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        auth = Auth(uid="2666736248956905", device_id="d" * 32, access_token="at")
        client.checkin_claim(auth)
        assert len(attempts) == 3
        assert attempts[0] == attempts[1] == "2666736248956905"  # 首选值试两次
        assert attempts[2] != "2666736248956905"  # 换到下一个候选

    def test_checkin_claim_all_candidates_fail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content='{"code":9074,"message":"busy"}'.encode("utf-8"),
            )

        client = Client(
            agent_host="https://fake.example",
            ug_host="https://fake.example",
            transport=httpx.MockTransport(handler),
            checkin_retry_delay=0.001,
        )
        auth = Auth(uid="2666736248956905", device_id="d" * 32, access_token="at")
        with pytest.raises(CheckinError) as exc:
            client.checkin_claim(auth)
        assert exc.value.code == 9074


class TestFetchModels:
    """模型表拉取。"""

    def test_fetch_models(self):
        detail = json.dumps(
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
        client = make_upstream(lambda req: (200, detail, False))
        infos = client.fetch_models(Auth(uid="u1", access_token="at"))
        assert len(infos) == 5

        first = infos[0]
        assert first.id == "glm-5.2"
        assert first.name == "GLM-5.2"
        assert first.fee_level == 2
        assert first.context_window == 256000
        assert first.has_rate is True
        assert first.rate == 0.39  # 折扣命中 → 折后价
        assert first.original_rate == 0.78
        assert first.discount_percent == 50
        assert first.discount_matched is True

        second = infos[1]
        assert second.has_rate is False

        assert infos[2].is_custom is True
        assert infos[4].is_invisible is True
        assert infos[4].name == ""  # "-" 归一化为空

    def test_fetch_models_empty_raises(self):
        client = make_upstream(lambda req: (200, '{"config_info_list":[]}', False))
        with pytest.raises(UpstreamError):
            client.fetch_models(Auth(uid="u1", access_token="at"))


class TestGetUserInfo:
    """GetUserInfo。"""

    def test_get_user_info(self):
        payload = json.dumps(
            {"Result": {"UserID": "u9", "ScreenName": "Nick", "EnterpriseID": "ent-9"}}
        )
        captured: dict = {}

        def behavior(request: httpx.Request) -> tuple[int, str, bool]:
            captured["path"] = request.url.path
            captured["token"] = request.headers.get("X-Cloudide-Token")
            return 200, payload, False

        client = make_upstream(behavior)
        uid, nick, ent = client.get_user_info(Auth(uid="u1", access_token="at"))
        assert (uid, nick, ent) == ("u9", "Nick", "ent-9")
        assert captured["path"] == C.EpUserInfo
        assert captured["token"] == "at"
