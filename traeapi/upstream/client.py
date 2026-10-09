"""client.py SOLO 上游客户端：llm_utils_chat / get_detail_param / ExchangeToken /

checkin_credits / ide_user_ent_usage + 错误分类。
对应原版 internal/upstream/client.go。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

import httpx

from .. import jsonutil
from ..auth import Auth
from .constants import (
    AgentHost,
    ClientID,
    EpChat,
    EpCheckinClaim,
    EpCheckinStatus,
    EpEntUsage,
    EpExchange,
    EpModels,
    EpUserInfo,
    IdeVersion,
    OAuthHost,
    UgHost,
    active_function,
)
from .headers import checkin_device_plan, oauth_headers, solo_headers, ug_headers
from .payload import prepare_body
from .sse import SOLOStreamError, aggregate, iter_openai_sse

log = logging.getLogger("traeapi.upstream")

# 单次读取上限（对齐 Go 的 io.LimitReader(resp.Body, 1<<20)）
MAX_BODY = 1 << 20

# 签到限流业务码（实测：HTTP 200 但 code=9074「当前登录用户太多，请稍后重试」）
CODE_CHECKIN_BUSY = 9074


class ErrKind(str, Enum):
    """错误分类，pool 据此决定冷却时长。"""

    NONE = "none"
    PLAN_LIMIT = "plan_limit"  # 1005 + plan → 权益不足（硬冷却 12h）
    SOFT_RATE = "soft_rate"  # 429 → 短冷却 60s
    SESSION_DEAD = "session_dead"  # 401 + Cloud-IDE-JWT 失效 → 禁用
    NOT_FOUND = "not_found"  # 404 → 短冷却 60s 不累计 errCount
    SERVER = "server"  # 5xx
    CLIENT = "client"  # 其他 4xx

    def __str__(self) -> str:  # noqa: D105
        return self.value


class UpstreamError(Exception):
    """带分类的上游错误。"""

    def __init__(self, kind: ErrKind, status: int, msg: str) -> None:
        super().__init__(f"upstream {kind} (http {status}): {msg}")
        self.kind = kind
        self.status = status
        self.msg = msg


class CheckinError(Exception):
    """签到业务错误：HTTP 成功但响应体 code != 0。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"code {code}: {message}")
        self.code = code
        self.message = message

    def retryable(self) -> bool:
        """是否为瞬时（限流类）错误，可由调用方退避后重试。"""
        return self.code == CODE_CHECKIN_BUSY


_SESSION_DEAD_MARKERS = (
    "login",
    "token 失效",
    "token invalid",
    "session",
    "unauthorized",
    "401",
)


def classify(status: int, body: str) -> ErrKind:
    """按 HTTP 状态码 + body 判定错误类别。"""
    lower = (body or "").lower()
    # 1005 plan 权益不足
    if '"code":1005' in body or ("1005" in body and "plan" in lower):
        return ErrKind.PLAN_LIMIT
    if status == 401:
        for marker in _SESSION_DEAD_MARKERS:
            if marker.lower() in lower:
                return ErrKind.SESSION_DEAD
        return ErrKind.SESSION_DEAD
    if status == 429:
        return ErrKind.SOFT_RATE
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if status >= 400:
        return ErrKind.CLIENT
    return ErrKind.NONE


def truncate(s: str, n: int) -> str:
    """裁掉首尾空白并截断到 n 个字符。"""
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def normalize_expires_at(value: int) -> int:
    """把 ExchangeToken 的 TokenExpireAt 归一化为 Unix 秒。

    上游返回毫秒（如 1786847930141），auth 文件用秒（1786847930）。
    毫秒时间戳 ~1.7e12，秒时间戳 ~1.7e9，用 1e12 区分。
    """
    if value > 1_000_000_000_000:
        return value // 1000
    return value


# ---------------------------------------------------------------------------
# 模型信息
# ---------------------------------------------------------------------------


