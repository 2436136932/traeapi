#!/usr/bin/env bash
# traeapi 一键启动脚本（Linux / macOS / Git Bash / WSL）
#
# 用法:
#   ./start.sh                 后台启动（默认）
#   ./start.sh -f              前台运行（Ctrl+C 停止）
#   ./start.sh -r              先停止已有进程再启动
#   ./start.sh -s              停止服务
#   ./start.sh -p 8080         指定监听端口
set -euo pipefail
cd "$(dirname "$0")"

DATA_DIR="data"
LOG_PATH="$DATA_DIR/server.log"
ERR_LOG_PATH="$DATA_DIR/server.err.log"
PID_PATH="$DATA_DIR/server.pid"
MODULE="traeapi"

FOREGROUND=0
RESTART=0
STOP=0
PORT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--foreground) FOREGROUND=1 ;;
        -r|--restart)    RESTART=1 ;;
        -s|--stop)       STOP=1 ;;
        -p|--port)       PORT="${2:-0}"; shift ;;
        *) echo "[!] 未知参数: $1" ;;
    esac
    shift
done

step() { echo "[*] $1"; }
ok()   { echo "[+] $1"; }
note() { echo "[!] $1"; }
fail() { echo "[x] $1"; }

# 定位 Python 解释器：项目内虚拟环境优先
find_python() {
    if [[ -x ".venv/bin/python" ]]; then echo ".venv/bin/python"; return; fi
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return; fi
    done
    echo ""
}

running_pids() {
    [[ -f "$PID_PATH" ]] || return 0
    local pid
    pid="$(head -n1 "$PID_PATH" | tr -d '[:space:]')"
    [[ -n "$pid" ]] || return 0
    if kill -0 "$pid" 2>/dev/null; then echo "$pid"; fi
}

# 反查占用指定端口的监听进程 PID（PID 文件缺失时的兜底）。
port_owner_pids() {
    local port="$1" pids=""
    if command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
    elif command -v ss >/dev/null 2>&1; then
        pids="$(ss -lptnH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 || true)"
    elif command -v netstat >/dev/null 2>&1; then
        pids="$(netstat -lptn 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $7}' | cut -d/ -f1 || true)"
    fi
    echo "$pids" | tr '\n' ' '
}

# 推断监听端口：-p 参数 > config.json 的 listen > 7864。
# stop_service 在 -s 场景下需要它（那时还没有走到 LISTEN_PORT 的计算）。
detect_listen_port() {
    if [[ "${PORT:-0}" -gt 0 ]]; then echo "$PORT"; return; fi
    local listen=""
    if [[ -f "config.json" ]]; then
        listen="$("${PY:-python3}" -c '
import json, sys
try:
    with open("config.json", encoding="utf-8") as fh:
        print(str(json.load(fh).get("listen") or ""))
except Exception:
    print("")
' 2>/dev/null || true)"
    fi
    if [[ -n "$listen" ]]; then echo "${listen##*:}"; else echo 7864; fi
}

# 判断 PID 是否像 traeapi 服务（命令行含 traeapi）。只清理确认属于本服务的进程。
is_traeapi_pid() {
    local pid="$1"
    [[ -n "$pid" ]] || return 1
    if [[ -r "/proc/$pid/cmdline" ]]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q traeapi && return 0
        return 1
    fi
    # macOS / 其他平台：用 ps 取命令行
    ps -p "$pid" -o command= 2>/dev/null | grep -q traeapi
}

stop_service() {
    local pids
    pids="$(running_pids || true)"
    if [[ -n "$pids" ]]; then
        kill $pids 2>/dev/null || true
        sleep 1
        for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
        ok "已停止 traeapi (PID: $pids)"
    else
        note "没有通过 PID 文件登记的 traeapi 进程"
    fi
    rm -f "$PID_PATH"

    # 兜底：PID 文件可能缺失（进程被强杀 / 以脚本之外的方式启动），
    # 按端口反查并清理，确保 -s 真的能把服务停干净。
    local port owners
    port="${LISTEN_PORT:-}"
    [[ -n "$port" ]] || port="$(detect_listen_port)"
    owners="$(port_owner_pids "$port")"
    for pid in $owners; do
        if is_traeapi_pid "$pid"; then
            kill -9 "$pid" 2>/dev/null || true
            ok "已停止未登记的 traeapi 实例 (PID: $pid，占用端口 $port)"
        else
            note "端口 $port 被非 traeapi 进程占用 (PID: $pid)，未处理"
        fi
    done
}

if [[ "$STOP" == "1" ]]; then
    stop_service
    exit 0
fi

if [[ -n "$(running_pids || true)" ]]; then
    if [[ "$RESTART" != "1" ]]; then
        note "服务已在运行 (PID: $(running_pids | tr '\n' ',' ))，如需重启请执行 ./start.sh -r"
        exit 0
    fi
    step "正在停止已有进程..."
    stop_service
fi

PY="$(find_python)"
if [[ -z "$PY" ]]; then
    fail "未检测到 Python，请先安装 Python 3.9+"
    exit 1
