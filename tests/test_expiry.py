"""积分到期时间（expire_at）的解析与汇总测试。

数据来源实测：POST {ug}/trae/api/v2/pay/ide_user_ent_usage 的每个包里
`expire_time`（Unix 秒）与 `entitlement_base_info.end_time` 始终相等；
`start_time` 是生效时间。
"""

from __future__ import annotations

import json
import time

from traeapi.server.admin import EXPIRING_SOON_DAYS, _expiry_summary
from traeapi.upstream import constants as C
from traeapi.upstream.client import PoolInfo
from tests.conftest import make_auth, make_pool, make_route_upstream, make_state, make_test_client

NOW = int(time.time())
DAY = 86400


def _pack(ent_id: str, limit: int, used: float, expire_at: int, start_at: int = 0, **extra) -> dict:
    """构造一个上游格式的权益包。"""
    pack = {
        "entitlement_base_info": {
            "entitlement_id": ent_id,
            "quota": {"credits_limit": limit},
            "end_time": expire_at,
            "start_time": start_at,
        },
        "usage": {"credits_amount": used},
        "expire_time": expire_at,
    }
    pack.update(extra)
    return pack


class TestPoolInfoExpiry:
    """PoolInfo 的到期字段解析。"""

    def test_parses_expire_and_start(self):
        payload = json.dumps(
            {
                "user_entitlement_pack_list": [
                    _pack("checkin_20261009_u1", 100, 10, NOW + 30 * DAY, NOW)
                ]
            }
        )
        client = make_route_upstream({C.EpEntUsage: payload})
        pools = client.ent_pools(make_auth("u1"))
        assert len(pools) == 1
        assert pools[0].expire_at == NOW + 30 * DAY
        assert pools[0].start_at == NOW

    def test_falls_back_to_end_time_when_expire_time_missing(self):
        """上游若只给 entitlement_base_info.end_time，也要能取到。"""
        pack = {
            "entitlement_base_info": {
                "entitlement_id": "free_u1",
                "quota": {"credits_limit": 500},
                "end_time": NOW + 5 * DAY,
            },
            "usage": {"credits_amount": 0},
            # 注意：没有包级 expire_time
        }
        payload = json.dumps({"user_entitlement_pack_list": [pack]})
        client = make_route_upstream({C.EpEntUsage: payload})
        pools = client.ent_pools(make_auth("u1"))
        assert pools[0].expire_at == NOW + 5 * DAY

    def test_missing_expiry_yields_zero(self):
        """上游完全不给到期字段时，expire_at 为 0（不臆造数值）。"""
        pack = {
            "entitlement_base_info": {
                "entitlement_id": "no_expiry",
                "quota": {"credits_limit": 100},
            },
            "usage": {"credits_amount": 0},
        }
        payload = json.dumps({"user_entitlement_pack_list": [pack]})
        client = make_route_upstream({C.EpEntUsage: payload})
        pools = client.ent_pools(make_auth("u1"))
        assert pools[0].expire_at == 0
        assert pools[0].start_at == 0

    def test_to_dict_includes_expire_at(self):
        info = PoolInfo(id="p1", limit=100, used=10, remain=90, expire_at=NOW + DAY, start_at=NOW)
        out = info.to_dict()
        assert out["expire_at"] == NOW + DAY
        assert out["start_at"] == NOW

    def test_to_dict_omits_start_when_zero(self):
        info = PoolInfo(id="p1", limit=100, used=10, remain=90, expire_at=NOW + DAY)
        out = info.to_dict()
        assert out["expire_at"] == NOW + DAY
        assert "start_at" not in out

    def test_to_dict_always_has_expire_at_key(self):
        """expire_at 始终存在（0 表示上游未提供），便于前端统一处理。"""
        out = PoolInfo(id="p1", limit=100).to_dict()
        assert "expire_at" in out and out["expire_at"] == 0

    def test_parses_real_world_multi_pack_response(self):
        """复刻实测响应：签到包按天顺延 + 月度包到月底。"""
        month_end = NOW + 22 * DAY
        packs = [
            _pack("monthly_bonus_202610_u1", 500, 0, month_end),
            _pack("checkin_20261001_u1", 150, 0, NOW + 8 * DAY),
            _pack("checkin_20261002_u1", 150, 0, NOW + 9 * DAY),
            _pack("checkin_20261003_u1", 150, 0, NOW + 10 * DAY),
        ]
        payload = json.dumps({"user_entitlement_pack_list": packs})
        client = make_route_upstream({C.EpEntUsage: payload})
        pools = client.ent_pools(make_auth("u1"))
        assert len(pools) == 4
        assert pools[0].expire_at == month_end
        assert pools[1].expire_at == NOW + 8 * DAY
        # 每个包的到期时间互不相同
        assert len({p.expire_at for p in pools}) == 4