@dataclass
class ModelInfo:
    """动态模型信息（字段来自 get_detail_param 实测结构）。"""

    id: str = ""
    name: str = ""
    context_window: int = 0  # context_window_tokens.dev（上游真实值）
    max_tokens: int = 0  # = maxOutputTokens
    # Rate 当前生效的消耗倍率；若命中会员折扣则取折后价。
    # enable=false 或字段缺失时 has_rate=False，调用方不得臆造数值。
    rate: float = 0.0
    has_rate: bool = False
    original_rate: float = 0.0
    discount_percent: int = 0  # 会员折扣百分比（50 表示 5 折）；0 表示上游未提供折扣
    discount_matched: bool = False  # 上游 is_discount_matched：折扣条件是否已满足
    fee_level: int = 0  # display_config.fee_model_level
    is_custom: bool = False  # display_config.is_custom_model
    is_invisible: bool = False  # 上游标记 is_invisible_to_user=true


@dataclass
class RateInfo:
    """display_contact_config 中的消耗倍率与会员折扣信息。"""

    ok: bool = False
    rate: float = 0.0
    original_rate: float = 0.0
    discount_percent: int = 0
    discount_matched: bool = False


def parse_rate_info(raw: str) -> RateInfo:
    """解析 display_contact_config（JSON 字符串）中的真实消耗倍率与会员折扣。

    实测结构：

        {"consumption_rate":{"enable":true,"data":{"rate":0.78}},
         "discount":{"enable":true,"subKey":"member_discount",
                     "data":{"original_consumption_rate":0.78,"consumption_rate":0.39,
                             "member_discount":50,"is_discount_matched":false}},
         "reasoning":{"enable":true}}

    consumption_rate.rate 是标准倍率；仅当 is_discount_matched=true 时才按
    discount.data.consumption_rate（折后价）计费。取不到时 ok=False（不臆造数值）。
    """
    out = RateInfo()
    if not raw or not raw.strip():
        return out
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(cfg, dict):
        return out

    cr = cfg.get("consumption_rate")
    if not isinstance(cr, dict) or not cr.get("enable"):
        return out
    cr_data = cr.get("data")
    if not isinstance(cr_data, dict):
        cr_data = {}
    out.ok = True
    out.rate = _as_float(cr_data.get("rate"))
    out.original_rate = out.rate

    discount = cfg.get("discount")
    if isinstance(discount, dict) and discount.get("enable"):
        d_data = discount.get("data")
        if not isinstance(d_data, dict):
            d_data = {}
        member_discount = _as_int(d_data.get("member_discount"))
        if member_discount > 0:
            out.discount_percent = member_discount
            out.discount_matched = bool(d_data.get("is_discount_matched"))
            original = _as_float(d_data.get("original_consumption_rate"))
            if original > 0:
                out.original_rate = original
            discounted = _as_float(d_data.get("consumption_rate"))
            if out.discount_matched and discounted > 0:
                out.rate = discounted  # 折扣生效，按折后价计费
    return out


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def pick_context_window(tokens: Any) -> int:
    """从 context_window_tokens 挑上下文窗口：优先 dev，否则取最大值。"""
    if not isinstance(tokens, dict):
        return 0
    dev = tokens.get("dev")
    if isinstance(dev, (int, float)) and dev > 0:
        return int(dev)
    best = 0
    for value in tokens.values():
        if isinstance(value, (int, float)) and value > best:
            best = int(value)
    return best


# ---------------------------------------------------------------------------
# 权益包
# ---------------------------------------------------------------------------


@dataclass
class PoolInfo:
    """单个权益包的额度明细。

    TRAE 的额度由多个包组成（免费 / 每月登录赠送 / 每日签到 / 老用户福利…），
    计费按包顺序扣除，因此「本次扣的是哪个池」只能通过各包的 usage 变化看出来。
    """

    id: str = ""
    group: str = ""
    desc: str = ""
    limit: int = 0
    used: float = 0.0
    remain: float = 0.0
    # NumericID 表示 entitlement_id 为纯数字。实测 Work 专属积分包的 ID
    # 是纯数字（如 358204062466），而通用包带语义前缀（free_utc… / monthly_bonus… /
    # checkin…），故以此作为「Work 专属」的判定特征（启发式）。
    numeric_id: bool = False
    # 到期时间（Unix 秒，0 表示上游未提供）。实测上游在
    # entitlement_base_info.end_time 与包级 expire_time 各给一份，两者始终相等。
    expire_at: int = 0
    # 生效时间（Unix 秒，0 表示上游未提供）。
    start_at: int = 0

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "limit": self.limit,
            "used": self.used,
            "remain": self.remain,
            "numeric_id": self.numeric_id,
            "expire_at": self.expire_at,
        }
        if self.start_at:
            out["start_at"] = self.start_at
        if self.group:
            out["group_name"] = self.group
        if self.desc:
            out["desc"] = self.desc
        return out


