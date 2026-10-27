#!/usr/bin/env bash
# IT-Copilot 一键开发启动器：网关(8082，provider=openai 直连时跳过) + API(8001) + Vue(5173)。
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_IT_COPILOT_PORT="${IT_COPILOT_PORT-}"
CLI_IT_COPILOT_FRONTEND_PORT="${IT_COPILOT_FRONTEND_PORT-}"
CLI_GATEWAY_PORT="${GATEWAY_PORT-}"

if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env"
  set +a
fi

# .env 提供默认值，但显式传入的环境变量优先级更高。
[[ -n "$CLI_IT_COPILOT_PORT" ]] && IT_COPILOT_PORT="$CLI_IT_COPILOT_PORT"
[[ -n "$CLI_IT_COPILOT_FRONTEND_PORT" ]] && IT_COPILOT_FRONTEND_PORT="$CLI_IT_COPILOT_FRONTEND_PORT"
[[ -n "$CLI_GATEWAY_PORT" ]] && GATEWAY_PORT="$CLI_GATEWAY_PORT"
export IT_COPILOT_PORT="${IT_COPILOT_PORT:-8001}"
export IT_COPILOT_FRONTEND_PORT="${IT_COPILOT_FRONTEND_PORT:-5173}"
export GATEWAY_PORT="${GATEWAY_PORT:-8082}"
export NO_PROXY="127.0.0.1,localhost"; export no_proxy="$NO_PROXY"

# 优先使用项目虚拟环境，其次使用本机已有的工作虚拟环境；绝不优先使用系统 Python。
resolve_python() {
  local candidate
  local candidates=(
    "${IT_COPILOT_PYTHON:-}"
    "$HERE/.venv/bin/python"
    "$HERE/work/venv/bin/python"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return 0
  fi
  echo "未找到 Python 3，请安装 Python 3.14（uv python install 3.14）。" >&2
  return 1
}

PYTHON_BIN="$(resolve_python)"
if [[ "$PYTHON_BIN" == "$(command -v python3 2>/dev/null || true)" && ! -x "$HERE/.venv/bin/python" ]]; then
  echo "警告：未发现项目虚拟环境，将使用系统 Python：$PYTHON_BIN" >&2
fi

# 首次运行时创建项目虚拟环境（优先 uv + Python 3.14）；已有 work/venv 会直接复用。
if [[ "$PYTHON_BIN" == "$(command -v python3 2>/dev/null || true)" && ! -d "$HERE/.venv" ]]; then
  echo "创建项目虚拟环境 $HERE/.venv ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.14 "$HERE/.venv"
  else
    python3 -m venv "$HERE/.venv"
  fi
  PYTHON_BIN="$HERE/.venv/bin/python"
fi

install_requirements() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON_BIN" -r "$1"
  else
    "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$1"
  fi
}

ensure_python_deps() {
  if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn, pydantic_ai, redis, chromadb, dotenv' >/dev/null 2>&1; then
    echo "Python 依赖不完整，使用 $PYTHON_BIN 安装 requirements.txt ..."
    install_requirements "$HERE/requirements.txt"
  fi
}

ensure_redis() {
  command -v docker >/dev/null 2>&1 || { echo "未找到 Docker，无法启动必需的 Redis 容器。" >&2; return 1; }
  docker info >/dev/null 2>&1 || { echo "Docker 未运行，无法启动 it-copilot-redis。" >&2; return 1; }
  local container="${IT_COPILOT_REDIS_CONTAINER:-it-copilot-redis}"
  local image="${IT_COPILOT_REDIS_IMAGE:-redis:7-alpine}"
  local password="${REDIS_PASSWORD:-it-copilot123}"
  if docker container inspect "$container" >/dev/null 2>&1; then
    local state
    state="$(docker inspect -f '{{.State.Running}}' "$container")"
    [[ "$state" == "true" ]] || { echo "启动 Redis 容器 $container ..."; docker start "$container" >/dev/null; }
  else
    echo "创建 Redis 容器 $container ..."
    docker run -d --name "$container" --restart unless-stopped \
      -p 127.0.0.1:6379:6379 \
      -v it-copilot-redis-data:/data \
      "$image" redis-server --appendonly yes --requirepass "$password" >/dev/null
  fi
  local i
  for i in {1..20}; do
    if docker exec "$container" redis-cli -a "$password" --no-auth-warning PING 2>/dev/null | grep -qx PONG; then
      echo "Redis 已就绪：$container (127.0.0.1:6379)"
      return 0
    fi
    sleep 0.5
  done
  echo "Redis 容器已启动但认证/健康检查失败：$container。请检查 REDIS_PASSWORD 与容器启动命令。" >&2
  return 1
}

free_port() {
  local port="$1" pids pid
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  echo "清理端口 $port 上的旧进程：$pids"
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  sleep 0.5
  for pid in $pids; do kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true; done
}

