#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

[ -x .venv/bin/uvicorn ] || {
  echo "Environment missing. Run ./setup.sh first." >&2
  exit 1
}
export PATH="$ROOT/.tools/node_modules/.bin:$PATH"
CONFIG_FILE=${ENV_FILE:-$ROOT/.env}
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a
elif [ -z "${GITHUB_REPOSITORY:-}" ]; then
  echo ".env or exported application settings required. Run ./setup.sh first." >&2
  exit 1
fi

exec .venv/bin/uvicorn --factory agent_pipeline.main:create_app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --workers 1
