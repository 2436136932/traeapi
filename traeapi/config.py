"""config.py 加载 JSON 配置 + TW2A_* 环境变量覆盖。

APIKey 只从环境变量 TW2A_API_KEY 读取（脱敏纪律：key 走 env，不落盘 git）。
对应原版 cmd/server/config.go。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Go time.ParseDuration 支持的单位（含中文文档里的常用写法）。
_DURATION_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,  # U+00B5
    "μs": 1e-6,  # U+03BC
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")


class ConfigError(ValueError):
    """配置解析失败（对齐 Go 的 error 返回）。"""


def parse_duration(value: str) -> float:
    """解析 Go duration 字符串，返回秒数（float）。

    支持 `12h` / `60s` / `10m` / `1h30m` / `1.5h` / `500ms` / `-5m` / `0`。
    与原版 time.ParseDuration 语义对齐：空串、`0`、无法解析都报错
    （原版 `time.ParseDuration("0")` 合法返回 0；空串报错）。
    """
    if value is None:
        raise ConfigError("invalid duration: <nil>")
    s = str(value).strip()
    if s == "":
        raise ConfigError('time: invalid duration ""')
    if s in ("0", "+0", "-0"):
        return 0.0

    sign = 1.0
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1.0
        s = s[1:]
    if s == "":
        raise ConfigError(f"time: invalid duration {value!r}")

    total = 0.0
    pos = 0
    matched = False
    while pos < len(s):
        m = _DURATION_TOKEN.match(s, pos)
        if not m:
            raise ConfigError(f"time: invalid duration {value!r}")
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
        matched = True
    if not matched:
        raise ConfigError(f"time: invalid duration {value!r}")
    return sign * total


def format_duration(seconds: float) -> str:
    """把秒数格式化成 Go duration 风格的短字符串（日志/展示用）。"""
    if seconds <= 0:
        return "0s"
    for unit, size in (("h", 3600.0), ("m", 60.0), ("s", 1.0)):
        if seconds >= size:
            val = seconds / size
            text = f"{val:.6f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
    return f"{seconds * 1000:.6f}".rstrip("0").rstrip(".") + "ms"


def parse_listen(listen: str) -> tuple[str, int]:
    """把 Go 风格监听地址 `:7864` / `0.0.0.0:7864` / `127.0.0.1:8080` 拆成 (host, port)。

    原版直接把 `:7864` 交给 http.Server.Addr，Go 会监听所有网卡；
    Python 侧统一映射为 `0.0.0.0`。
    """
    s = (listen or "").strip()
    if s == "":
        return "0.0.0.0", 7864
    if s.startswith(":"):
        return "0.0.0.0", _port_of(s[1:])
    if ":" in s:
        host, _, port = s.rpartition(":")
        host = host.strip("[]") or "0.0.0.0"
        return host, _port_of(port)
    # 纯端口
    return "0.0.0.0", _port_of(s)


def _port_of(text: str) -> int:
    try:
        port = int(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid listen port: {text!r}") from exc
    if not (0 <= port <= 65535):
        raise ConfigError(f"invalid listen port: {text!r}")
    return port


@dataclass
class CooldownConfig:
    """冷却类配置（原始字符串 + 解析后的秒数）。"""

    plan_credit: str = "12h"
    soft_rate: str = "60s"
    err_threshold: int = 3
    err_cooldown: str = "10m"

    # 解析后（秒）
    plan_credit_dur: float = 0.0
    soft_rate_dur: float = 0.0
    err_cooldown_dur: float = 0.0


@dataclass
class ScheduleConfig:
    """定时任务配置。"""

    checkin_hour: int = 9
    refresh_hours: list[int] = field(default_factory=lambda: [3])
    checkin_retry_minutes: int = 30


@dataclass
class UpstreamConfig:
    """上游请求配置。"""

    timeout_seconds: int = 120


@dataclass
class Config:
    """顶层配置，对应原版 Config 结构体。"""

    listen: str = ":7864"
    callback_port: str = "18080"
    # 只读 env TW2A_API_KEY（不读 json）
    api_key: str = ""
    auth_dir: str = "./auths"
    state_file: str = "./data/state.json"
    default_model: str = "glm-5.2"
    # 面板展示用的模型倍率（本地参考系数，上游并不提供该数据）。
    # 未配置的模型按 1.0 展示；仅影响控制台显示，不影响转发逻辑。
    model_rates: dict[str, float] = field(default_factory=dict)
    # 为 True 时隐藏上游标记 is_invisible_to_user=true 的模型
    hide_invisible_models: bool = False
    # 上游 SOLO function，默认 solo_work_lite
    solo_function: str = ""

    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)

    @property
    def plan_credit_dur(self) -> float:
        return self.cooldown.plan_credit_dur

    @property
    def soft_rate_dur(self) -> float:
        return self.cooldown.soft_rate_dur

    @property
    def err_cooldown_dur(self) -> float:
        return self.cooldown.err_cooldown_dur

    @property
    def err_threshold(self) -> int:
        return self.cooldown.err_threshold

    def host_port(self) -> tuple[str, int]:
        """主服务监听 (host, port)。"""
        return parse_listen(self.listen)

    def callback_host_port(self) -> tuple[str, int] | None:
        """回调服务监听地址；`0` / 空 表示不启动。"""
        cp = (self.callback_port or "").strip()
        if cp == "" or cp == "0":
            return None
        return "127.0.0.1", _port_of(cp)


def default_config() -> Config:
    """返回默认配置（对应 Go 的 Default()）。"""
    return Config()


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_bool(value, fallback: bool) -> bool:
    """对齐 Go strconv.ParseBool：1/t/T/TRUE/true/True/0/f/F/FALSE/false/False。"""
    if isinstance(value, bool):
        return value
    s = str(value).strip()
    if s in ("1", "t", "T", "true", "TRUE", "True"):
        return True
    if s in ("0", "f", "F", "false", "FALSE", "False"):
        return False
    return fallback


def _apply_env(cfg: Config) -> None:
    """TW2A_* 环境变量覆盖（变量名与原版完全一致，不可更改）。"""
    env = os.environ
    if v := env.get("TW2A_API_KEY"):
        cfg.api_key = v
    if v := env.get("TW2A_LISTEN"):
        cfg.listen = v
    if v := env.get("TW2A_CALLBACK_PORT"):
        cfg.callback_port = v
    if v := env.get("TW2A_AUTH_DIR"):
        cfg.auth_dir = v
    if v := env.get("TW2A_STATE_FILE"):
        cfg.state_file = v
    if v := env.get("TW2A_DEFAULT_MODEL"):
        cfg.default_model = v
    if v := env.get("TW2A_PLAN_CREDIT"):
        cfg.cooldown.plan_credit = v
    if v := env.get("TW2A_SOFT_RATE"):
        cfg.cooldown.soft_rate = v
    if v := env.get("TW2A_ERR_THRESHOLD"):
        cfg.cooldown.err_threshold = _as_int(v, cfg.cooldown.err_threshold)
    if v := env.get("TW2A_ERR_COOLDOWN"):
        cfg.cooldown.err_cooldown = v
    if v := env.get("TW2A_CHECKIN_HOUR"):
        cfg.schedule.checkin_hour = _as_int(v, cfg.schedule.checkin_hour)
    if v := env.get("TW2A_CHECKIN_RETRY_MINUTES"):
        cfg.schedule.checkin_retry_minutes = _as_int(v, cfg.schedule.checkin_retry_minutes)
    if v := env.get("TW2A_HIDE_INVISIBLE_MODELS"):
        cfg.hide_invisible_models = _as_bool(v, cfg.hide_invisible_models)
    if v := env.get("TW2A_FUNCTION"):
        cfg.solo_function = v
    if v := env.get("TW2A_TIMEOUT_SECONDS"):
        cfg.upstream.timeout_seconds = _as_int(v, cfg.upstream.timeout_seconds)


def _normalize(cfg: Config) -> Config:
    """解析 duration、补默认值（对应 Go 的 normalize()）。"""
    try:
        cfg.cooldown.plan_credit_dur = parse_duration(cfg.cooldown.plan_credit)
    except ConfigError as exc:
        raise ConfigError(f"cooldown.plan_credit: {exc}") from exc
    try:
        cfg.cooldown.soft_rate_dur = parse_duration(cfg.cooldown.soft_rate)
    except ConfigError as exc:
        raise ConfigError(f"cooldown.soft_rate: {exc}") from exc
    try:
        cfg.cooldown.err_cooldown_dur = parse_duration(cfg.cooldown.err_cooldown)
    except ConfigError as exc:
        raise ConfigError(f"cooldown.err_cooldown: {exc}") from exc

    if cfg.cooldown.err_threshold <= 0:
        cfg.cooldown.err_threshold = 3
    if cfg.upstream.timeout_seconds <= 0:
        cfg.upstream.timeout_seconds = 120
    if not cfg.default_model:
        cfg.default_model = "glm-5.2"
    if not cfg.listen:
        cfg.listen = ":7864"
    if not cfg.listen.startswith(":") and ":" not in cfg.listen:
        cfg.listen = ":" + cfg.listen
    # CallbackPort：空/未设 → 默认 18080；显式 "0" → 不起回调 server（纯手动粘贴模式）
    if cfg.callback_port == "":
        cfg.callback_port = "18080"
    if cfg.schedule.refresh_hours is None:
        cfg.schedule.refresh_hours = [3]
    return cfg


def _load_json_into(cfg: Config, raw: dict) -> None:
    """把 config.json 的字段灌进 Config（键名与原版 json tag 一致）。"""
    if not isinstance(raw, dict):
        raise ConfigError("parse config: top-level value must be an object")

    simple = {
        "listen": "listen",
        "callback_port": "callback_port",
        "auth_dir": "auth_dir",
        "state_file": "state_file",
        "default_model": "default_model",
        "solo_function": "solo_function",
    }
    for key, attr in simple.items():
        if key in raw and raw[key] is not None:
            setattr(cfg, attr, str(raw[key]))

    if "hide_invisible_models" in raw and raw["hide_invisible_models"] is not None:
        cfg.hide_invisible_models = _as_bool(raw["hide_invisible_models"], False)

    if "model_rates" in raw and isinstance(raw["model_rates"], dict):
        rates: dict[str, float] = {}
        for k, v in raw["model_rates"].items():
            try:
                rates[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        cfg.model_rates = rates

    cd = raw.get("cooldown")
    if isinstance(cd, dict):
        if cd.get("plan_credit") is not None:
            cfg.cooldown.plan_credit = str(cd["plan_credit"])
        if cd.get("soft_rate") is not None:
            cfg.cooldown.soft_rate = str(cd["soft_rate"])
        if cd.get("err_cooldown") is not None:
            cfg.cooldown.err_cooldown = str(cd["err_cooldown"])
        if cd.get("err_threshold") is not None:
            cfg.cooldown.err_threshold = _as_int(cd["err_threshold"], cfg.cooldown.err_threshold)

    sc = raw.get("schedule")
    if isinstance(sc, dict):
        if sc.get("checkin_hour") is not None:
            cfg.schedule.checkin_hour = _as_int(sc["checkin_hour"], cfg.schedule.checkin_hour)
        if sc.get("checkin_retry_minutes") is not None:
            cfg.schedule.checkin_retry_minutes = _as_int(
                sc["checkin_retry_minutes"], cfg.schedule.checkin_retry_minutes
            )
        if isinstance(sc.get("refresh_hours"), list):
            hours: list[int] = []
            for h in sc["refresh_hours"]:
                try:
                    hours.append(int(h))
                except (TypeError, ValueError):
                    continue
            cfg.schedule.refresh_hours = hours

    up = raw.get("upstream")
    if isinstance(up, dict) and up.get("timeout_seconds") is not None:
        cfg.upstream.timeout_seconds = _as_int(up["timeout_seconds"], cfg.upstream.timeout_seconds)


def load(path: str | Path | None = "config.json") -> Config:
    """从 path 读配置，再用 TW2A_* env 覆盖。path 为空或不存在时用默认 + env。

    注意：APIKey 永不从 json 读取（对齐原版 `json:"-"`）。
    """
    cfg = default_config()
    if path:
        p = Path(path)
        try:
            raw_text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 配置文件可选：不存在 → 纯默认 + env
            raw_text = ""
        except OSError as exc:
            raise ConfigError(f"read config: {exc}") from exc

        if raw_text.strip():
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"parse config: {exc}") from exc
            _load_json_into(cfg, parsed)

    _apply_env(cfg)
    return _normalize(cfg)
