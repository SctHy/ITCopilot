#!/bin/sh
set -eu

cat >/usr/share/nginx/html/runtime-config.js <<CONFIG
window.__IT_COPILOT_CONFIG__ = {
  pythonApiUrl: "${PYTHON_API_URL:-/api}"
};
CONFIG

exec nginx -g "daemon off;"
