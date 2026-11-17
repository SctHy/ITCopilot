#!/bin/sh
set -eu

# Escape the endpoint for the quoted HTML attribute before rendering the template.
PYTHON_API_URL=$(printf '%s' "${PYTHON_API_URL:-/api}" | sed 's/&/\&amp;/g; s/"/\&quot;/g; s/</\&lt;/g; s/>/\&gt;/g')
export PYTHON_API_URL
envsubst '$PYTHON_API_URL' < /etc/nginx/index.html.template > /usr/share/nginx/html/index.html

exec nginx -g "daemon off;"
