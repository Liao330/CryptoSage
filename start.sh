#!/bin/bash
# ===========================================================
# CryptoSage 启动脚本
# 用法：
#   ./start.sh            前台启动（Ctrl+C 停止，默认行为，兼容旧用法）
#   ./start.sh start       后台启动（nohup，可关闭终端）
#   ./start.sh restart     重启（先停止已记录的旧进程/占用端口的进程，再后台启动）
#   ./start.sh stop        停止后台运行的服务
#   ./start.sh status      查看服务运行状态
# ===========================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

VENV_DIR="$SCRIPT_DIR/.venv"
PID_DIR="$SCRIPT_DIR/.run"
BACKEND_PID_FILE="$PID_DIR/backend.pid"
FRONTEND_PID_FILE="$PID_DIR/frontend.pid"
BACKEND_LOG="$PID_DIR/backend.log"
FRONTEND_LOG="$PID_DIR/frontend.log"
BACKEND_PORT=8000
FRONTEND_PORT=3000
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"

MODE="${1:-run}"   # run(默认前台) | start(后台) | restart | stop | status

mkdir -p "$PID_DIR"

# ── 工具函数 ──

# 杀掉占用指定端口的所有进程（不依赖 PID 文件，兜底清理外部/上次遗留的进程）
kill_port() {
    local port="$1"
    local pids
    pids=$(lsof -ti ":$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo -e "${YELLOW}  停止占用端口 $port 的进程: $pids${NC}"
        kill -9 $pids 2>/dev/null || true
    fi
}

# 根据 PID 文件停止一个已记录的进程（存在且仍在运行才 kill）
kill_pid_file() {
    local pid_file="$1"
    local label="$2"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo -e "${YELLOW}  停止 $label (PID: $pid)${NC}"
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

do_stop() {
    echo -e "${YELLOW}正在停止 CryptoSage 服务...${NC}"
    kill_pid_file "$BACKEND_PID_FILE" "后端"
    kill_pid_file "$FRONTEND_PID_FILE" "前端"
    # 兜底：即便 PID 文件丢失/失效，也按端口强制清理，确保端口一定被释放
    kill_port "$BACKEND_PORT"
    kill_port "$FRONTEND_PORT"
    echo -e "${GREEN}[✓] 服务已停止${NC}"
}

do_status() {
    local running=false
    if lsof -i ":$BACKEND_PORT" &>/dev/null 2>&1; then
        echo -e "${GREEN}[✓] 后端运行中${NC}  (端口 $BACKEND_PORT)"
        running=true
    else
        echo -e "${RED}[✗] 后端未运行${NC}  (端口 $BACKEND_PORT)"
    fi
    if lsof -i ":$FRONTEND_PORT" &>/dev/null 2>&1; then
        echo -e "${GREEN}[✓] 前端运行中${NC}  (端口 $FRONTEND_PORT)"
        running=true
    else
        echo -e "${RED}[✗] 前端未运行${NC}  (端口 $FRONTEND_PORT)"
    fi
    if [ "$running" = true ]; then
        echo -e "  💚 健康检查: ${BLUE}http://localhost:$BACKEND_PORT/health${NC}"
        echo -e "  🌐 前端: ${BLUE}http://localhost:$FRONTEND_PORT${NC}"
    fi
}

# stop / status 不需要走后面的环境检查与启动流程，直接处理并退出
if [ "$MODE" = "stop" ]; then
    do_stop
    exit 0
fi

if [ "$MODE" = "status" ]; then
    do_status
    exit 0
fi

if [ "$MODE" = "restart" ]; then
    do_stop
    echo ""
    MODE="start"
fi

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}   CryptoSage - Multi-Agent Crypto Analysis${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# 0. 前置检查
echo -e "${YELLOW}[0/5] 前置检查...${NC}"

# Python 版本检查
PYTHON_CMD="python3"
if ! command -v $PYTHON_CMD &>/dev/null; then
    echo -e "${RED}[错误] 未找到 python3${NC}"
    exit 1
fi
PY_VER=$($PYTHON_CMD --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -z "$PY_VER" ] || [ "$(echo "$PY_VER >= 3.10" | bc -l 2>/dev/null || echo 0)" = "0" ]; then
    echo -e "${RED}[错误] Python 版本需 >= 3.10，当前: $PY_VER${NC}"
    exit 1
fi
echo -e "${GREEN}  Python $PY_VER ✓${NC}"

# Node 版本检查
NODE_CMD="node"
if ! command -v $NODE_CMD &>/dev/null; then
    echo -e "${YELLOW}[警告] 未找到 node，前端将不可用${NC}"
fi

# 端口冲突检查：run/start 模式下若端口已被占用，直接报错提示用 restart（不静默清理，
# 避免误杀用户手动在这些端口上跑的其他无关服务）
if lsof -i ":$BACKEND_PORT" &>/dev/null 2>&1; then
    echo -e "${RED}[错误] 端口 $BACKEND_PORT 已被占用${NC}"
    echo -e "  若是本脚本此前启动的旧服务，请使用: ${BLUE}./start.sh restart${NC}"
    exit 1
fi
if command -v lsof &>/dev/null && lsof -i ":$FRONTEND_PORT" &>/dev/null 2>&1; then
    echo -e "${YELLOW}[警告] 端口 $FRONTEND_PORT 已被占用，前端将使用其他端口${NC}"
fi

# .env 检查
if [ ! -f ".env" ]; then
    echo -e "${RED}[错误] 未找到 .env 文件${NC}"
    echo -e "  请复制 .env.example 为 .env 并配置所选 LLM provider 的 API Key"
    exit 1
fi

PROVIDER=$(grep -E '^LLM_PROVIDER=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')
PROVIDER="${PROVIDER:-hy3}"
if [ "$PROVIDER" = "deepseek" ]; then
    HAS_KEY=$(grep -E '^DEEPSEEK_API_KEY=.+' .env 2>/dev/null | grep -v '^#' | grep -v 'sk-your-deepseek-key' | grep -v '^$' || true)
    REQUIRED_KEY="DEEPSEEK_API_KEY"
else
    HAS_KEY=$(grep -E '^HY3_API_KEY=.+' .env 2>/dev/null | grep -v '^#' | grep -v 'your-hy3-api-key' | grep -v '^$' || true)
    REQUIRED_KEY="HY3_API_KEY"
fi
if [ -z "$HAS_KEY" ]; then
    echo -e "${RED}[错误] $REQUIRED_KEY 未配置（LLM_PROVIDER=$PROVIDER）${NC}"
    exit 1
fi
echo -e "${GREEN}[✓] 前置检查通过${NC}"

# 1. Python 虚拟环境
echo -e "${YELLOW}[1/5] 准备 Python 环境...${NC}"
if [ ! -d "$VENV_DIR" ]; then
    $PYTHON_CMD -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# 优先用 requirements.txt，回退逐个安装
if [ -f "requirements.txt" ]; then
    pip install -q -r requirements.txt 2>&1 | tail -1
else
    pip install -q openai langgraph fastapi "uvicorn[standard]" httpx pandas numpy python-dotenv pydantic aiosqlite certifi 2>&1 | tail -1
fi
echo -e "${GREEN}[✓] Python 依赖就绪${NC}"

# 2. 数据库初始化
echo -e "${YELLOW}[2/5] 初始化数据库...${NC}"
PYTHONPATH="$SCRIPT_DIR" $PYTHON_CMD -c "from backend.data.db import init_db; import asyncio; asyncio.run(init_db())" 2>/dev/null || true
echo -e "${GREEN}[✓] 数据库就绪${NC}"

# 3. 前端依赖
echo -e "${YELLOW}[3/5] 检查前端依赖...${NC}"
HAS_FRONTEND=false
if [ -d "frontend" ] && command -v npm &>/dev/null; then
    HAS_FRONTEND=true
    if [ ! -d "frontend/node_modules" ]; then
        cd frontend && npm install --silent 2>&1 | tail -1 && cd ..
    else
        echo -e "${GREEN}[✓] 前端依赖已有${NC}"
    fi
else
    echo -e "${YELLOW}[跳过] 前端不可用${NC}"
fi

# 4. 启动后端
echo -e "${YELLOW}[4/5] 启动后端 API (端口 $BACKEND_PORT)...${NC}"
cd "$SCRIPT_DIR"
source "$VENV_DIR/bin/activate"

if [ "$MODE" = "start" ]; then
    nohup python -m uvicorn backend.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --log-level info \
        > "$BACKEND_LOG" 2>&1 &
    BACKEND_PID=$!
    disown
    echo "$BACKEND_PID" > "$BACKEND_PID_FILE"
else
    python -m uvicorn backend.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" --log-level info &
    BACKEND_PID=$!
fi
sleep 2

if kill -0 $BACKEND_PID 2>/dev/null; then
    echo -e "${GREEN}[✓] 后端已启动 (PID: $BACKEND_PID)${NC}"
else
    echo -e "${RED}[错误] 后端启动失败，请查看日志: $BACKEND_LOG${NC}"
    exit 1
fi

# 5. 启动前端
echo -e "${YELLOW}[5/5] 启动前端...${NC}"
FRONTEND_PID=""
if [ "$HAS_FRONTEND" = true ]; then
    cd "$SCRIPT_DIR/frontend"
    if [ "$MODE" = "start" ]; then
        nohup npx vite --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" > "$FRONTEND_LOG" 2>&1 &
        FRONTEND_PID=$!
        disown
        echo "$FRONTEND_PID" > "$FRONTEND_PID_FILE"
    else
        npx vite --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
        FRONTEND_PID=$!
    fi
    cd "$SCRIPT_DIR"
fi

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}   CryptoSage 启动成功!${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "  🌐 前端: ${BLUE}http://localhost:$FRONTEND_PORT${NC}"
echo -e "  📡 后端: ${BLUE}http://localhost:$BACKEND_PORT${NC}"
echo -e "  💚 健康检查: ${BLUE}http://localhost:$BACKEND_PORT/health${NC}"
echo -e "  📊 API 文档: ${BLUE}http://localhost:$BACKEND_PORT/docs${NC}"
echo ""

if [ "$MODE" = "start" ]; then
    echo -e "  服务已在后台运行（关闭终端不受影响）"
    echo -e "  停止服务: ${BLUE}./start.sh stop${NC}"
    echo -e "  重启服务: ${BLUE}./start.sh restart${NC}"
    echo -e "  查看状态: ${BLUE}./start.sh status${NC}"
    echo -e "  日志文件: ${BLUE}$BACKEND_LOG${NC} / ${BLUE}$FRONTEND_LOG${NC}"
    echo ""
    exit 0
fi

echo -e "  按 Ctrl+C 停止所有服务"
echo ""

cleanup() {
    echo ""
    echo -e "${YELLOW}正在停止服务...${NC}"
    kill $BACKEND_PID 2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill $FRONTEND_PID 2>/dev/null || true
    echo -e "${GREEN}服务已停止${NC}"
    exit 0
}
trap cleanup INT TERM

wait
