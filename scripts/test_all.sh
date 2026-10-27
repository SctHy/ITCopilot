#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
pytest
(
  cd frontend
  npm test
  npm run build
)
echo "ALL TESTS PASSED: backend pytest + frontend node tests + production build"
