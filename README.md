<div align="center">

<img src="./docs/logo.svg" alt="trae2api-web logo" width="110" height="110" />


# trae2api-web

**TRAE SOLO 逆向工程服务 · OpenAI 兼容 API · 可视化账号管理面板**

[![Go Version](https://img.shields.io/badge/Go-1.22+-00ADD8?style=flat-square&logo=go&logoColor=white)](https://golang.org/)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20Compatible-412991?style=flat-square&logo=openai&logoColor=white)](https://platform.openai.com/)
[![Web Admin](https://img.shields.io/badge/Web%20Admin-Built--in-10b981?style=flat-square)](http://localhost:7864/admin)
[![License](https://img.shields.io/badge/License-MIT-blue?style=flat-square)](LICENSE)

</div>

---

> 基于上游 [https://github.com/Sliverkiss/traework2api](https://github.com/Sliverkiss/traework2api) 改进。
> 
## 概述

`trae2api-web` 是一个将 TRAE SOLO 对话通道包装为标准 OpenAI 协议（`/v1/chat/completions` 与 `/v1/models`）的高性能反向代理服务。基于纯 Go 标准库构建，具备极低资源消耗与高并发处理能力。

本项目内置了轻量级 Web 控制台，支持多账号凭证管理、额度监控、自动化轮换保活以及开箱即用的一键网页登录闭环。

## 核心特性

- **OpenAI 协议兼容**：提供标准 `/v1/chat/completions`（支持流式 Streaming 与非流式）与 `/v1/models` 端点，无缝接入 NextChat、Chatbox、Claude Code、Cline 等客户端。
- **可视化 Web 管理控制台**：内置轻量 Web 界面（`GET /admin`），实时展示账号配额（剩余/已用/总量）、签到状态与健康度，支持多账号并发查询与自动刷新。
- **凭证全生命周期管理**：提供 Web 凭证导入、软启停开关、昵称修改、删除及一键 Web 登录闭环（无需手动抓包或提取 Token）。
- **多账号智能调度池**：基于账号可用积分降序挑选，自动处理 1005、429、401、5xx 等异常状态，支持动态冷却与故障自动轮转。
- **自动化运维与保活**：每日定时自动签到，并在 Token 过期前 24 小时自动预刷新与原子落盘，保障长周期稳定可用。
- **新模型支持**：同步支持 glm-5.3、glm-5.2 等新版模型调度，适配最新协议版本。
- **纯净轻量**：纯 Go 标准库开发，零第三方运行时依赖，静态编译产物小巧，内存占用极低。

## 快速开始

### Docker Compose 部署（推荐）

1. **准备目录与配置文件**

```bash
mkdir -p auths data
cp .env.example .env
```

Windows PowerShell 等价写法：

```powershell
New-Item -ItemType Directory -Force -Path auths, data | Out-Null
Copy-Item .env.example .env
```

编辑 `.env` 文件，配置自定义的管理鉴权密钥：

```env
TW2A_API_KEY=your_secure_api_key
```

2. **构建并启动服务**

```bash
docker compose up -d --build
```

3. **接口健康检查与模型验证**

```bash
# 健康检查
curl http://127.0.0.1:7864/healthz

# 查看可用模型列表（需鉴权）
curl -H "Authorization: Bearer ${TW2A_API_KEY}" http://127.0.0.1:7864/v1/models

# 查看账号池状态（需鉴权）
curl -H "Authorization: Bearer ${TW2A_API_KEY}" http://127.0.0.1:7864/status
```

### 本地直接运行

环境要求：Go 1.22+

**Linux / macOS**

```bash
# 设置访问密钥
export TW2A_API_KEY="your_secure_api_key"

# 编译并启动服务
go build -o trae2api-web ./cmd/server
./trae2api-web
```

**Windows（PowerShell）**

```powershell
# 1. 设置访问密钥（新开终端需重新设置，不会持久化）
$env:TW2A_API_KEY = "your_secure_api_key"

# 2. 编译并启动（前台运行，Ctrl+C 停止）
go build -o trae2api-web.exe ./cmd/server
.\trae2api-web.exe
```

后台常驻运行（关闭终端不退出）：

```powershell
cd <项目目录>
$env:TW2A_API_KEY = "your_secure_api_key"
Start-Process -FilePath .\trae2api-web.exe -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

停止与重启：

```powershell
# 停止
Get-Process trae2api-web | Stop-Process

# 修改代码后重新编译再启动
go build -o trae2api-web.exe ./cmd/server
.\trae2api-web.exe
```

Windows 本地运行注意事项：

- **本地运行不会自动加载 `.env`**：服务仅从环境变量与 `config.json` 读取配置，`.env` 仅供 `docker compose` 做变量插值。未设置 `TW2A_API_KEY` 时密钥为空，所有接口返回 401。
- PowerShell 中的 `curl` 是 `Invoke-WebRequest` 的别名，验证接口请使用 `curl.exe`。
- `config.json` 与 `auths/` 目录均允许不存在：前者缺失时回退内置默认配置，后者缺失时账号池为空但服务可正常启动。

### 一键启动脚本（Windows）

项目根目录提供了 `start.ps1` 与 `start.cmd`，自动完成密钥检查、按需编译、启动与健康检查：

```powershell
.\start.ps1                 # 后台启动（默认，关闭终端不退出）
.\start.ps1 -Foreground     # 前台运行，Ctrl+C 停止
.\start.ps1 -Restart        # 先停止已有进程再启动
.\start.ps1 -Stop           # 停止服务
.\start.ps1 -Port 8080      # 指定监听端口（覆盖 config.json 中的 listen）
```

也可以直接双击 `start.cmd`（启动）或 `stop.cmd`（停止），二者均以 `-ExecutionPolicy Bypass` 调用 `start.ps1`，避免 PowerShell 执行策略拦截。

停止服务的三种方式：

```powershell
.\start.ps1 -Stop                        # 推荐：优雅停止并提示结果
Get-Process trae2api-web | Stop-Process  # 直接结束进程
# 或双击 stop.cmd
```

脚本行为：

- 读取 `.env` 中的 `TW2A_API_KEY`；缺失或仍为占位符（`changeme`）时自动生成随机密钥并写回 `.env`
- 源码有改动或二进制不存在时自动执行 `go build`，否则复用已有二进制
- 端口优先级：`-Port` 参数 > `config.json` 的 `listen` > `7864`；端口被占用时直接报错退出
- 后台模式日志写入 `data/server.log` 与 `data/server.err.log`
- 启动后轮询 `/healthz` 确认可用，失败时打印日志尾部并返回非零退出码

### 首次使用：导入账号

服务启动后账号池为空（`/status` 返回 `{"accounts":[]}`），此时无法调用对话接口，需先导入至少一个 TRAE 账号：

1. 浏览器打开 `http://127.0.0.1:7864/admin`
2. 点击「Web 登录」，用手机号 / 验证码完成登录
3. 浏览器回调至本地后自动回传，服务完成 Token 换取与热加载，无需重启
4. 凭证落盘至 `auths/trae-{uid}.json`，再次执行 `curl.exe -s -H "Authorization: Bearer $env:TW2A_API_KEY" http://127.0.0.1:7864/status` 即可看到账号

> 若 18080 端口被占用，Web 登录会自动降级为「手动粘贴回调链接」模式；也可使用 `./login.sh`（需 bash + python3）。

## Web 管理面板

服务启动后，访问 `http://127.0.0.1:7864/admin` 即可进入可视化管理后台（直接访问根路径 `http://127.0.0.1:7864` 会 302 自动跳转到面板）：

- **账号总览**：实时查看各账号的剩余积分、配额总量、已用积分、权益包数及签到状态。
- **一键签到**：额度监控页顶部「一键签到」按钮，对所有启用账号并发执行签到（token 临期会先自动刷新），随后刷新积分并自动解冻冷却账号，结果按账号逐条展示（新签到 / 已签到 / 失败 / 跳过）。

> **签到提示「当前参与用户太多」（code 9074）**：这是 TRAE 上游在高峰时段的限流，并非本服务故障。上游失败时同样返回 HTTP 200，服务会解析 body 中的 `code` 字段识别失败（不会误报成功），遇到 9074 会自动退避重试一次；仍失败请稍后再试，每日定时任务也会自动补签。

- **模型 & 倍率**：实时列出上游可用模型，含显示名、上下文窗口、倍率与费率等级。上下文窗口取上游 `context_window_tokens`，倍率取 `display_contact_config → consumption_rate.data.rate`（上游真实消耗系数，各模型不同，如 0.08 / 0.2 / 0.77），并标注来源；仅当上游未返回时才回退到 `config.json` 的 `model_rates` 或 `1.0x` 占位。倍率同时展示上游的会员折扣信息（`discount.member_discount` 与 `is_discount_matched`）：折扣生效时按折后价计费并标注「会员 X 折」，未生效时保留原价并标注「（未匹配）」。上游模型表默认缓存 1 小时，页面上的「**重新拉取最新模型**」按钮会绕过缓存立即刷新（对应 `POST /admin/api/models/refresh`，写操作需 Key）。
  **仅展示面向用户的官方模型**：自动过滤两类——① 你在 TRAE 客户端自行添加或接入的模型（`is_custom_model=true` 与 `custom_model_*` 槽位，例如你接的 Gemini / Claude / GPT-5）；② 非面向用户的模型，含上游标记 `is_invisible_to_user=true` 且没有正式展示名的内部代号模型（如 `sagitta` / `aquila`，其 `display_name` 只是占位符 `-`），以及 `browser_use_subagent`、`file_search_agent`、`explore_sub_agent_*`、`summary` 这类子 agent / 摘要模型。面板会分别提示隐藏数量。该过滤同样作用于 `/v1/models`。

  > 仅被标记不可见、但有正式展示名的旧版模型（如 `glm-5`、`DeepSeek-V4-Pro`）会**保留**，避免误伤仍在使用的模型。
- **调用记录**：展示本进程启动以来的调用明细（时间、模型、账号、流式/非流式、token 用量、耗时、失败原因）与汇总（累计调用、成功率、平均耗时、token 合计）。记录仅保存在内存、不含对话正文，重启即清空，最多保留最近 200 条。
- **Web 登录闭环**：在面板点击发起登录，浏览器完成验证后回调自动回传至服务并完成 Token 换取与热加载，无需手动复制凭证。
- **凭证管理**：支持粘贴 JSON 凭证或回调链接直接导入账号，支持随时启停软开关、修改备注昵称或删除失效账号。
- **安全脱敏**：前端展示严格脱敏（仅显示前缀与长度），写操作（导入、删除、修改）均受 `TW2A_API_KEY` 保护。

## API 调用示例

### 对话补全 (Chat Completions)

```bash
curl -X POST http://127.0.0.1:7864/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TW2A_API_KEY}" \
  -d '{
    "model": "glm-5.2",
    "messages": [
      {
        "role": "user",
        "content": "请用简短的一句话介绍你自己。"
      }
    ],
    "stream": false
  }'
```

Windows PowerShell 写法：注意 PowerShell 中的 `curl` 是 `Invoke-WebRequest` 别名，必须写 `curl.exe`；同时 PowerShell 5.1 向 `curl.exe` 传递含双引号的 JSON 时不会自动转义，直接内联 `-d '{"model":...}'` 会因引号被剥离而收到 400（`invalid character 'm' looking for beginning of object key string`）。把 JSON 写进文件再传最稳妥：

```powershell
$body = '{"model":"glm-5.2","messages":[{"role":"user","content":"请用简短的一句话介绍你自己。"}],"stream":false}'
[IO.File]::WriteAllText("$PWD\body.json", $body, (New-Object Text.UTF8Encoding $false))

curl.exe -s -X POST http://127.0.0.1:7864/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $env:TW2A_API_KEY" `
  --data-binary "@body.json"
```

## 运维脚本

项目根目录下提供了便捷的 CLI 运维工具：

```bash
# 全账号批量签到与 Token 保活
./signin.sh

# 账号积分与使用情况报表
./credit.sh

# 输出 JSON 格式报表
./credit.sh -json

# 查看指定 UID 账号
./credit.sh <UID>
```

上述脚本均为 bash 脚本且依赖 `python3`，**Windows 下无法在 PowerShell 直接运行**，请在 Git Bash / WSL 中执行；也可以直接编译为 Windows 可执行文件使用：

```powershell
go build -o signin.exe ./cmd/signin
go build -o credit.exe ./cmd/credit

.\signin.exe                # 遍历 .\auths 批量签到
.\signin.exe auths          # 指定账号目录
.\credit.exe -pretty        # 人类可读报表
.\credit.exe -json          # 原始 JSON
.\credit.exe <UID>          # 指定账号
```

## 配置项参考

所有配置项均可通过环境变量或 `config.json` 进行调整：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TW2A_API_KEY` | (必填) | 服务鉴权密钥，用于 API 调用与控制台写操作 |
| `TW2A_LISTEN` | `:7864` | 主服务监听地址及端口 |
| `TW2A_AUTH_DIR` | `./auths` | 账号凭证存储目录 |
| `TW2A_STATE_FILE` | `./data/state.json` | 账号池状态持久化文件 |
| `TW2A_DEFAULT_MODEL` | `glm-5.2` | 默认回退请求模型 |
| `TW2A_CALLBACK_PORT` | `18080` | 本地 OAuth 回调端口（设为 0 可关闭） |
| `TW2A_TIMEOUT_SECONDS` | `120` | 上游请求超时时长（秒） |
| `TW2A_PLAN_CREDIT` | `12h` | 权益不足（1005）账号的冷却时长 |
| `TW2A_SOFT_RATE` | `60s` | 限流（429 / 404）账号的冷却时长 |
| `TW2A_ERR_THRESHOLD` | `3` | 触发冷却前的连续错误次数 |
| `TW2A_ERR_COOLDOWN` | `10m` | 连续错误达到阈值后的冷却时长 |
| `TW2A_CHECKIN_HOUR` | `9` | 每日自动签到的小时（0-23） |
| `TW2A_CHECKIN_RETRY_MINUTES` | `30` | 签到失败（如 9074 限流）后的自动重试间隔分钟数，0 表示关闭重试 |

> 冷却类配置使用 Go duration 格式（如 `12h`、`60s`、`10m`），与 `config.json` 中的 `cooldown` 字段一一对应。
>
> 模型倍率默认直接展示上游真实值（`rate_source=upstream`）。如需人工兜底，可在 `config.json` 配置 `"model_rates": { "glm-5.2": 1.0 }`——仅在对应模型未被上游提供倍率时才生效（`rate_source=config`）。该值仅用于控制台展示，不影响转发逻辑。

## 安全声明

- **本地存储与脱敏**：所有账号凭证仅保存于本地 `auths/` 目录，控制台及日志中所有 Token 均严格脱敏输出。
- **环境隔离**：凭证文件、状态数据及环境配置文件默认加入 `.gitignore`，防止误提交泄漏。

## 开源协议

本项目基于 [MIT License](LICENSE) 许可发布。
