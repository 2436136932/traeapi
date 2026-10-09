<div align="center">

# traeapi

**把 TRAE SOLO 对话通道包装成 OpenAI 兼容 API，自带 Web 管理面板**

Python 实现 · 流式 / 非流式 · 多账号池 · 一键签到 · 定时任务

</div>

---

## 目录

- [这是什么](#这是什么)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [导入账号](#导入账号)
- [调用 API](#调用-api)
- [管理面板](#管理面板)
- [配置](#配置)
- [运维脚本](#运维脚本)
- [测试](#测试)
- [项目结构](#项目结构)
- [从 Go 版迁移](#从-go-版迁移)
- [常见问题](#常见问题)

## 这是什么

TRAE 官方的 SOLO 对话通道（`llm_utils_chat`）不是 OpenAI 格式，无法直接被 NextChat / Chatbox / Cline / Claude Code 这类客户端使用。本项目把这条通道包装成 **OpenAI 兼容 API**，并提供一套 Web 管理面板来管理账号池。

核心能力：

| 能力 | 说明 |
|---|---|
| **OpenAI 兼容接口** | `/v1/chat/completions`（流式 + 非流式）、`/v1/models`、`/status`、`/healthz` |
| **协议双向转换** | OpenAI 请求体 → SOLO 请求体；SOLO 自定义 SSE → OpenAI SSE / `chat.completion` |
| **多账号池** | 按剩余积分挑号、请求级自动轮换、三类冷却 + session 失效硬禁用 |
| **Web 管理面板** | 账号管理 / 额度监控 / 模型 / 调用记录 四个标签页 |
| **Web 登录闭环** | 面板点一下即可用手机号登录导入账号，回调自动回传，无需重启 |
| **一键签到** | 全账号并发签到（含上游风控 9074 的自动重试）、签到后自动解冻冷却账号 |
| **定时任务** | 每日自动签到（失败按间隔重试）+ token 预刷新 |
| **在线改密钥** | 面板可直接修改 API 密钥，立即生效并持久化，无需改配置重启 |
| **工具调用** | 完整支持 OpenAI `tools` / `tool_choice` 与多轮 tool 结果回传 |
| **运维 CLI** | 批量签到、积分报表 |

## 环境要求

- **Python 3.9+**（开发验证于 3.13）
- 依赖：`fastapi`、`uvicorn`、`httpx`

```bash
python -m pip install -r requirements.txt
```

## 快速开始

### Windows 一键脚本（推荐）

双击 `start.cmd` 即可；也可在 PowerShell 中执行：

```powershell
.\start.ps1                 # 后台启动（默认，关闭终端不退出）
.\start.ps1 -Foreground     # 前台运行，Ctrl+C 停止
.\start.ps1 -Restart        # 停止后重新启动
.\start.ps1 -Stop           # 停止服务（也可双击 stop.cmd）
.\start.ps1 -Port 8080      # 指定监听端口
```

脚本会自动完成：定位 Python（优先 `.venv\Scripts\python.exe`）→ 按需安装依赖 → 读取或生成 `.env` 中的密钥 → 启动 → 健康检查，最后打印访问地址与密钥。

### Linux / macOS / Git Bash / WSL

```bash
./start.sh          # 后台启动
./start.sh -f       # 前台运行
./start.sh -r       # 重启
./start.sh -s       # 停止
./start.sh -p 8080  # 指定端口
```

### 直接运行

```bash
# Linux / macOS
export TW2A_API_KEY="your_secure_api_key"
python -m traeapi

# Windows PowerShell
$env:TW2A_API_KEY = "your_secure_api_key"
python -m traeapi

# 指定配置文件
python -m traeapi -config my.json
```

> **注意**：直接运行**不会读取 `.env`**，密钥必须用环境变量传入（`.env` 是给 `start.ps1` / `start.sh` 读的）。`config.json` 与 `auths/` 目录不存在也能正常启动，会使用内置默认配置。

启动后访问 <http://127.0.0.1:7864>，会自动跳转到管理面板 `/admin`。

后台运行时的文件位置：

| 文件 | 说明 |
|---|---|
| `data/server.log` | 标准输出日志 |
| `data/server.err.log` | 标准错误日志（服务日志主要在这里） |
| `data/server.pid` | 进程 PID，供 `-Stop` / `-Restart` 使用 |

## 导入账号

账号池为空时无法调用对话接口，需先导入至少一个 TRAE 账号。

### 方式一：Web 登录（推荐）

1. 打开 <http://127.0.0.1:7864/admin>
2. 在「账号管理」页点击「添加账号（TRAE 登录）」
3. 用手机号 / 验证码完成登录
4. 回调会自动回传到本地，服务完成 Token 换取并热加载，**无需重启**
5. 凭证落盘在 `auths/trae-{uid}.json`

如果 18080 端口被占用，Web 登录会自动降级为「手动粘贴回调链接」模式 —— 把浏览器地址栏的完整回调链接粘贴到「导入凭证」框即可。

### 方式二：粘贴凭证

在「导入凭证」框粘贴以下任一形式：

- TRAE 登录回调链接：`http://127.0.0.1:18080/authorize?refreshToken=...`
- 嵌套形 JSON：`{"account":{...},"auth":{...}}`
- 扁平形 JSON：`{"accessToken":"...","uid":"...","machineId":"...","deviceId":"..."}`

## 调用 API

密钥就是启动时用的 `TW2A_API_KEY`（脚本启动后会打印出来）。

接口地址：`http://127.0.0.1:7864/v1`

```bash
curl -X POST http://127.0.0.1:7864/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TW2A_API_KEY}" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

> **Windows PowerShell 有两个坑**：`curl` 是 `Invoke-WebRequest` 的别名，必须写 `curl.exe`；且 PowerShell 会把内联 JSON 的引号吃掉导致 400。建议把 JSON 写入文件再传：
>
> ```powershell
> $body = '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":false}'
> [IO.File]::WriteAllText("$PWD\body.json", $body, (New-Object Text.UTF8Encoding $false))
>
> curl.exe -s -X POST http://127.0.0.1:7864/v1/chat/completions `
>   -H "Content-Type: application/json" `
>   -H "Authorization: Bearer $env:TW2A_API_KEY" `
>   --data-binary "@body.json"
> ```

### 客户端接入

| 配置项 | 值 |
|---|---|
| 接口地址 | `http://127.0.0.1:7864/v1` |
| API Key | 你的 `TW2A_API_KEY` |
| 模型 | 「模型」页列出的模型 ID，如 `glm-5.2` |

### 端点一览

| 端点 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/v1/chat/completions` | POST | 需 Key | 对话（流式 / 非流式） |
| `/v1/models` | GET | 需 Key | 模型列表 |
| `/status` | GET | 需 Key | 账号池状态 |
| `/healthz` | GET | 免 Key | 健康检查 |

### 支持的特性

- **流式**：`stream: true` 返回标准 OpenAI SSE，含 `reasoning_content`（思考链）增量与 `usage`，末尾保证有 `data: [DONE]`
- **工具调用**：`tools` / `tool_choice`（`auto` / `required` / `none` / 指定函数），支持多轮 `assistant.tool_calls` + `tool` 结果回传
- **思考链**：上游 `reasoning_content` 原样透出
- **模型别名**：`auto` 或留空 → 默认模型；`glm-5.2__dev` 这类内部名 → 自动映射回 `glm-5.2`；`deepseek_v4_pro` 这类下划线写法 → 宽松匹配 `DeepSeek-V4-Pro`
- **多模态**：`content` 为数组时原样透传

## 管理面板

<http://127.0.0.1:7864/admin>，四个标签页。

> **写操作（导入、删除、启停、签到、刷新模型）需要先在顶部填入 API Key 并保存。**

| 标签页 | 用途 |
|---|---|
| 账号管理 | 查看账号状态 / 积分 / Token 有效期，启停开关、刷新 token、删除、查看脱敏凭证 |
| 额度监控 | 各账号积分卡片，以及「一键签到」按钮、积分池明细 |
| 模型 | 对话通道（SOLO function）切换、官方模型列表、上下文窗口、倍率与会员折扣，可强制重新拉取 |
| 调用记录 | 本进程启动以来的调用明细与汇总 |

### 在线修改密钥

点顶部工具条的 **「修改服务端密钥」**：

1. 输入新密钥（≥6 位、不能含空格），或点「随机生成」生成 48 位随机密钥
2. 点「保存新密钥」→ **立即生效、无需重启**，本页会自动记住新密钥

密钥持久化在 `data/admin_key.json`。优先级：

```
data/admin_key.json  >  TW2A_API_KEY 环境变量  >  空（不鉴权）
```

即**面板设过的密钥优先于 `.env`**。想回到 `.env` 里的密钥，点「清除密钥」或删除 `data/admin_key.json` 后重启即可。

**忘记密钥了怎么办**：删除 `data/admin_key.json` 并重启服务 → 回到 `.env` 的 `TW2A_API_KEY`；若 `.env` 也没配，服务不再鉴权，此时可直接在面板重设。

> **安全提醒**：「清除密钥（不再鉴权）」会让**任何能访问该端口的人**都能增删账号，仅建议本机自用。对外暴露时请务必设置密钥。

### 使用要点

- **一键签到**：对所有启用账号并发签到（并发上限 4）；token 临期会先自动刷新，签到后自动解冻冷却账号，结果逐条展示（新签到 / 已签到 / 失败 / 跳过）。

- **签到失败提示「当前参与用户太多」（code 9074）**：这是上游对请求头 `x-device-id`（设备号）**取值**的风控判定，与签到时段无关。实测同一账号当天**首次**签到：传 uid 能过，传登录流程自生成的 32 位 GUID / 随机 16 位数字会被拒（账号当天已签到后 claim 幂等返回成功，不再校验设备号）。服务现在按「uid → 账号 deviceId → 派生值」顺序重试并退避，正常不会复现；万一某个账号仍失败，定时任务每天 9 点签到、失败后每 30 分钟重试（最多 12 次），服务重启时也会补签一次。

- **模型过滤**：只展示面向用户的官方模型。你在 TRAE 客户端自行添加的模型（`is_custom_model` / `custom_model_*` 槽位）以及 `browser_use_subagent`、`summary` 等内部功能模型会被自动隐藏，面板会提示隐藏数量。该过滤同样作用于 `/v1/models`。

- **面板列表与 TRAE 客户端不一致**：上游标记「不可见」的旧版模型（`glm-5`、`glm-5-turbo`、`DeepSeek-V4-Pro` 非正式版等）默认**保留** —— 它们技术上仍可调用，但客户端不展示，因此面板会比客户端多几项；把 `hide_invisible_models` 设为 `true` 即可一并隐藏。

- **倍率**：直接取上游真实值，并展示会员折扣（折扣生效按折后价计费并标注「会员 X 折」，未生效时标注「未匹配」，按原价计费）。注意：倍率只是上游展示系数，**不是计费公式** —— 实测实际扣费按输入/输出分别计价（如 glm-5.2 约 输入 120 / 输出 756 积分每百万 token，kimi-k3 约 800 / 4000），模型之间与倍率不成正比；真实消耗以「积分池明细 / 额度监控」的前后差值为准。

- **模型列表缓存 1 小时**，点「重新拉取最新模型」可立即刷新。

- **调用记录只存内存**：不含对话正文，重启即清空，最多保留最近 200 条。

- **积分池明细**：额度监控页会按包列出积分（标签「通用」/「Work 专属」、限额、已用、剩余）；**同类型包合并为一行**（如「每日签到 ×5」显示合计），点「明细」可展开逐包数据。TRAE 的额度按包顺序扣费 —— **通用积分用完后才会动用 Work 专属积分**，所以看哪个包的「已用」在涨就知道当前扣的是它；点两次「刷新积分池」，被扣的那个包会标注「较上次 -X」。

## 配置

所有配置项都可用环境变量，或写在 `config.json`（模板见 `config.example.json`）。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TW2A_API_KEY` | (必填) | 服务鉴权密钥，API 调用与控制台写操作都用它 |
| `TW2A_LISTEN` | `:7864` | 监听地址及端口 |
| `TW2A_AUTH_DIR` | `./auths` | 账号凭证存储目录 |
| `TW2A_STATE_FILE` | `./data/state.json` | 账号池状态文件 |
| `TW2A_DEFAULT_MODEL` | `glm-5.2` | 未指定模型时的默认模型 |
| `TW2A_CALLBACK_PORT` | `18080` | 本地 OAuth 回调端口（0 = 关闭） |
| `TW2A_TIMEOUT_SECONDS` | `120` | 上游请求超时（秒） |
| `TW2A_PLAN_CREDIT` | `12h` | 权益不足（1005）账号的冷却时长 |
| `TW2A_SOFT_RATE` | `60s` | 限流（429 / 404）账号的冷却时长 |
| `TW2A_ERR_THRESHOLD` | `3` | 触发冷却前的连续错误次数 |
| `TW2A_ERR_COOLDOWN` | `10m` | 连续错误达到阈值后的冷却时长 |
| `TW2A_CHECKIN_HOUR` | `9` | 每日自动签到的小时（0-23） |
| `TW2A_CHECKIN_RETRY_MINUTES` | `30` | 签到失败后的重试间隔（分钟，0 = 关闭重试） |
| `TW2A_HIDE_INVISIBLE_MODELS` | `false` | 设为 `true` 时连同「上游标记不可见」的旧版模型一起隐藏 |
| `TW2A_FUNCTION` | `solo_work_lite` | 对话通道，见下表；面板可热切换，重启后回到此配置 |

冷却类配置使用 Go duration 格式（`12h`、`60s`、`10m`，也支持 `1h30m`、`1.5h`、`500ms`）。

另有只能写在 `config.json` 的 `model_rates`：人工兜底倍率，仅在对应模型没有上游倍率时才生效。

### 对话通道（SOLO function）

四个通道取值实测自 TraeWork 客户端，四者走同一端点 `/api/agent/v3/llm_utils_chat`，返回的 SSE 结构完全一致：

| 通道 | 说明 |
|---|---|
| `solo_work_lite` | 工作场景 · 轻量本地处理（默认，最稳） |
| `solo_work_remote` | 工作场景 · 云端 agent 处理 |
| `solo_design_lite` | 设计场景 · 轻量本地处理 |
| `solo_design_remote` | 设计场景 · 云端 agent 处理 |

## 运维脚本

```bash
python cmd/signin.py            # 遍历 ./auths 批量签到，打印逐账号结果表
python cmd/signin.py auths      # 指定账号目录

python cmd/credit.py            # 积分报表（原始 JSON）
python cmd/credit.py -pretty    # 人类可读日报
python cmd/credit.py <UID>      # 指定账号
python cmd/credit.py -pretty <UID>
```

## 测试

```bash
python -m pytest tests/ -q
```

248 个用例，全部基于 mock 上游（不消耗真实积分）：

| 测试文件 | 用例 | 覆盖内容 |
|---|---|---|
| `test_upstream.py` | 77 | 错误分类、请求头与设备号策略、请求体改写、SSE 解析与转换、倍率解析、签到重试、Token 刷新、模型表 |
| `test_checkin_login_scheduler.py` | 64 | 一键签到、登录回调解析、登录 URL 构造、Web 登录闭环、调度器、配置加载与 duration 解析 |
| `test_server_api.py` | 41 | 对话聚合与流式、账号轮换与冷却、鉴权、模型过滤与倍率、额度与积分池接口 |
| `test_auth_pool.py` | 39 | 凭证解析与原子落盘、账号池冷却状态机、state.json 兼容与并发安全 |
| `test_api_key.py` | 27 | 面板在线密钥管理（改密钥立即生效、持久化、优先级回退、清除后免鉴权） |

## 项目结构

```
traeapi/
├── traeapi/
│   ├── __main__.py            # 入口：加载配置 → 构建账号池 → 起两个 HTTP 服务
│   ├── config.py              # config.json + TW2A_* 环境变量（含 Go duration 解析）
│   ├── auth.py                # 凭证解析 / 原子落盘 / 目录扫描
│   ├── pool.py                # 账号池：挑号 + 冷却状态机 + state.json
│   ├── scheduler.py           # 定时签到与 token 预刷新
│   ├── apikey.py              # 面板可自定义的密钥存储
│   ├── jsonutil.py            # JSON 编解码工具
│   ├── upstream/              # SOLO 上游协议适配
│   │   ├── constants.py       #   主机 / 端点 / function 白名单
│   │   ├── headers.py         #   三类请求头 + 签到设备号策略
│   │   ├── payload.py         #   OpenAI 请求体 → SOLO 请求体
│   │   ├── sse.py             #   SOLO SSE 解析 / 聚合 / 流式转换
│   │   └── client.py          #   上游 HTTP 客户端 + 错误分类
│   └── server/                # HTTP 层
│       ├── app.py             #   FastAPI 路由与鉴权
│       ├── chat.py            #   /v1/chat/completions 挑号轮换状态机
│       ├── models.py          #   模型表缓存 / 过滤 / 映射
│       ├── admin.py           #   面板页面 + 额度监控
│       ├── admin_ext.py       #   模型 / 调用记录 / 对话通道
│       ├── accounts.py        #   账号 CRUD 与导入
│       ├── checkin.py         #   一键签到
│       ├── callback.py        #   回调链接解析 / 登录 URL 构造
│       ├── login.py           #   Web 登录闭环
│       ├── keys.py            #   在线密钥管理
│       ├── stats.py           #   调用记录环形缓冲
│       ├── state.py           #   运行时状态容器
│       └── templates/admin.html
├── cmd/{signin.py,credit.py}  # 运维 CLI
├── tests/                     # pytest 用例
├── start.ps1 / start.cmd / stop.cmd / start.sh
├── config.example.json
└── requirements.txt
```

## 从 Go 版迁移

本项目的配置文件与数据文件与 Go 版**双向兼容**，可直接迁移：

| 项 | 兼容性 |
|---|---|
| `config.json` | 键名完全一致，可直接复制 |
| `TW2A_*` 环境变量 | 名称完全一致 |
| `auths/trae-{uid}.json` | 格式一致（嵌套形），可直接复制 |
| `data/state.json` | 格式一致（含 Go 的 `0001-01-01T00:00:00Z` 零值时间） |

迁移步骤：

```bash
# 1. 复制账号凭证（也可直接用面板重新登录导入）
cp traeapi-web-main/auths/*.json trae2api/auths/

# 2. 复制配置（可选）
cp traeapi-web-main/config.json trae2api/config.json

# 3. 启动
./start.sh    # 或 .\start.ps1
```

### 与 Go 版的差异

功能等价，以下为实现层面的差异：

| 项 | Go 版 | Python 版 |
|---|---|---|
| **密钥来源** | 只能环境变量 `TW2A_API_KEY`，改密钥须重启 | 面板可在线修改（`data/admin_key.json` 优先于 env），立即生效 |
| 模型缓存 | 包级全局变量 | 每个 `ServerState` 独立持有 |
| 并发模型 | goroutine + `sync.RWMutex` | 线程池 + `threading.RLock` |
| JSON 字段顺序 | `map` 按字典序 | 按插入序（语义相同） |
| 中文昵称乱码 | `login.sh` 里有修复，Go 版漏了 | **已补上** |
| `login.sh` | 提供 | 未移植（Web 面板已覆盖） |

新增的端点（Go 版没有）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/admin/api/key` | GET | 当前密钥状态（只读、脱敏，永不返回明文） |
| `/admin/api/key` | POST | 在线修改密钥（需当前 Bearer） |
| `/admin/api/key` | DELETE | 清除密钥 → 服务不再鉴权（需当前 Bearer） |

## 常见问题

**调用返回 401**
没有设置 `TW2A_API_KEY`，或请求头里的密钥不对。注意直接运行（`python -m traeapi`）时密钥只能用环境变量传入，不会读 `.env`（`.env` 是给 `start.ps1` / `start.sh` 读的）。若你在面板里改过密钥，则 `data/admin_key.json` 优先。

**忘了面板密钥**
删除 `data/admin_key.json` 并重启服务，即可回到 `.env` 里的 `TW2A_API_KEY`；若 `.env` 也没配，服务将不再鉴权，此时可在面板重新设置。

**签到提示「当前参与用户太多」（9074）**
上游 `checkin_credits/claim` 的风控拒绝，**不是高峰限流**：账号当天首次签到时，成败取决于请求头 `x-device-id` 的取值 —— 实测传 **uid** 能通过，传登录流程自生成的 32 位十六进制 GUID、随机 16 位数字、派生值都会被判为 9074；完全不传该头则是 9004「order parameters are incorrect」。

有个坑要注意：账号**当天已签到后** claim 会幂等返回成功且不再校验设备号，所以拿已签到账号反复试设备号会「全都成功」，不可据此下结论。

服务现在按 `uid → 账号 deviceId → 派生值` 依次重试（首选值先退避重试一次覆盖瞬时挤兑），正常不会复现；若某账号仍报 9074，说明它被上游风控限制，等定时任务重试（每 30 分钟、最多 12 次）或稍后手动再点。想给某账号指定设备号：把 `auths/trae-<uid>.json` 里 `auth.deviceId` 写成一个 16 位数字即可（会被优先采用）。

**面板里少了一些模型**
自定义模型与内部功能模型会被自动隐藏，面板顶部会显示隐藏数量。若客户端能看到、面板却始终没有（典型如 `GLM-5.3-Flash`、`DeepSeek-V4.1-Flash`、`Kimi-K2.8-Preview`、`Qwen3.8-Flash`），是当前账号的上游模型表（`get_detail_param`）未下发这些模型：已实测，改 `function` 参数、`X-Ide-Version` 版本号或 `X-App-Version-Code` 版本码，上游返回的都是同一批模型，故这类模型在 API 通道上暂不可用。

**面板里多了一些模型**
那是上游标记「不可见」的旧版模型（如 `glm-5`、`glm-5-turbo`、`DeepSeek-V4-Pro`），默认保留且仍可调用；把 `hide_invisible_models` 设为 `true` 即可隐藏，与客户端展示对齐。

**接口地址填成了 `http://127.0.0.1:7864`**
根路径会 302 跳转到管理面板，API 在 `/v1` 下，请填 `http://127.0.0.1:7864/v1`。

**PowerShell 里 curl 报 JSON 解析错误**
用 `curl.exe` 并把 JSON 写入文件后 `--data-binary "@body.json"`，见[调用 API](#调用-api)。

**中文日志 / CLI 输出乱码**
服务启动时会自动把 stdout/stderr 切到 UTF-8。若在旧版 Windows 控制台手动运行 CLI 仍见乱码，先设 `$env:PYTHONIOENCODING='utf-8'`。

**端口 18080 被占用**
不影响主服务：Web 登录会降级为「手动粘贴回调链接」模式，面板上粘贴回调链接即可导入。也可用 `TW2A_CALLBACK_PORT=0` 显式关闭回调服务。

## License

[MIT](LICENSE)
