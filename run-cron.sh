#!/usr/bin/env bash
# Nightly (or manual) runner for cron: fixed paths + optional .env for Instapaper credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ENV_FILE="${INSTADD_ENV_FILE:-$ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

exec "$ROOT/.venv/bin/python" "$ROOT/rss_to_instapaper.py" "$@"
