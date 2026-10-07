#!/bin/bash
set -e
ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ -x "$ROOT/nexus/.venv/bin/python" ]]; then
  PYTHON="$ROOT/nexus/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON="$(command -v python3.12)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "Please install Python 3.12 first."
  exit 1
fi
exec "$PYTHON" "$ROOT/run-nexus.py" "$@"
