#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

for command in python3 git npm; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
PY

[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --quiet --editable '.[dev]'

if [ ! -x .tools/node_modules/.bin/pi ]; then
  npm install --silent --prefix .tools --ignore-scripts @earendil-works/pi-coding-agent@0.84.2
fi

[ -f .env ] || cp .env.example .env
mkdir -p var/worktrees

missing=()
while IFS='=' read -r key value; do
  case "$key" in
    GITHUB_TOKEN|GITHUB_WEBHOOK_SECRET|DASHBOARD_PASSWORD)
      [ -n "$value" ] || missing+=("$key")
      ;;
  esac
done < .env

printf 'Setup complete.\n'
if ((${#missing[@]})); then
  printf 'Set these values in .env before running: %s\n' "${missing[*]}"
fi
printf 'Next: ./tests.sh\n'
