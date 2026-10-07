#!/bin/bash
set -e
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
exec /bin/bash "$ROOT/start-nexus.command" "$@"