ensure_python_deps
ensure_redis
free_port "$GATEWAY_PORT"
free_port "$IT_COPILOT_PORT"
free_port "$IT_COPILOT_FRONTEND_PORT"

export IT_COPILOT_PYTHON="$PYTHON_BIN"
export PYTHONPATH="$HERE"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:${GATEWAY_PORT}}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-sk-local}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-deepseek-chat}"
export REDIS_URL="${REDIS_URL:-redis://:it-copilot123@127.0.0.1:6379/0}"
export CHROMA_PERSIST_DIRECTORY="${CHROMA_PERSIST_DIRECTORY:-$HERE/data/chroma}"
export IT_COPILOT_SKILLS_DIR="${IT_COPILOT_SKILLS_DIR:-$HERE/skills}"
mkdir -p "$HERE/logs" "$CHROMA_PERSIST_DIRECTORY"

GATEWAY_PID=""; API_PID=""; FRONTEND_PID=""
cleanup() {
  trap - INT TERM EXIT
  for pid in "$FRONTEND_PID" "$API_PID" "$GATEWAY_PID"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

LLM_PROVIDER="$(printf '%s' "${IT_COPILOT_LLM_PROVIDER:-anthropic}" | tr '[:upper:]' '[:lower:]')"
if [[ "$LLM_PROVIDER" == "openai" ]]; then
  [[ -n "${OPENAI_BASE_URL:-}" && -n "${OPENAI_API_KEY:-}" ]] || { echo "错误：IT_COPILOT_LLM_PROVIDER=openai 需要在 .env 中配置 OPENAI_BASE_URL 和 OPENAI_API_KEY。" >&2; exit 1; }
  echo "LLM provider=openai：PydanticAI 直连 ${OPENAI_BASE_URL}，跳过 8082 网关。"
elif [[ -f "$HERE/gateway/.env.gateway" ]]; then
  :
else
  echo "错误：未找到 gateway/.env.gateway。请先复制 gateway/.env.gateway.example 并填写模型配置。" >&2
  echo "网关未配置时不会启动 API；详细说明见 $HERE/logs/gateway.log（如已存在）。" >&2
  exit 1
fi

if [[ "$LLM_PROVIDER" != "openai" ]]; then
IT_COPILOT_PYTHON="$PYTHON_BIN" "$HERE/gateway/run_gateway.sh" >"$HERE/logs/gateway.log" 2>&1 & GATEWAY_PID=$!

gateway_ready() {
  local health
  [[ -n "$GATEWAY_PID" ]] || return 1
  kill -0 "$GATEWAY_PID" 2>/dev/null || return 1
  health="$($PYTHON_BIN - "$GATEWAY_PORT" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as response:
        body = json.load(response)
    print("ready" if body.get("status") == "ok" and body.get("key_configured") is True else "not-ready")
except Exception:
    print("not-ready")
PY
  )"
  [[ "$health" == "ready" ]]
}

echo "等待 Anthropic→OpenAI 网关就绪（127.0.0.1:${GATEWAY_PORT}）..."
GATEWAY_READY=false
for _ in {1..20}; do
  if gateway_ready; then GATEWAY_READY=true; break; fi
  sleep 1
done
if [[ "$GATEWAY_READY" != true ]]; then
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "错误：网关进程已退出，API 不会启动。" >&2
  else
    echo "错误：网关在等待窗口内未通过健康检查，或 key_configured=false；API 不会启动。" >&2
  fi
  echo "请检查 gateway/.env.gateway、8082 端口和上游配置；详细日志：$HERE/logs/gateway.log" >&2
  exit 1
fi
echo "网关已就绪：status=ok，key_configured=true。"
fi

IT_COPILOT_PYTHON="$PYTHON_BIN" "$HERE/run_local.sh" >"$HERE/logs/api.log" 2>&1 & API_PID=$!

NPM_CACHE="$HERE/.npm-cache"
mkdir -p "$NPM_CACHE"
chmod -R u+rwX "$NPM_CACHE" 2>/dev/null || true
export npm_config_cache="$NPM_CACHE"
cd "$HERE/frontend"
if [[ ! -x "$HERE/frontend/node_modules/.bin/vite" ]]; then
  echo "前端依赖不完整，使用项目缓存 $NPM_CACHE 安装..."
  npm cache clean --force --cache "$NPM_CACHE" >/dev/null 2>&1 || true
  npm ci --no-audit --no-fund
fi

npm run dev -- --host 127.0.0.1 --port "$IT_COPILOT_FRONTEND_PORT" >"$HERE/logs/frontend.log" 2>&1 & FRONTEND_PID=$!

echo "IT-Copilot 已启动："
echo "  前端: http://127.0.0.1:${IT_COPILOT_FRONTEND_PORT}"
echo "  API:  http://127.0.0.1:${IT_COPILOT_PORT}"
echo "  日志: $HERE/logs/{gateway,api,frontend}.log"
wait "$API_PID"
