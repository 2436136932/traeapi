"""pytest 公共夹具：fake 上游、账号池、FastAPI TestClient。

对应原版 Go 测试里的 newFakeUpstream / newRouteUpstream / testPoolWith。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import httpx
import pytest
from starlette.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from traeapi.auth import Auth  # noqa: E402
from traeapi.config import Config  # noqa: E402
from traeapi.pool import Pool  # noqa: E402
from traeapi.server.app import create_app  # noqa: E402
from traeapi.server.state import ServerState  # noqa: E402
from traeapi.upstream.client import Client  # noqa: E402

# 模拟 SOLO SSE 响应（glm-5.2 回答"你好"）。
SOLO_SSE = (
    'event:metadata\ndata:{"model":"","session_id":"s1"}\n\n'
    'event:output\ndata:{"response":"你好","reasoning_content":"想一下","tool_calls":null}\n\n'
    'event:token_usage\ndata:{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}\n\n'
    'event:done\ndata:{"finish_reason":"stop"}\n\n'
)

# 捕获的 SOLO llm_utils_chat SSE 样例（真实结构，token 无）。
SOLO_SSE_FIXTURE = (
    'id:1\nevent:metadata\ndata:{"model":"","session_id":"897f0f3f-935a-4f42-a0fc-60f5140ccd02","prompt_completion_id":0}\n\n'
    'id:2\nevent:timing_cost\ndata:{"name":"llm_raw_chat_v2","preprocess_timing":71}\n\n'
    'event:output\ndata:{"response":"中国","reasoning_content":"让我想想","tool_calls":null}\n\n'
    'event:output\ndata:{"response":"的首都是北京。","reasoning_content":"","tool_calls":null}\n\n'
    'event:extra_info\ndata:{"reasoning_content":"让我想想"}\n\n'
    'event:token_usage\ndata:{"prompt_tokens":21,"completion_tokens":142,"total_tokens":163,"reasoning_tokens":135}\n\n'
    'event:done\ndata:{"finish_reason":"stop"}\n\n'
)


def make_upstream(
    behavior: Callable[[httpx.Request], tuple[int, str, bool]],
    checkin_retry_delay: float = 0.001,
) -> Client:
    """构造 ChatStream 走 fake 的 upstream.Client。

    behavior(request) -> (status, body, is_stream)
    """

    def handler(request: httpx.Request) -> httpx.Response:
        status, body, is_stream = behavior(request)
        content_type = "text/event-stream" if is_stream else "application/json"
        return httpx.Response(
            status_code=status,
            headers={"Content-Type": content_type},
            content=body.encode("utf-8"),
        )

    return Client(
        agent_host="https://fake.example",
        ug_host="https://fake.example",
        oauth_host="https://fake.example",
        transport=httpx.MockTransport(handler),
        checkin_retry_delay=checkin_retry_delay,
    )


def make_route_upstream(
    routes: dict[str, str],
    checkin_retry_delay: float = 0.001,
) -> Client:
    """按 URL path 分派固定响应的 fake 上游。

    签到链路涉及三个不同端点（status/claim/ent_usage），需要按路径区分。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in routes:
            return httpx.Response(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=routes[path].encode("utf-8"),
            )
        return httpx.Response(
            status_code=404,
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )

    return Client(
        agent_host="https://fake.example",
        ug_host="https://fake.example",
        oauth_host="https://fake.example",
        transport=httpx.MockTransport(handler),
        checkin_retry_delay=checkin_retry_delay,
    )


def make_pool(*auths: Auth, credits: int = 1000) -> Pool:
    """构造带积分的测试账号池（state_fp 为空 → 不落盘）。"""
    pool = Pool("")
    for auth in auths:
        pool.add(auth)
        pool.set_credits(auth.uid, credits)
    return pool


def make_auth(uid: str, token: str = "", expires_at: int = 9999999999) -> Auth:
    """构造凭证齐全、token 远未过期的测试账号。"""
    return Auth(
        uid=uid,
        access_token=token or f"at-{uid}",
        refresh_token=f"rt-{uid}",
        expires_at=expires_at,
    )


def make_state(
    pool: Pool,
    upstream: Client,
    *,
    api_key: str = "",
    config: Config | None = None,
    **kwargs,
) -> ServerState:
    """构造 ServerState（测试用，可覆盖各冷却参数）。"""
    cfg = config or Config(api_key=api_key)
    state = ServerState(
        pool=pool,
        upstream=upstream,
        config=cfg,
        api_key=api_key or cfg.api_key,
        auth_dir=cfg.auth_dir,
        plan_cooldown=cfg.plan_credit_dur or 12 * 3600.0,
        soft_cooldown=cfg.soft_rate_dur or 60.0,
        err_threshold=cfg.err_threshold,
        err_cooldown=cfg.err_cooldown_dur or 600.0,
        default_model=cfg.default_model,
        model_rates=dict(cfg.model_rates),
        hide_invisible_models=cfg.hide_invisible_models,
        solo_function=cfg.solo_function,
    )
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def make_test_client(state: ServerState) -> TestClient:
    """构造 FastAPI TestClient。"""
    return TestClient(create_app(state), raise_server_exceptions=False)


# 预热用的官方模型集（够 map_model 认出 glm-5.2 / glm-5 等常用名）。
PRIME_MODELS = [
    "glm-5.2",
    "glm-5",
    "glm-5-turbo",
    "kimi-k3",
    "Doubao-Seed-2.1-Pro",
    "DeepSeek-V4-Pro",
]


def prime_models(state: ServerState, model_ids: list[str] | None = None) -> None:
    """预热模型缓存，让 map_model / model_list 不再打上游。

    原版 Go 测试依赖包级 dynamicModelsCache 被前面的用例填充（隐式的测试顺序耦合）；
    Python 版每个 state 各有独立 catalog，因此需要显式预热才能稳定断言「上游被调了几次」。
    """
    from traeapi.upstream.client import ModelInfo

    ids = model_ids if model_ids is not None else PRIME_MODELS
    state.catalog.prime_cache([ModelInfo(id=mid, name=mid.upper()) for mid in ids])


def make_primed_state(pool: Pool, upstream: Client, **kwargs) -> ServerState:
    """构造已预热模型缓存的 ServerState（对话/轮换类用例用）。"""
    state = make_state(pool, upstream, **kwargs)
    prime_models(state)
    return state


@pytest.fixture(autouse=True)
def _reset_solo_function():
    """每个用例前后都把 SOLO function 复位到默认值（全局状态）。"""
    from traeapi.upstream import constants as C

    C.set_function("")
    yield
    C.set_function("")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """清理 TW2A_* 环境变量，避免宿主机配置串味。"""
    for name in (
        "TW2A_API_KEY",
        "TW2A_LISTEN",
        "TW2A_CALLBACK_PORT",
        "TW2A_AUTH_DIR",
        "TW2A_STATE_FILE",
        "TW2A_DEFAULT_MODEL",
        "TW2A_PLAN_CREDIT",
        "TW2A_SOFT_RATE",
        "TW2A_ERR_THRESHOLD",
        "TW2A_ERR_COOLDOWN",
        "TW2A_CHECKIN_HOUR",
        "TW2A_CHECKIN_RETRY_MINUTES",
        "TW2A_HIDE_INVISIBLE_MODELS",
        "TW2A_FUNCTION",
        "TW2A_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
