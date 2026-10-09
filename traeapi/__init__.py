"""traeapi — 把 TRAE SOLO 对话通道包装成 OpenAI 兼容 API，自带 Web 管理面板。

Python 复刻版（原版为 Go 实现）。模块划分与原版一一对应：

    traeapi.config            <- cmd/server/config.go
    traeapi.auth              <- internal/auth/auth.go
    traeapi.pool              <- internal/pool/pool.go
    traeapi.scheduler         <- internal/scheduler/scheduler.go
    traeapi.upstream.*        <- internal/upstream/*.go
    traeapi.server.*          <- internal/server/*.go
"""

__version__ = "1.0.0"
