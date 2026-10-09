"""server 包：OpenAI 兼容 HTTP 接口 + 管理面板。

对应原版 internal/server/。
"""

from .state import ServerState, build_state

__all__ = ["ServerState", "build_state"]
