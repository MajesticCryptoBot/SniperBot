#!/usr/bin/env bash
# Dump the Turso database with the Turso CLI. No Docker.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p backups

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a
  # Do not echo values. Only TURSO_DB_NAME is used.
  # shellcheck disable=SC1090
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | sed 's/\r$//')
  set +a
fi

DB_NAME="${TURSO_DB_NAME:-}"
if [[ -z "${DB_NAME}" ]]; then
  echo "Set TURSO_DB_NAME in .env (the database name, not the libsql URL)."
  exit 1
fi
if ! command -v turso >/dev/null 2>&1; then
  echo "Install the Turso CLI: https://docs.turso.tech/cli/installation"
  echo "Then: turso auth login"
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/turso-${DB_NAME}-${STAMP}.sql"
turso db shell "${DB_NAME}" ".dump" > "${OUT}"
echo "Wrote ${OUT}"
echo "Also available without the CLI: python tools/export_backup.py"
echo "Backup files contain encrypted private keys. Protect them."
