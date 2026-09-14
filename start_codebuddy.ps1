# CryptoSage x CodeBuddy AI Key 一键启动
# 前置：.env 中已填入 CODEBUDDY_API_KEY（https://copilot.tencent.com/profile/ 创建）
# 用法：powershell -File start_codebuddy.ps1          # 启动
#       powershell -File start_codebuddy.ps1 -Action stop
param(
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Stop-All {
    # 1) 杀掉 8000/8360 端口的实际监听进程（uvicorn --reload 会产生孤儿 worker，
    #    仅按命令行匹配父进程会漏杀，必须按端口定位）
    foreach ($port in 8000, 8360) {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            Write-Host "停止端口 $port 监听进程 PID $($c.OwningProcess)"
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
    # 2) 兜底：按命令行匹配父进程
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match "codebuddy_llm_proxy|backend\.main|uvicorn.*backend\.main"
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

if ($Action -eq "stop") { Stop-All; Write-Host "已全部停止"; exit 0 }

# 从 .env 读 CODEBUDDY_API_KEY
$apiKey = $null
if (Test-Path "$Root\.env") {
    foreach ($line in Get-Content "$Root\.env" -Encoding UTF8) {
        if ($line -match '^\s*CODEBUDDY_API_KEY\s*=\s*(.+)\s*$') {
            $apiKey = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $apiKey) {
    Write-Host "[ERROR] .env 中未配置 CODEBUDDY_API_KEY" -ForegroundColor Red
    Write-Host "请到 https://copilot.tencent.com/profile/ 创建 AI Key 后填入 .env"
    exit 1
}
$env:CODEBUDDY_API_KEY = $apiKey
$env:CODEBUDDY_INTERNET_ENVIRONMENT = "internal"
Write-Host "[OK] CODEBUDDY_API_KEY 已加载（长度 $($apiKey.Length)）"

Stop-All

# 1. CodeBuddy LLM Proxy（OpenAI 兼容 → CodeBuddy Agent SDK）
Write-Host "[1/2] 启动 CodeBuddy LLM Proxy (127.0.0.1:8360)..."
Start-Process -FilePath "python" -ArgumentList "codebuddy_llm_proxy.py" `
    -WorkingDirectory $Root -WindowStyle Minimized
Start-Sleep -Seconds 5
Write-Host "      proxy: $((Invoke-WebRequest -Uri 'http://127.0.0.1:8360/health' -UseBasicParsing -TimeoutSec 5).Content)"

# 2. 后端（不用 --reload：reload 模式的 worker 子进程被杀后易成孤儿占用端口）
Write-Host "[2/2] 启动 CryptoSage 后端 (127.0.0.1:8000)..."
Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","backend.main:app","--host","127.0.0.1","--port","8000" `
    -WorkingDirectory $Root -WindowStyle Minimized
Start-Sleep -Seconds 10
Write-Host "      backend: $((Invoke-WebRequest -Uri 'http://127.0.0.1:8000/ready' -UseBasicParsing -TimeoutSec 10).Content)"

Write-Host ""
Write-Host "完成！测试一次分析（全程约 15-30 分钟，CodeBuddy CLI 子进程链路较慢）："
Write-Host '  curl -X POST http://127.0.0.1:8000/api/analyze -H "Content-Type: application/json" -d "{\"symbol\":\"BTC-USDT\",\"query\":\"BTC 现在适合进场吗\"}"'
Write-Host "前端（可选）：cd frontend; npm install; npm run dev  → http://localhost:3000"
Write-Host "停止：powershell -File start_codebuddy.ps1 -Action stop"
