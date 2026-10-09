"""constants.py SOLO 上游技术常量（来自实测，禁止改动）。

对应原版 internal/upstream/constants.go。
"""

from __future__ import annotations

import threading

AgentHost = "https://trae-api-cn.mchost.guru"
UgHost = "https://api.trae.cn"
OAuthHost = "https://api.trae.com.cn"
ConsoleHost = "https://www.trae.cn"
ClientID = "en1oxy7wnw8j9n"  # SOLO stable
AppID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IdeVersion = "0.1.52"
IdeVersionCode = "20260811"
DeviceBrand = "83DG"
OSVersion = "Windows 11 Pro"

# SOLO function 取值（实测自 TraeWork 客户端 ai_agent.dll）。
# 四者都走同一个端点 /api/agent/v3/llm_utils_chat，返回的 SSE 事件序列相同。
FunctionWorkLite = "solo_work_lite"  # 轻量对话（本项目默认）
FunctionWorkRemote = "solo_work_remote"  # 远程 agent（TraeWork 原生取值）
FunctionDesignLite = "solo_design_lite"  # 设计模式（轻量）
FunctionDesignRemote = "solo_design_remote"  # 设计模式（远程）

# 默认 function（solo_work_lite）。运行时可用 set_function 切换。
Function = FunctionWorkLite

# 端点
EpChat = "/api/agent/v3/llm_utils_chat"
EpModels = "/api/ide/v1/get_detail_param"
EpExchange = "/cloudide/api/v3/trae/oauth/ExchangeToken"
EpUserInfo = "/cloudide/api/v3/trae/GetUserInfo"
EpCheckinStatus = "/trae/api/v2/ug/checkin_credits/status"
EpCheckinClaim = "/trae/api/v2/ug/checkin_credits/claim"
EpEntUsage = "/trae/api/v2/pay/ide_user_ent_usage"

# 允许切换的 function 白名单（顺序即面板展示顺序）。
_KNOWN_FUNCTIONS: tuple[str, ...] = (
    FunctionWorkLite,
    FunctionWorkRemote,
    FunctionDesignLite,
    FunctionDesignRemote,
)

# 各 function 的中文说明（面板展示用）。
_FUNCTION_DESCS: dict[str, str] = {
    FunctionWorkLite: "工作场景 · 轻量本地处理（默认，最稳）",
    FunctionWorkRemote: "工作场景 · 云端 agent 处理",
    FunctionDesignLite: "设计场景 · 轻量本地处理",
    FunctionDesignRemote: "设计场景 · 云端 agent 处理",
}

# 当前使用的 function（空串 = 用默认 Function）。
_active_lock = threading.Lock()
_active_function: str = ""


def active_function() -> str:
    """返回当前 SOLO function；未设置时回退默认值。"""
    with _active_lock:
        return _active_function or Function


def set_function(name: str) -> bool:
    """切换 SOLO function。空串表示恢复默认。

    未知取值返回 False 且不修改当前值 —— 避免拼错导致上游 4001（param is invalid）。
    """
    global _active_function
    trimmed = (name or "").strip()
    if trimmed == "":
        with _active_lock:
            _active_function = ""
        return True
    if not is_known_function(trimmed):
        return False
    with _active_lock:
        _active_function = trimmed
    return True


def is_known_function(name: str) -> bool:
    """判断是否为已知（可切换）的 function 取值。"""
    return name in _KNOWN_FUNCTIONS


def known_functions() -> list[str]:
    """返回可切换的 function 列表（副本，供面板展示）。"""
    return list(_KNOWN_FUNCTIONS)


def function_options() -> list[dict]:
    """返回全部可切换通道及其中文说明（顺序同白名单）。"""
    return [{"value": f, "desc": _FUNCTION_DESCS.get(f, "")} for f in _KNOWN_FUNCTIONS]


# 兼容别名（原版是包级变量 ACTIVE_FUNCTION，Python 侧用函数式接口）。
ACTIVE_FUNCTION = Function
