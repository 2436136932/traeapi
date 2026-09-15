<div align="center">

**把 TRAE SOLO 对话通道包装成 OpenAI 兼容 API，自带 Web 管理面板**

</div>

---

## 一、启动服务

### 方式 1：Windows 一键脚本（推荐）

双击 `start.cmd` 即可；也可在 PowerShell 中执行：

```powershell
.\start.ps1                 # 后台启动（默认，关闭终端不退出）
.\start.ps1 -Foreground     # 前台运行，Ctrl+C 停止
.\start.ps1 -Restart        # 停止后重新启动
.\start.ps1 -Stop           # 停止服务（也可双击 stop.cmd）
.\start.ps1 -Port 8080      # 指定监听端口
```

脚本会自动完成：读取或生成 `.env` 中的密钥 → 按需编译 → 启动 → 健康检查，最后打印访问地址与密钥。后台模式日志在 `data/server.log` 与 `data/server.err.log`。

### 方式 2：Docker Compose

```bash
mkdir -p auths data
cp .env.example .env     # 编辑 .env，把 TW2A_API_KEY 改成你自己的密钥
docker compose up -d --build
```

### 方式 3：本地直接运行

环境要求：Go 1.22+

```bash
# Linux / macOS
export TW2A_API_KEY="your_secure_api_key"
go build -o trae2api-web ./cmd/server
./trae2api-web
```

```powershell
# Windows PowerShell
$env:TW2A_API_KEY = "your_secure_api_key"
go build -o trae2api-web.exe ./cmd/server
.\trae2api-web.exe
```

> 本地直接运行**不会读取 `.env`**，密钥必须用环境变量传入（`.env` 只供 `docker compose` 取值）。`config.json` 与 `auths/` 目录不存在也能正常启动，会使用内置默认配置。

启动后访问 <http://127.0.0.1:7864>，会自动跳转到管理面板 `/admin`。

## 二、导入账号

账号池为空时无法调用对话接口，需先导入至少一个 TRAE 账号。

1. 打开 <http://127.0.0.1:7864/admin>
2. 在「账号管理」页点击「添加账号（TRAE 登录）」
3. 用手机号 / 验证码完成登录；回调会自动回传，服务完成 Token 换取并热加载，**无需重启**
4. 凭证落盘在 `auths/trae-{uid}.json`

如果 18080 端口被占用，Web 登录会自动降级为「手动粘贴回调链接」模式。也可以直接在「导入凭证」框粘贴回调链接或凭证 JSON，或用命令行 `./login.sh`（需 bash + python3）。

## 三、调用 API

密钥就是启动时用的 `TW2A_API_KEY`（脚本启动后也会打印出来）。

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

Windows PowerShell 下有两个坑：`curl` 是 `Invoke-WebRequest` 的别名，必须写 `curl.exe`；且 PowerShell 会把内联 JSON 的引号吃掉导致 400，建议把 JSON 写入文件再传：

```powershell
$body = '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":false}'
[IO.File]::WriteAllText("$PWD\body.json", $body, (New-Object Text.UTF8Encoding $false))

curl.exe -s -X POST http://127.0.0.1:7864/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $env:TW2A_API_KEY" `
  --data-binary "@body.json"
```

客户端接入（NextChat / Chatbox / Cline / Claude Code 等）：

| 配置项 | 值 |
|---|---|
| 接口地址 | `http://127.0.0.1:7864/v1` |
| API Key | 你的 `TW2A_API_KEY` |
| 模型 | 「模型」页列出的模型 ID，如 `glm-5.2` |

其他端点：

| 端点 | 说明 |
|---|---|
| `GET /v1/models` | 模型列表（需鉴权） |
| `GET /status` | 账号池状态（需鉴权） |
| `GET /healthz` | 健康检查（无需鉴权） |

## 四、管理面板

<http://127.0.0.1:7864/admin>，四个标签页。**写操作（导入、删除、启停、签到、刷新）需要先在顶部填入 API Key 并保存。**

| 标签页 | 用途 |
|---|---|
| 账号管理 | 查看账号状态 / 积分 / Token 有效期，启停开关、刷新 token、删除、查看脱敏凭证 |
| 额度监控 | 各账号积分卡片，以及「一键签到」按钮 |
| 模型 | 官方模型列表、上下文窗口、倍率与会员折扣，可强制重新拉取 |
| 调用记录 | 本进程启动以来的调用明细与汇总 |

使用要点：

- **一键签到**：对所有启用账号并发签到；token 临期会先自动刷新，签到后自动解冻冷却账号，结果逐条展示（新签到 / 已签到 / 失败 / 跳过）。
- **签到失败提示 `当前参与用户太多`（code 9074）**：这是 TRAE 上游高峰时段的限流，不是本服务故障，等上游放行即可。服务会自动处理——定时任务每天 9 点签到，失败后每 30 分钟重试（最多 12 次），服务重启时也会补签一次。
- **模型过滤**：只展示面向用户的官方模型。你在 TRAE 客户端自行添加的模型（`is_custom_model` / `custom_model_*` 槽位）以及 `browser_use_subagent`、`summary` 等内部功能模型会被自动隐藏，面板会提示隐藏数量。该过滤同样作用于 `/v1/models`。
- **倍率**：直接取上游真实值，并展示会员折扣（折扣生效按折后价计费并标注「会员 X 折」，未生效时标注「未匹配」，按原价计费）。
- **模型列表缓存 1 小时**，点「重新拉取最新模型」可立即刷新。
- **调用记录只存内存**：不含对话正文，重启即清空，最多保留最近 200 条。

## 五、配置

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

冷却类配置使用 Go duration 格式（`12h`、`60s`、`10m`）。另有只能写在 `config.json` 的 `model_rates`：人工兜底倍率，仅在对应模型没有上游倍率时才生效。

## 六、运维脚本

`signin.sh`（批量签到保活）、`credit.sh`（积分报表）、`login.sh`（命令行登录取凭证）都是 bash 脚本，需在 Git Bash / WSL 中运行。Windows 下也可直接编译使用：

```powershell
go build -o signin.exe ./cmd/signin
go build -o credit.exe ./cmd/credit

.\signin.exe                # 遍历 .\auths 批量签到
.\signin.exe auths          # 指定账号目录
.\credit.exe -pretty        # 人类可读报表
.\credit.exe -json          # 原始 JSON
.\credit.exe <UID>          # 指定账号
```

## 七、常见问题

**调用返回 401**
没有设置 `TW2A_API_KEY`，或请求头里的密钥不对。注意本地运行时密钥只能用环境变量传入，不会读 `.env`。

**签到一直是「当前参与用户太多」（9074）**
上游高峰限流，不是本地问题，换请求头或加快重试都没用。服务会自动重试（每 30 分钟、最多 12 次），也可以稍后手动再点。

**面板里少了一些模型**
自定义模型与内部功能模型会被自动隐藏，面板顶部会显示隐藏数量。

**接口地址填成了 `http://127.0.0.1:7864`**
根路径会 302 跳转到管理面板，API 在 `/v1` 下，请填 `http://127.0.0.1:7864/v1`。

**PowerShell 里 curl 报 JSON 解析错误**
用 `curl.exe` 并把 JSON 写入文件后 `--data-binary "@body.json"`，见「三、调用 API」。

## License

[MIT](LICENSE)
