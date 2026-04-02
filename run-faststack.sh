#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_EXE="$REPO_ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON_EXE" ]]; then
  echo "Could not find venv Python at: $PYTHON_EXE" >&2
  exit 1
fi

cd "$REPO_ROOT"
exec "$PYTHON_EXE" -m faststack.app "$@"
