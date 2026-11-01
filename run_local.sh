#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_IT_COPILOT_PORT="${IT_COPILOT_PORT-}"
CLI_OPENAI_BASE_URL="${OPENAI_BASE_URL-}"
CLI_OPENAI_MODEL="${OPENAI_MODEL-}"
if [[ -f "$HERE/.env" ]]; then set -a; source "$HERE/.env"; set +a; fi
[[ -n "$CLI_IT_COPILOT_PORT" ]] && IT_COPILOT_PORT="$CLI_IT_COPILOT_PORT"
[[ -n "$CLI_OPENAI_BASE_URL" ]] && OPENAI_BASE_URL="$CLI_OPENAI_BASE_URL"
[[ -n "$CLI_OPENAI_MODEL" ]] && OPENAI_MODEL="$CLI_OPENAI_MODEL"
PYTHON_BIN="${IT_COPILOT_PYTHON:-}"
for candidate in "$HERE/.venv/bin/python" "$HERE/work/venv/bin/python"; do
  if [[ -z "$PYTHON_BIN" && -x "$candidate" ]]; then PYTHON_BIN="$candidate"; break; fi
done
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"
export PYTHONPATH="$HERE"
export APP_ENV="${APP_ENV:-development}"
export IT_COPILOT_PORT="${IT_COPILOT_PORT:-8001}"
export NO_PROXY="127.0.0.1,localhost"; export no_proxy="$NO_PROXY"
export REDIS_URL="${REDIS_URL:-redis://:it-copilot123@127.0.0.1:6379/0}"
export IT_COPILOT_DATA_DIR="${IT_COPILOT_DATA_DIR:-$HERE/data}"
export IT_COPILOT_SKILLS_DIR="${IT_COPILOT_SKILLS_DIR:-$HERE/skills}"
mkdir -p "$IT_COPILOT_DATA_DIR"
if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS="$(lsof -nP -tiTCP:"$IT_COPILOT_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$OLD_PIDS" ]]; then
    echo "清理端口 $IT_COPILOT_PORT 上的旧进程：$OLD_PIDS"
    for pid in $OLD_PIDS; do kill "$pid" 2>/dev/null || true; done
    sleep 0.5
  fi
fi
echo "IT-Copilot API: http://127.0.0.1:${IT_COPILOT_PORT}"
exec "$PYTHON_BIN" -m uvicorn api.main:app --host 127.0.0.1 --port "$IT_COPILOT_PORT"
