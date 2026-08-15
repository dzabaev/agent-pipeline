#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

[ -x .venv/bin/python ] || {
  echo "Environment missing. Run ./setup.sh first." >&2
  exit 1
}

.venv/bin/python -m unittest discover -s tests/unit -p 'test_*.py' -v
.venv/bin/python -m unittest discover -s tests/integration -p 'test_*.py' -v
