"""state.py 服务运行时状态容器。

原版 Go 把这些依赖塞在 Handler.cfg（Config 结构体）里；Python 侧用一个显式
的 ServerState 承载，便于测试注入 fake 上游。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .. import apikey as apikey_mod
from ..auth import Auth
from ..config import Config
from ..pool import Pool
from ..upstream.client import Client
from .models import ModelCatalog
from .stats import UsageLog, USAGE_LOG_CAPACITY

# 请求体大小上限（8MB），超过返回 413。
MAX_BODY_BYTES = 8 << 20


@dataclass
class PendingLogin:
    """单次登录的临时上下文。"""

    state: str = "pending"  # pending | success | failed | canceled
    machine_id: str = ""
    device_id: str = ""
    callback_url: str = ""
    created_at: float = 0.0
    uid: str = ""
    nickname: str = ""
    err_msg: str = ""


@dataclass
class ServerState:
    """handler 依赖集合。"""

    pool: Pool
    upstream: Client
    config: Config

    api_key: str = ""
    auth_dir: str = "./auths"

    max_rotate: int = 3
    plan_cooldown: float = 12 * 3600.0
    soft_cooldown: float = 60.0
    err_threshold: int = 3
    err_cooldown: float = 600.0
    refresh_skew: float = 24 * 3600.0
    default_model: str = "glm-5.2"
    model_rates: dict[str, float] = field(default_factory=dict)
    hide_invisible_models: bool = False
    solo_function: str = ""

    catalog: ModelCatalog | None = None
    stats: UsageLog = field(default_factory=lambda: UsageLog(USAGE_LOG_CAPACITY))

    # 面板可在线修改的密钥：key_file 为持久化路径，key_source 记录当前来源。
    key_file: str = ""
    key_source: str = "none"  # stored | env | none
    # 保护 api_key 的读写（面板改密钥与请求鉴权可能并发）
    key_mu: threading.Lock = field(default_factory=threading.Lock)

    # Web 登录 pending 态：pending_id → 登录进行中的临时上下文。
    logins: dict[str, PendingLogin] = field(default_factory=dict)
    login_mu: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.max_rotate <= 0:
            self.max_rotate = 3
        if self.plan_cooldown <= 0:
            self.plan_cooldown = 12 * 3600.0
        if self.soft_cooldown <= 0:
            self.soft_cooldown = 60.0
        if self.err_threshold <= 0:
            self.err_threshold = 3
        if self.err_cooldown <= 0:
            self.err_cooldown = 600.0
        if self.refresh_skew <= 0:
            self.refresh_skew = 24 * 3600.0
        if not self.default_model:
            self.default_model = "glm-5.2"
        if not self.key_file:
            self.key_file = apikey_mod.key_file_for(self.config.state_file)
        if self.catalog is None:
            self.catalog = ModelCatalog(
                client=self.upstream,
                default_model=self.default_model,
                model_rates=self.model_rates,
                hide_invisible_models=self.hide_invisible_models,
            )

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------

    def pick_account(self) -> Auth | None:
        """从池中挑一个 healthy 账号（供模型目录使用）。"""
        return self.pool.pick()

    # ------------------------------------------------------------------
    # API 密钥（面板可在线修改）
    # ------------------------------------------------------------------

    def current_key(self) -> str:
        """读取当前生效的密钥（线程安全快照）。"""
        with self.key_mu:
            return self.api_key

    def set_key(self, key: str, source: str) -> None:
        """替换当前生效的密钥。"""
        with self.key_mu:
            self.api_key = key
            self.key_source = source

    def key_status(self) -> dict:
        """面板展示用的密钥状态（永不返回明文）。"""
        key = self.current_key()
        return {
            "configured": bool(key),
            "masked": apikey_mod.mask_key(key),
            "length": len(key),
            "source": self.key_source,
            "key_file": self.key_file,
            "min_length": apikey_mod.MIN_KEY_LENGTH,
        }


def resolve_api_key(config: Config, state_file: str) -> tuple[str, str]:
    """决定启动时使用哪个密钥，返回 (key, source)。

    优先级：面板持久化的密钥 > TW2A_API_KEY 环境变量 > 空（不鉴权）。
    """
    stored = apikey_mod.load_stored_key(state_file)
    if stored:
        return stored, "stored"
    if config.api_key:
        return config.api_key, "env"
    return "", "none"


def build_state(config: Config, pool: Pool, upstream: Client) -> ServerState:
    """按配置构造 ServerState（对应原版 main.go 里拼 Config 的那一段）。"""
    key, source = resolve_api_key(config, config.state_file)
    return ServerState(
        pool=pool,
        upstream=upstream,
        config=config,
        api_key=key,
        key_source=source,
        key_file=apikey_mod.key_file_for(config.state_file),
        auth_dir=config.auth_dir,
        plan_cooldown=config.plan_credit_dur,
        soft_cooldown=config.soft_rate_dur,
        err_threshold=config.err_threshold,
        err_cooldown=config.err_cooldown_dur,
        default_model=config.default_model,
        model_rates=config.model_rates,
        hide_invisible_models=config.hide_invisible_models,
        solo_function=config.solo_function,
    )