fi
step "使用 Python: $PY"

if ! "$PY" -c 'import fastapi, uvicorn, httpx' >/dev/null 2>&1; then
    step "检测到缺少运行依赖，开始安装..."
    "$PY" -m pip install -r requirements.txt
fi

# 读取或生成 .env 中的密钥
ENV_FILE=".env"
API_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    API_KEY="$(grep -E '^[[:space:]]*TW2A_API_KEY[[:space:]]*=' "$ENV_FILE" | head -n1 | sed -E 's/^[^=]*=[[:space:]]*//' | tr -d '"'"'"'')"
fi
if [[ -z "$API_KEY" || "$API_KEY" == "changeme" || "$API_KEY" == "your_secure_api_key" ]]; then
    API_KEY="$("$PY" -c 'import secrets;print(secrets.token_hex(24))')"
    if [[ -f "$ENV_FILE" ]] && grep -qE '^[[:space:]]*TW2A_API_KEY[[:space:]]*=' "$ENV_FILE"; then
        "$PY" - "$ENV_FILE" "$API_KEY" <<'PYEOF'
import re, sys
path, key = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    text = fh.read()
text = re.sub(r"(?m)^\s*TW2A_API_KEY\s*=.*$", f"TW2A_API_KEY={key}", text)
with open(path, "w", encoding="utf-8") as fh:
    fh.write(text)
PYEOF
    else
        printf 'TW2A_API_KEY=%s\n' "$API_KEY" >> "$ENV_FILE"
    fi
    step "已生成新密钥并写入 .env"
fi

export TW2A_API_KEY="$API_KEY"
export PYTHONIOENCODING=utf-8

LISTEN_PORT="$("$PY" - "$PORT" <<'PYEOF'
import json, os, sys
port = int(sys.argv[1])
if port > 0:
    print(port); raise SystemExit
listen = ""
if os.path.exists("config.json"):
    try:
        with open("config.json", encoding="utf-8") as fh:
            listen = str(json.load(fh).get("listen") or "")
    except Exception:
        listen = ""
if listen:
    print(listen.rsplit(":", 1)[-1])
else:
    print(7864)
PYEOF
)"

if [[ "$PORT" -gt 0 ]]; then export TW2A_LISTEN=":$PORT"; fi

# 端口被占：先尝试清理未登记的 traeapi 孤儿实例（PID 文件缺失时靠端口反查），
# 只有确认是别的程序占用才报错退出。
OWNERS="$(port_owner_pids "$LISTEN_PORT")"
if [[ -n "${OWNERS// /}" ]]; then
    note "端口 $LISTEN_PORT 已被占用，正在检查占用者..."
    BLOCKED=0
    for pid in $OWNERS; do
        if is_traeapi_pid "$pid"; then
            kill -9 "$pid" 2>/dev/null || true
            sleep 0.4
            ok "已清理未登记的 traeapi 实例 (PID: $pid)"
        else
            fail "端口 $LISTEN_PORT 被非 traeapi 进程占用 (PID: $pid)"
            BLOCKED=1
        fi
    done
    if [[ "$BLOCKED" == "1" ]]; then
        fail "请先释放该端口或使用 -p 指定其他端口"
        exit 1
    fi
fi

if [[ "$FOREGROUND" == "1" ]]; then
    ok "前台启动，按 Ctrl+C 停止"
    echo "    接口地址: http://127.0.0.1:${LISTEN_PORT}/v1"
    echo "    管理面板: http://127.0.0.1:${LISTEN_PORT}/admin"
    echo "    鉴权密钥: ${API_KEY}"
    exec "$PY" -m "$MODULE"
fi

mkdir -p "$DATA_DIR"
step "正在后台启动服务..."
nohup "$PY" -m "$MODULE" >"$LOG_PATH" 2>"$ERR_LOG_PATH" &
echo $! > "$PID_PATH"

HEALTHY=0
for _ in $(seq 1 30); do
    sleep 0.5
    if "$PY" - "$LISTEN_PORT" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
    sys.exit(0 if resp.status == 200 else 1)
PYEOF
    then HEALTHY=1; break; fi
done

if [[ "$HEALTHY" == "1" ]]; then
    ok "启动成功 (PID: $(cat "$PID_PATH"))，健康检查通过"
    echo ""
    echo "    接口地址: http://127.0.0.1:${LISTEN_PORT}/v1"
    echo "    管理面板: http://127.0.0.1:${LISTEN_PORT}/admin"
    echo "    鉴权密钥: ${API_KEY}"
    echo "    运行日志: ${ERR_LOG_PATH}"
    echo ""
    echo "    停止服务: ./start.sh -s"
    echo "    重启服务: ./start.sh -r"
    echo ""
    note "下一步: 打开管理面板完成 Web 登录导入账号（当前账号池可能为空）"
else
    fail "启动失败或健康检查未通过。日志尾部："
    for f in "$ERR_LOG_PATH" "$LOG_PATH"; do
        if [[ -s "$f" ]]; then
            echo "--- $f ---"
            tail -n 20 "$f"
        fi
    done
    exit 1
fi