def is_all_digits(s: str) -> bool:
    """判断字符串是否全为数字（且非空）。"""
    return bool(s) and s.isdigit() and s.isascii()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """SOLO 上游 HTTP 客户端。Host 字段可覆盖便于测试。"""

    def __init__(
        self,
        agent_host: str = AgentHost,
        ug_host: str = UgHost,
        oauth_host: str = OAuthHost,
        client_id: str = ClientID,
        timeout_seconds: int = 120,
        checkin_retry_delay: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.agent_host = agent_host
        self.ug_host = ug_host
        self.oauth_host = oauth_host
        self.client_id = client_id
        self.timeout_seconds = timeout_seconds
        # 签到被上游风控/限流拒绝（9074）后的退避时长；None 表示用默认值（1.5s）
        self.checkin_retry_delay = checkin_retry_delay

        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=90.0,
        )
        # 短 JSON 请求：有总超时兜底
        self.http = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            limits=limits,
            transport=transport,
            follow_redirects=False,
        )
        # 流式对话：不设总超时，避免长 SSE 流被截断；
        # 仅用 read 超时兜底「上游一直不返回首字节」的悬挂。
        self.stream_http = httpx.Client(
            timeout=httpx.Timeout(
                None, connect=float(timeout_seconds), read=float(timeout_seconds)
            ),
            limits=limits,
            transport=transport,
            follow_redirects=False,
        )

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接池。"""
        try:
            self.http.close()
        finally:
            self.stream_http.close()

    def _do_json(self, method: str, url: str, headers: dict, content: bytes) -> Any:
        """发请求并解 JSON；HTTP 非 2xx 时抛带 body 片段的 UpstreamError。"""
        try:
            resp = self.http.request(method, url, headers=headers, content=content)
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrKind.CLIENT, 0, f"transport error: {exc}") from exc
        raw = resp.content[:MAX_BODY]
        if resp.status_code >= 400:
            text = raw.decode("utf-8", errors="replace")
            raise UpstreamError(
                classify(resp.status_code, text), resp.status_code, truncate(text, 200)
            )
        try:
            return json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpstreamError(
                ErrKind.CLIENT,
                resp.status_code,
                f"invalid json: {exc}",
            ) from exc

    def _oauth_host_for(self, auth: Auth) -> str:
        return auth.api_host or self.oauth_host

    # ------------------------------------------------------------------
    # Token 刷新
    # ------------------------------------------------------------------

    def refresh_token(self, auth: Auth) -> None:
        """通过 ExchangeToken 强制刷新 access token（refreshToken 轮换）。

        成功时更新 auth 的字段；调用方负责 save_atomic。全程持 auth 写锁。
        """
        with auth.lock:
            self._refresh_locked(auth)

    def refresh_token_if_needed(self, auth: Auth, skew: float) -> bool:
        """仅当 token 在 skew 内即将过期（或已过期）时才刷新，返回是否真正刷新。

        持锁内重查，避免并发请求对同一账号重复 ExchangeToken 轮换。
        调用方仅在返回 True 时需要 save_atomic。
        """
        with auth.lock:
            if not auth.needs_refresh_locked(skew):
                return False
            self._refresh_locked(auth)
            return True

    def _refresh_locked(self, auth: Auth) -> None:
        """refresh_token 的持锁内部实现；调用方必须已持有 auth 写锁。

        任何失败路径都不改写 auth 字段，保证旧 refreshToken 可重试。
        """
        if not (auth.refresh_token or "").strip():
            raise UpstreamError(ErrKind.CLIENT, 0, "no refreshToken")

        host = self._oauth_host_for(auth)
        body = jsonutil.dumps(
            {
                "ClientID": self.client_id,
                "RefreshToken": auth.refresh_token,  # 已持写锁，直接读
                "ClientSecret": "-",
                "UserID": "",
            }
        ).encode("utf-8")

        data = self._do_json("POST", host + EpExchange, oauth_headers(), body)
        result = data.get("Result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            result = {}

        token = result.get("Token")
        token = token if isinstance(token, str) else ""
        if not token:
            raise UpstreamError(
                ErrKind.SESSION_DEAD,
                200,
                "refresh_failed: no token in response — re-login required",
            )

        auth.access_token = token
        new_refresh = result.get("RefreshToken")
        if isinstance(new_refresh, str) and new_refresh:
            auth.refresh_token = new_refresh

        # 过期时间：优先 TokenExpireAt（上游返回毫秒，需归一化为 Unix 秒）
        expire_at = _as_int(result.get("TokenExpireAt"))
        expire_duration = _as_int(result.get("TokenExpireDuration"))
        if expire_at > 0:
            auth.expires_at = normalize_expires_at(expire_at)
        elif expire_duration > 0:
            auth.expires_at = int(time.time()) + expire_duration

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    def chat_stream(
        self, auth: Auth, body: bytes
    ) -> tuple[httpx.Response | None, int, bytes, Exception | None]:
        """发 llm_utils_chat 请求并返回原始 SSE 响应流（调用方负责 close）。

        非 2xx 时 response 为 None、status 与 body 为上游响应，err 为 None；
        只有传输层失败才返回 err。
        """
        prepared = prepare_body(body)
        try:
            request = self.stream_http.build_request(
                "POST",
                self.agent_host + EpChat,
                headers=solo_headers(auth, True),
                content=prepared,
            )
            resp = self.stream_http.send(request, stream=True)
        except httpx.HTTPError as exc:
            log.warning("chat_stream uid=%s: transport error: %s", auth.uid, exc)
            return None, 0, b"", exc

        if resp.status_code >= 400:
            try:
                raw = resp.read()[:MAX_BODY]
            finally:
                resp.close()
            text = raw.decode("utf-8", errors="replace")
            kind = classify(resp.status_code, text)
            log.warning(
                "chat_stream uid=%s: upstream %s %s body=%s",
                auth.uid,
                resp.status_code,
                kind,
                truncate(text, 200),
            )
            return None, resp.status_code, raw, None

        return resp, resp.status_code, b"", None

    def iter_stream_bytes(self, resp: httpx.Response) -> Iterator[bytes]:
        """迭代上游 SSE 的字节块（用完需 resp.close()）。"""
        return resp.iter_bytes()

    # ------------------------------------------------------------------
    # 模型表
    # ------------------------------------------------------------------

    def fetch_models(self, auth: Auth) -> list[ModelInfo]:
        """拉 SOLO 模型表（get_detail_param）。"""
        body = jsonutil.dumps(
            {
                "function": active_function(),
                "config_names": None,
                "need_prompt": False,
                "current_config_info": None,
                "poly_prompt": True,
                "mode_type": None,
                "agent_type": None,
            }
        ).encode("utf-8")

        data = self._do_json("POST", self.agent_host + EpModels, solo_headers(auth, False), body)
        config_list = data.get("config_info_list") if isinstance(data, dict) else None
        if not isinstance(config_list, list):
            config_list = []

        out: list[ModelInfo] = []
        for cfg in config_list:
            if not isinstance(cfg, dict):
                continue
            config_name = cfg.get("config_name")
            if not isinstance(config_name, str) or not config_name:
                continue

            display = cfg.get("display_config")
            if not isinstance(display, dict):
                display = {}
            name = display.get("display_name")
            name = name if isinstance(name, str) else ""
            if name == "-":
                # 上游对无展示名的模型用 "-" 占位，统一按空处理
                name = ""

            info = ModelInfo(
                id=config_name,
                name=name,
                fee_level=_as_int(display.get("fee_model_level")),
                is_custom=bool(display.get("is_custom_model")),
                is_invisible=bool(cfg.get("is_invisible_to_user")),
            )
            window = pick_context_window(cfg.get("context_window_tokens"))
            if window > 0:
                info.context_window = window

            contact = cfg.get("display_contact_config")
            rate = parse_rate_info(contact if isinstance(contact, str) else "")
            if rate.ok:
                info.has_rate = True
                info.rate = rate.rate
                info.original_rate = rate.original_rate
                info.discount_percent = rate.discount_percent
                info.discount_matched = rate.discount_matched

            out.append(info)

        if not out:
            raise UpstreamError(ErrKind.CLIENT, 200, "models api returned empty list")
        return out

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------

    def checkin_status(self, auth: Auth) -> tuple[bool, int, bool]:
        """查询签到状态 → (checked_in, credits, enable)。"""
        data = self._do_json(
            "POST", self.ug_host + EpCheckinStatus, ug_headers(auth), b"{}"
        )
        if not isinstance(data, dict):
            data = {}
        return (
            bool(data.get("checked_in")),
            _as_int(data.get("credits")),
            bool(data.get("enable")),
        )

    def checkin_claim(self, auth: Auth) -> None:
        """执行签到。

        上游语义（实测）：
          - 失败同样是 HTTP 200 + body 里的 code（如 9074），所以必须解析 code；
          - 账号当天**已签到**时，claim 幂等返回 code 0（不再校验设备号）；
          - 当天**首次** claim 才走风控：`x-device-id` 取值决定成败 —— 实测
            传 uid 成功，传登录流程的 32 位 GUID / 派生值 / 随机 16 位数字都被 9074 拒。

        因此按 checkin_device_plan 的候选顺序重试：首选值（uid）试两次以覆盖瞬时挤兑，
        之后每个候选值各试一次；只有 9074 这类可重试错误才换值，其它错误立即返回。
        全部候选都失败时抛最后一个错误（调用方照实展示，不谎报成功）。
        """
        plan = checkin_device_plan(auth)
        last_err: Exception | None = None
        for i, device_id in enumerate(plan):
            attempts = 2 if i == 0 else 1
            for k in range(attempts):
                try:
                    self._checkin_claim_with(auth, device_id)
                    return
                except CheckinError as exc:
                    last_err = exc
                    if not exc.retryable():
                        raise  # 非 9074：不是设备号问题，直接返回
                    if k < attempts - 1:
                        time.sleep(self._checkin_retry_delay_value())
                except Exception as exc:  # noqa: BLE001 - 非业务错误同样立即上抛
                    raise exc
            if i < len(plan) - 1:
                time.sleep(self._checkin_retry_delay_value())
        if last_err is not None:
            raise last_err

    def _checkin_claim_with(self, auth: Auth, device_id: str) -> None:
        """用指定设备号发一次 claim。"""
        headers = ug_headers(auth)
        if device_id:
            headers["X-Device-Id"] = device_id
        data = self._do_json("POST", self.ug_host + EpCheckinClaim, headers, b"{}")
        if not isinstance(data, dict):
            # 空/非 JSON body：视为成功（部分成功响应无 body）
            return
        code = _as_int(data.get("code"))
        if code != 0:
            message = data.get("message")
            raise CheckinError(code, message if isinstance(message, str) else "")

    def _checkin_retry_delay_value(self) -> float:
        """返回签到重试退避时长（默认 1.5s，可由 checkin_retry_delay 注入）。"""
        if self.checkin_retry_delay is not None and self.checkin_retry_delay > 0:
            return self.checkin_retry_delay
        return 1.5

    # ------------------------------------------------------------------
    # 积分 / 额度
    # ------------------------------------------------------------------

    def user_ent_usage(self, auth: Auth) -> int:
        """聚合积分（ide_user_ent_usage 的 credits_limit 求和）。"""
        remain, _, _, _ = self.ent_usage(auth)
        return remain

    def ent_usage(self, auth: Auth) -> tuple[int, int, int, int]:
        """查询账号额度明细 → (remain, limit, used, packs)。

        remain = limit - used，usage.credits_amount 是已用积分（实测）。
        """
        data = self._do_json("POST", self.ug_host + EpEntUsage, ug_headers(auth), b"{}")
        packs_list = data.get("user_entitlement_pack_list") if isinstance(data, dict) else None
        if not isinstance(packs_list, list):
            packs_list = []

        remain = limit = used = packs = 0
        for pack in packs_list:
            if not isinstance(pack, dict):
                continue
            base = pack.get("entitlement_base_info")
            if not isinstance(base, dict):
                base = {}
            quota = base.get("quota")
            if not isinstance(quota, dict):
                quota = {}
            pack_limit = _as_int(quota.get("credits_limit"))
            if pack_limit <= 0:
                continue
            usage = pack.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            pack_used = int(_as_float(usage.get("credits_amount")))
            limit += pack_limit
            used += pack_used
            remain += pack_limit - pack_used
            packs += 1
        return remain, limit, used, packs

    def ent_pools(self, auth: Auth) -> list[PoolInfo]:
        """返回账号各权益包的额度明细（按上游返回顺序）。"""
        data = self._do_json("POST", self.ug_host + EpEntUsage, ug_headers(auth), b"{}")
        packs_list = data.get("user_entitlement_pack_list") if isinstance(data, dict) else None
        if not isinstance(packs_list, list):
            packs_list = []

        out: list[PoolInfo] = []
        for pack in packs_list:
            if not isinstance(pack, dict):
                continue
            base = pack.get("entitlement_base_info")
            if not isinstance(base, dict):
                base = {}
            quota = base.get("quota")
            if not isinstance(quota, dict):
                quota = {}
            pack_limit = _as_int(quota.get("credits_limit"))
            if pack_limit <= 0:
                continue  # 免费包额度为 0，不参与计费
            ent_id = base.get("entitlement_id")
            ent_id = ent_id if isinstance(ent_id, str) else ""
            usage = pack.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            pack_used = _as_float(usage.get("credits_amount"))
            group = pack.get("group_name")
            desc = pack.get("display_desc")
            # 到期时间：包级 expire_time 优先，回退 entitlement_base_info.end_time
            # （实测两者始终相等；上游未来若只给其一也能取到）。
            expire_at = _as_int(pack.get("expire_time")) or _as_int(base.get("end_time"))
            out.append(
                PoolInfo(
                    id=ent_id,
                    group=group if isinstance(group, str) else "",
                    desc=desc if isinstance(desc, str) else "",
                    limit=pack_limit,
                    used=pack_used,
                    remain=float(pack_limit) - pack_used,
                    numeric_id=is_all_digits(ent_id),
                    expire_at=expire_at,
                    start_at=_as_int(base.get("start_time")),
                )
            )
        return out

    # ------------------------------------------------------------------
    # 用户信息
    # ------------------------------------------------------------------

    def get_user_info(self, auth: Auth) -> tuple[str, str, str]:
        """查询账号信息（登录用）→ (uid, nickname, enterprise_id)。"""
        host = self._oauth_host_for(auth)
        body = jsonutil.dumps({"ReqSource": "IDE", "IDEVersion": IdeVersion}).encode("utf-8")
        headers = oauth_headers()
        headers["X-Cloudide-Token"] = auth.jwt()  # 读锁快照

        data = self._do_json("POST", host + EpUserInfo, headers, body)
        result = data.get("Result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            result = {}
        uid = result.get("UserID")
        nickname = result.get("ScreenName")
        enterprise = result.get("EnterpriseID")
        return (
            uid if isinstance(uid, str) else "",
            nickname if isinstance(nickname, str) else "",
            enterprise if isinstance(enterprise, str) else "",
        )


# 便于外部直接引用
__all__ = [
    "CODE_CHECKIN_BUSY",
    "CheckinError",
    "Client",
    "ErrKind",
    "ModelInfo",
    "PoolInfo",
    "RateInfo",
    "SOLOStreamError",
    "UpstreamError",
    "aggregate",
    "classify",
    "iter_openai_sse",
    "normalize_expires_at",
    "parse_rate_info",
    "pick_context_window",
    "truncate",
]
