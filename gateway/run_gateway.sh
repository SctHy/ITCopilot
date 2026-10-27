#!/usr/bin/env bash
# 启动 Anthropic -> OpenAI-compatible 协议转换网关。
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/.." && pwd)"
CLI_GATEWAY_PORT="${GATEWAY_PORT-}"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi
if [[ -f "$HERE/.env.gateway" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env.gateway"
  set +a
fi
[[ -n "$CLI_GATEWAY_PORT" ]] && export GATEWAY_PORT="$CLI_GATEWAY_PORT"

resolve_python() {
  local candidate
  local candidates=(
    "${IT_COPILOT_PYTHON:-}"
    "$PROJECT_ROOT/.venv/bin/python"
    "$PROJECT_ROOT/work/venv/bin/python"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  done
  if command -v python3 >/dev/null 2>&1; then printf '%s\n' "$(command -v python3)"; return 0; fi
  echo "未找到 Python 3，请安装 Python 3.14（uv python install 3.14）。" >&2; return 1
}

install_requirements() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON_BIN" -r "$1"
  else
    "$PYTHON_BIN" -m pip install --disable-pip-version-check -r "$1"
  fi
}

PYTHON_BIN="$(resolve_python)"
if ! "$PYTHON_BIN" -c 'import fastapi, uvicorn, httpx' >/dev/null 2>&1; then
  echo "网关依赖不完整，使用 $PYTHON_BIN 安装项目依赖..."
  install_requirements "$PROJECT_ROOT/requirements.txt"
fi

cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" "$HERE/anthropic_to_openai.py"