class TestExpirySummary:
    """_expiry_summary 的汇总逻辑。"""

    def test_counts_credits_expiring_within_window(self):
        pools = [
            PoolInfo(id="a", limit=100, used=0, remain=100, expire_at=NOW + 3 * DAY),
            PoolInfo(id="b", limit=200, used=0, remain=200, expire_at=NOW + 30 * DAY),
        ]
        s = _expiry_summary(pools)
        assert s["expiring_soon_days"] == EXPIRING_SOON_DAYS
        assert s["expiring_soon_credits"] == 100  # 只有 3 天内的那个算
        assert s["expired_credits"] == 0
        assert s["next_expire_at"] == NOW + 3 * DAY

    def test_counts_expired_credits(self):
        pools = [
            PoolInfo(id="a", limit=100, used=40, remain=60, expire_at=NOW - DAY),
            PoolInfo(id="b", limit=100, used=0, remain=100, expire_at=NOW + 30 * DAY),
        ]
        s = _expiry_summary(pools)
        assert s["expired_credits"] == 60
        assert s["expiring_soon_credits"] == 0
        assert s["next_expire_at"] == NOW + 30 * DAY  # 已过期的不算「最近到期」

    def test_fully_used_expired_pack_not_counted(self):
        """已过期但已用完的包不应计入过期积分。"""
        pools = [PoolInfo(id="a", limit=100, used=100, remain=0, expire_at=NOW - DAY)]
        s = _expiry_summary(pools)
        assert s["expired_credits"] == 0

    def test_zero_remain_in_window_not_counted(self):
        pools = [PoolInfo(id="a", limit=100, used=100, remain=0, expire_at=NOW + DAY)]
        s = _expiry_summary(pools)
        assert s["expiring_soon_credits"] == 0

    def test_packs_without_expiry_ignored(self):
        pools = [
            PoolInfo(id="a", limit=100, used=0, remain=100, expire_at=0),
            PoolInfo(id="b", limit=100, used=0, remain=100),
        ]
        s = _expiry_summary(pools)
        assert s["next_expire_at"] == 0
        assert s["expiring_soon_credits"] == 0

    def test_next_expire_is_earliest_future(self):
        pools = [
            PoolInfo(id="a", limit=1, remain=1, expire_at=NOW + 20 * DAY),
            PoolInfo(id="b", limit=1, remain=1, expire_at=NOW + 2 * DAY),
            PoolInfo(id="c", limit=1, remain=1, expire_at=NOW + 9 * DAY),
        ]
        assert _expiry_summary(pools)["next_expire_at"] == NOW + 2 * DAY

    def test_empty_pools(self):
        s = _expiry_summary([])
        assert s["next_expire_at"] == 0
        assert s["expiring_soon_credits"] == 0
        assert s["expired_credits"] == 0

    def test_boundary_exactly_at_window_edge(self):
        """恰好落在窗口边界（7 天）内的应计入。"""
        pools = [PoolInfo(id="a", limit=100, remain=100, expire_at=NOW + EXPIRING_SOON_DAYS * DAY)]
        assert _expiry_summary(pools)["expiring_soon_credits"] == 100


class TestExpiryEndpoints:
    """HTTP 层暴露的到期信息。"""

    def _state(self, packs):
        payload = json.dumps({"user_entitlement_pack_list": packs})
        routes = {
            C.EpEntUsage: payload,
            C.EpCheckinStatus: '{"checked_in":false,"credits":100,"enable":true}',
        }
        return make_state(make_pool(make_auth("u1")), make_route_upstream(routes))

    def test_pools_endpoint_exposes_expire_at(self):
        state = self._state(
            [
                _pack("checkin_a", 150, 0, NOW + 5 * DAY),
                _pack("checkin_b", 150, 0, NOW + 40 * DAY),
            ]
        )
        client = make_test_client(state)
        body = client.get("/admin/api/pools").json()
        account = body["accounts"][0]
        pools = account["pools"]
        assert pools[0]["expire_at"] == NOW + 5 * DAY
        assert pools[1]["expire_at"] == NOW + 40 * DAY
        # 汇总字段
        assert account["expiring_soon_days"] == EXPIRING_SOON_DAYS
        assert account["expiring_soon_credits"] == 150  # 只有 5 天的那个
        assert account["next_expire_at"] == NOW + 5 * DAY
        assert account["expired_credits"] == 0

    def test_pools_note_mentions_expiry(self):
        state = self._state([_pack("p1", 100, 0, NOW + DAY)])
        client = make_test_client(state)
        note = client.get("/admin/api/pools").json()["note"]
        assert "到期" in note

    def test_credits_endpoint_exposes_expiry_summary(self):
        state = self._state(
            [
                _pack("soon", 100, 0, NOW + 2 * DAY),
                _pack("later", 900, 0, NOW + 60 * DAY),
            ]
        )
        client = make_test_client(state)
        account = client.get("/admin/api/credits").json()["accounts"][0]
        assert account["remain"] == 1000
        assert account["packs"] == 2
        assert account["expiring_soon_credits"] == 100
        assert account["next_expire_at"] == NOW + 2 * DAY

    def test_credits_aggregates_match_pool_sums(self):
        """credits 的 remain/limit/used 应与明细求和一致（复用同一次上游响应）。"""
        state = self._state(
            [
                _pack("a", 100, 25, NOW + DAY),
                _pack("b", 300, 75, NOW + 2 * DAY),
            ]
        )
        client = make_test_client(state)
        account = client.get("/admin/api/credits").json()["accounts"][0]
        assert account["limit"] == 400
        assert account["used"] == 100
        assert account["remain"] == 300

    def test_expired_pack_reported(self):
        state = self._state([_pack("old", 200, 50, NOW - 3 * DAY)])
        client = make_test_client(state)
        account = client.get("/admin/api/pools").json()["accounts"][0]
        assert account["expired_credits"] == 150
        assert account["next_expire_at"] == 0
