#Requires -Version 5.1
<#
    traeapi 一键启动脚本（Windows / PowerShell，Python 版）

    用法:
        .\start.ps1                 后台启动（默认，关闭终端不退出）
        .\start.ps1 -Foreground     前台运行（Ctrl+C 停止）
        .\start.ps1 -Restart        先停止已有进程再启动
        .\start.ps1 -Stop           停止服务
        .\start.ps1 -Port 8080      指定监听端口（覆盖 config.json 中的 listen）

    脚本会自动完成:
        1. 定位 Python 解释器（优先 .venv\Scripts\python.exe）
        2. 读取 .env 中的 TW2A_API_KEY；缺失或仍是占位符时自动生成并写回
        3. 按需安装缺失依赖（fastapi / uvicorn / httpx）
        4. 启动服务并轮询 /healthz 确认可用，输出访问地址与密钥
#>
[CmdletBinding()]
param(
    [switch]$Foreground,
    [switch]$Restart,
    [switch]$Stop,
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$DataDir    = Join-Path $PSScriptRoot 'data'
$LogPath    = Join-Path $DataDir 'server.log'
$ErrLogPath = Join-Path $DataDir 'server.err.log'
$PidPath    = Join-Path $DataDir 'server.pid'
$ModuleName = 'traeapi'

function Write-Step($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "[x] $msg" -ForegroundColor Red }

# 定位 Python 解释器：项目内虚拟环境优先，其次 PATH 上的 python / py
function Get-PythonExe {
    $venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    foreach ($candidate in @('python', 'python3', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

# 读取 .env 中的密钥；缺失或为占位符时生成新的并写回文件
function Get-ApiKey {
    $envFile = Join-Path $PSScriptRoot '.env'
    $key = ''
    if (Test-Path -LiteralPath $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*TW2A_API_KEY\s*=\s*(.*)$' | Select-Object -First 1
        if ($m) { $key = $m.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'") }
    }
    if ($key -and $key -ne 'changeme' -and $key -ne 'your_secure_api_key') { return $key }

    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''

    if (Test-Path -LiteralPath $envFile) {
        $lines = @(Get-Content -LiteralPath $envFile)
        if ($lines -match '^\s*TW2A_API_KEY\s*=') {
            $lines = $lines -replace '^\s*TW2A_API_KEY\s*=.*$', "TW2A_API_KEY=$key"
            Set-Content -LiteralPath $envFile -Value $lines -Encoding ASCII
        } else {
            Add-Content -LiteralPath $envFile -Value "TW2A_API_KEY=$key" -Encoding ASCII
        }
    } else {
        Set-Content -LiteralPath $envFile -Value "TW2A_API_KEY=$key" -Encoding ASCII
    }
    Write-Step '已生成新密钥并写入 .env'
    return $key
}

# 端口优先级: -Port 参数 > config.json 的 listen > 7864
function Get-ListenPort {
    if ($Port -gt 0) { return $Port }
    $cfg = Join-Path $PSScriptRoot 'config.json'
    if (Test-Path -LiteralPath $cfg) {
        try {
            $j = Get-Content -Raw -LiteralPath $cfg | ConvertFrom-Json
            if ($j.listen) { return [int]("$($j.listen)" -replace '^.*:', '') }
        } catch { }
    }
    return 7864
}

# 读取已记录的 PID 并判断是否仍在运行
function Get-RunningProcess {
    if (-not (Test-Path -LiteralPath $PidPath)) { return @() }
    $raw = (Get-Content -LiteralPath $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    $procId = 0
    if (-not [int]::TryParse("$raw".Trim(), [ref]$procId)) { return @() }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($proc) { return @($proc) }
    return @()
}

function Stop-Service {
    $procs = Get-RunningProcess
    if ($procs.Count -eq 0) {
        Write-Note '没有正在运行的 traeapi 进程'
        if (Test-Path -LiteralPath $PidPath) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue }
        return
    }
    $ids = ($procs | ForEach-Object { $_.Id }) -join ','
    $procs | Stop-Process -Force
    Start-Sleep -Milliseconds 800
    if (Test-Path -LiteralPath $PidPath) { Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue }
    Write-Ok "已停止 traeapi (PID: $ids)"
}

function Test-PortListening($p) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        return [bool]$conn
    } catch {
        return [bool](netstat -ano | Select-String ":$p\s" | Select-String 'LISTENING')
    }
}

# 确保运行期依赖可用；缺失则用 pip 安装
function Ensure-Dependencies($pythonExe) {
    $probe = 'import fastapi, uvicorn, httpx'
    & $pythonExe -c $probe 2>$null
    if ($LASTEXITCODE -eq 0) { return $true }

    Write-Step '检测到缺少运行依赖，开始安装（fastapi / uvicorn / httpx）...'
    $req = Join-Path $PSScriptRoot 'requirements.txt'
    if (Test-Path -LiteralPath $req) {
        & $pythonExe -m pip install -r $req
    } else {
        & $pythonExe -m pip install fastapi uvicorn httpx
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Fail '依赖安装失败，请手动执行: python -m pip install fastapi uvicorn httpx'
        return $false
    }
    Write-Ok '依赖安装完成'
    return $true
}

# --------------------------- 主流程 ---------------------------

if ($Stop) {
    Stop-Service
    exit 0
}

$running = Get-RunningProcess
if ($running.Count -gt 0) {
    if (-not $Restart) {
        Write-Note "服务已在运行 (PID: $(($running | ForEach-Object { $_.Id }) -join ','))，如需重启请执行 .\start.ps1 -Restart"
        exit 0
    }
    Write-Step '正在停止已有进程...'
    Stop-Service
}

$pythonExe = Get-PythonExe
if (-not $pythonExe) {
    Write-Fail '未检测到 Python，请先安装 Python 3.9+ 并加入 PATH'
    exit 1
}
Write-Step "使用 Python: $pythonExe"

if (-not (Ensure-Dependencies $pythonExe)) { exit 1 }

$key = Get-ApiKey
$env:TW2A_API_KEY = $key
$listenPort = Get-ListenPort
if ($Port -gt 0) { $env:TW2A_LISTEN = ":$Port" }
$env:PYTHONIOENCODING = 'utf-8'

if (Test-PortListening $listenPort) {
    Write-Fail "端口 $listenPort 已被其他程序占用，请先释放该端口或使用 -Port 指定其他端口"
    exit 1
}

if ($Foreground) {
    Write-Ok '前台启动，按 Ctrl+C 停止'
    Write-Host "    接口地址: http://127.0.0.1:$listenPort/v1"
    Write-Host "    管理面板: http://127.0.0.1:$listenPort/admin"
    Write-Host "    鉴权密钥: $key"
    & $pythonExe -m $ModuleName
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
}

Write-Step '正在后台启动服务...'
$proc = Start-Process -FilePath $pythonExe -ArgumentList @('-m', $ModuleName) `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $LogPath -RedirectStandardError $ErrLogPath

Set-Content -LiteralPath $PidPath -Value $proc.Id -Encoding ASCII

$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) { break }
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$listenPort/healthz" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
}

if ($healthy) {
    Write-Ok "启动成功 (PID: $($proc.Id))，健康检查通过"
    Write-Host ''
    Write-Host "    接口地址: http://127.0.0.1:$listenPort/v1"
    Write-Host "    管理面板: http://127.0.0.1:$listenPort/admin"
    Write-Host "    鉴权密钥: $key"
    Write-Host "    运行日志: $ErrLogPath"
    Write-Host ''
    Write-Host '    停止服务: .\start.ps1 -Stop' -ForegroundColor DarkGray
    Write-Host '    重启服务: .\start.ps1 -Restart' -ForegroundColor DarkGray
    Write-Host ''
    Write-Note '下一步: 打开管理面板完成 Web 登录导入账号（当前账号池可能为空）'
} else {
    if ($proc.HasExited) {
        Write-Fail "启动失败，进程已退出（exit code: $($proc.ExitCode)）。日志尾部："
    } else {
        Write-Fail '进程仍在运行但健康检查未通过。日志尾部：'
    }
    foreach ($f in @($ErrLogPath, $LogPath)) {
        if ((Test-Path -LiteralPath $f) -and (Get-Item -LiteralPath $f).Length -gt 0) {
            Write-Host "--- $f ---" -ForegroundColor DarkGray
            Get-Content -LiteralPath $f -Tail 20 | ForEach-Object { Write-Host "    $_" }
        }
    }
    exit 1
}
