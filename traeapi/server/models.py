"""models.py 模型表：动态拉取 + 缓存 + 过滤 + 静态回退 + 模型名映射。

对应原版 internal/server/handler.go 中的模型相关部分。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..upstream.client import Client, ModelInfo

log = logging.getLogger("traeapi.models")

# TRAE「自定义模型」槽位的 config_name 前缀。
# 这类槽位由用户在 TRAE 客户端里接入第三方模型（Gemini / Claude / GPT-5 等），
# 上游的 is_custom_model 对它们反而是 false，因此需要按前缀一并识别。
CUSTOM_MODEL_PREFIX = "custom_model_"

# 动态模型缓存 TTL
DYNAMIC_MODELS_TTL = 3600.0  # 1h 成功缓存
MODELS_FETCH_FAIL_COOLDOWN = 300.0  # 5min 失败负缓存

# 静态 SOLO 模型表（动态拉取失败时回退）。
# 32 个 config_name，来自逆向报告。
STATIC_MODELS: list[dict] = [
    {"id": "Doubao-Seed-2.1-Pro", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "seed-code-pro-0430", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "Doubao-Seed-2.1-Turbo", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "Doubao-Seed-2.0-Code", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "DeepSeek-V4-Flash-Official", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "browser_use_subagent", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "glm-5.2", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "glm-5-turbo", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "glm-5", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "DeepSeek-V4-Pro", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "DeepSeek-V4-Flash", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "kimi-k3", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "kimi-k2.7-code", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "kimi-k2.6", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "minimax-m3", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "qwen-3.7-plus", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "sagitta", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "aquila", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_gemini", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_placeholder", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_1M_text", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_1M", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_kimi", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_claude", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_gpt-5", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_no-fc", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_deepseek_chat", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_deepseek_reasoner", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "custom_model_deepseek_v4", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "explore_sub_agent_v13", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "explore_sub_agent_v2", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
    {"id": "summary", "object": "model", "created": 1753600000, "owned_by": "trae-solo", "context_length": 131072},
]

# 上游未返回 context_window_tokens 时的兜底值（非上游数据）
FALLBACK_CONTEXT_LENGTH = 131072

# 静态表的 created 时间戳
STATIC_CREATED = 1753600000


def is_internal_model(model_id: str) -> bool:
    """判断是否为 TRAE 内部功能模型（子 agent / 工具 / 会话摘要）。

    这类模型不面向用户对话（倍率极低、多数没有独立展示名），默认从面板与
    /v1/models 隐藏：

        browser_use_subagent / file_search_agent / explore_sub_agent_v2 /
        explore_sub_agent_v13 / summary
    """
    lower = (model_id or "").lower()
    return (
        lower == "summary"
        or lower.endswith("_agent")
        or lower.endswith("_subagent")
        or "_sub_agent" in lower
    )


def normalize_model_name(s: str) -> str:
    """将下划线命名的内部名归一化为 config_name 风格（横线分隔）。

    对齐 Go 的 `strings.ToUpper(p[:1]) + strings.ToLower(p[1:])`：
    单字符段保持其（大写化后的）形式，空段原样保留。
    """
    parts = s.split("_")
    out: list[str] = []
    for part in parts:
        if part == "":
            out.append("")
            continue
        out.append(part[:1].upper() + part[1:].lower())
    return "-".join(out)


class ModelCatalog:
    """模型目录：动态拉取 + 缓存 + 过滤，静态表回退。"""

    def __init__(
        self,
        client: Client,
        default_model: str = "glm-5.2",
        model_rates: dict[str, float] | None = None,
        hide_invisible_models: bool = False,
    ) -> None:
        self.client = client
        self.default_model = default_model
        self.model_rates = dict(model_rates or {})
        self.hide_invisible_models = hide_invisible_models

        self._mu = threading.Lock()
        self._ids: list[ModelInfo] = []
        self._fetched = 0.0
        self._last_fail = 0.0

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def reset_cache(self) -> None:
        """清空缓存（测试用）。"""
        with self._mu:
            self._ids = []
            self._fetched = 0.0
            self._last_fail = 0.0

    def prime_cache(self, infos: list[ModelInfo]) -> None:
        """预热模型缓存（测试用）。

        对应 Go 测试里「前面的用例已经把包级 dynamicModelsCache 填好」的既成事实：
        预热后 map_model / model_list 不会再打上游，用例才能稳定断言请求次数。
        """
        with self._mu:
            self._ids = list(infos)
            self._fetched = time.time()
            self._last_fail = 0.0

    def fetch_dynamic(self, pick_account) -> list[ModelInfo]:
        """从池中任一健康账号拉模型列表（get_detail_param），缓存 1h。

        pick_account 是返回 Auth | None 的可调用对象。
        """
        with self._mu:
            if self._ids and (time.time() - self._fetched) < DYNAMIC_MODELS_TTL:
                return self._ids
            if self._last_fail and (time.time() - self._last_fail) < MODELS_FETCH_FAIL_COOLDOWN:
                return []

        acct = pick_account()
        if acct is None:
            return []
        try:
            infos = self.client.fetch_models(acct)
        except Exception:  # noqa: BLE001 - 拉取失败一律回退静态表
            with self._mu:
                self._last_fail = time.time()
            return []

        if not infos:
            with self._mu:
                self._last_fail = time.time()
            return []

        with self._mu:
            self._ids = infos
            self._fetched = time.time()
            self._last_fail = 0.0
        return infos

    def refresh_dynamic(self, pick_account) -> int:
        """绕过缓存，立即重新拉取上游模型表并更新缓存，返回上游返回的模型数量。

        供面板「重新拉取」按钮使用 —— 成功缓存与失败负缓存两者都跳过。
        """
        acct = pick_account()
        if acct is None:
            raise RuntimeError("no available account")
        try:
            infos = self.client.fetch_models(acct)
        except Exception as exc:  # noqa: BLE001
            with self._mu:
                self._last_fail = time.time()
            raise exc
        if not infos:
            raise RuntimeError("upstream returned empty model list")
        with self._mu:
            self._ids = infos
            self._fetched = time.time()
            self._last_fail = 0.0
        return len(infos)

    # ------------------------------------------------------------------
    # 列表构造
    # ------------------------------------------------------------------

    def model_list(self, pick_account) -> list[dict]:
        """动态获取模型列表并包装成 OpenAI 格式；失败回退静态表。"""
        out, _, _ = self.model_list_detailed(pick_account)
        return out

    def model_list_detailed(
        self, pick_account
    ) -> tuple[list[dict], int, int]:
        """同 model_list，并额外返回被过滤掉的自定义模型与内部功能模型数量。

        便于控制台提示「已隐藏 N 个自定义模型 / M 个内部模型」。
        """
        infos = self.fetch_dynamic(pick_account)
        hidden_custom = 0
        hidden_internal = 0

        if infos:
            out: list[dict] = []
            for mi in infos:
                # 只展示官方模型，两类都过滤：
                #   1) is_custom_model=true —— 用户在 TRAE 客户端自行添加的模型
                #   2) config_name 以 custom_model_ 开头 —— 自定义模型接入槽位
                if mi.is_custom or mi.id.startswith(CUSTOM_MODEL_PREFIX):
                    hidden_custom += 1
                    continue
                # 隐藏非面向用户的模型：
                #   1) 上游标记不可见「且」没有正式展示名 —— 内部代号模型
                #      （实测 sagitta / aquila 的 display_name 只是占位符 "-"）；
                #   2) 名字符合内部功能模型模式（子 agent / 摘要）作为兜底；
                #   3) 开启 hide_invisible_models 时，所有标记不可见的模型一并隐藏
                #      （含 glm-5 / glm-5-turbo / DeepSeek-V4-Pro 等旧版）。
                if (
                    mi.is_invisible and (mi.name == "" or self.hide_invisible_models)
                ) or is_internal_model(mi.id):
                    hidden_internal += 1
                    continue

                context_window = mi.context_window
                from_upstream = context_window > 0
                if not from_upstream:
                    context_window = FALLBACK_CONTEXT_LENGTH

                entry: dict[str, Any] = {
                    "id": mi.id,
                    "object": "model",
                    "created": STATIC_CREATED,
                    "owned_by": "trae-solo",
                    "context_length": context_window,
                    "context_from_upstream": from_upstream,
                }
                if mi.name:
                    entry["name"] = mi.name
                if mi.has_rate:
                    entry["rate"] = mi.rate  # 上游真实消耗倍率（命中会员折扣时为折后价）
                    if mi.original_rate > 0:
                        entry["original_rate"] = mi.original_rate
                    if mi.discount_percent > 0:
                        entry["discount_percent"] = mi.discount_percent
                        entry["discount_matched"] = mi.discount_matched
                if mi.fee_level > 0:
                    entry["fee_level"] = mi.fee_level
                out.append(entry)
            return out, hidden_custom, hidden_internal

        # 回退静态表：同样剔除自定义模型槽位与内部功能模型
        fallback: list[dict] = []
        for model in STATIC_MODELS:
            model_id = str(model.get("id", ""))
            if model_id.startswith(CUSTOM_MODEL_PREFIX):
                hidden_custom += 1
                continue
            if is_internal_model(model_id):
                hidden_internal += 1
                continue
            fallback.append(dict(model))
        return fallback, hidden_custom, hidden_internal

    # ------------------------------------------------------------------
    # 模型名映射
    # ------------------------------------------------------------------

    def known_model(self, model: str, pick_account) -> bool:
        """判断 model 是否在动态/静态模型表中。"""
        return any(m.get("id") == model for m in self.model_list(pick_account))

    def map_model(self, model: str, pick_account) -> str:
        """将客户端传入的 model 映射为 config_name：

            "glm-5.2"（config_name）        → 直接转发
            "glm-5.2__dev"（内部名）        → 去掉后缀映射回 config_name
            "auto" / ""                     → 默认模型
            其他未知                        → 抛 ValueError（调用方返回 400）
        """
        model = (model or "").strip()
        if model == "" or model == "auto":
            return self.default_model

        # 去掉内部名后缀（__dev / __max 等）
        base = model
        idx = model.find("__")
        if idx >= 0:
            base = model[:idx]

        if self.known_model(base, pick_account):
            return base

        # 宽松匹配：下划线 → 横线，大小写不敏感（deepseek_v4_pro → DeepSeek-V4-Pro）
        normalized = normalize_model_name(base)
        if self.known_model(normalized, pick_account):
            return normalized

        raise ValueError(f"unknown model {model!r}")
