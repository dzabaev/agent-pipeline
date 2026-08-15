#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

[ -x .venv/bin/uvicorn ] || {
  echo "Environment missing. Run ./setup.sh first." >&2
  exit 1
}
[ -f .env ] || {
  echo ".env missing. Run ./setup.sh first." >&2
  exit 1
}

export PATH="$ROOT/.tools/node_modules/.bin:$PATH"
set -a
# shellcheck disable=SC1091
source .env
set +a

exec .venv/bin/uvicorn --factory agent_pipeline.main:create_app --host 127.0.0.1 --port "${PORT:-8000}" --workers 1
