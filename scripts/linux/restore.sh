#!/usr/bin/env bash
# Restore a Turso CLI dump into an EMPTY database.
# Usage: scripts/linux/restore.sh backups/turso-....sql
set -euo pipefail
cd "$(dirname "$0")/../.."

DUMP="${1:-}"
if [[ -z "${DUMP}" || ! -f "${DUMP}" ]]; then
  echo "Usage: scripts/linux/restore.sh backups/<dump>.sql"
  exit 1
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | sed 's/\r$//')
  set +a
fi

DB_NAME="${TURSO_DB_NAME:-}"
if [[ -z "${DB_NAME}" ]]; then
  echo "Set TURSO_DB_NAME in .env."
  exit 1
fi
if ! command -v turso >/dev/null 2>&1; then
  echo "Install the Turso CLI and run: turso auth login"
  exit 1
fi

echo "This replays SQL into '${DB_NAME}'."
echo "Restoring into a database that already has tables can fail on CREATE or duplicate keys."
echo "Prefer a new empty database: turso db create <new-name>"
echo "Then put that name in TURSO_DB_NAME, create a new token, and update TURSO_DATABASE_URL."
read -r -p "Type the database name to continue: " CONFIRM
if [[ "${CONFIRM}" != "${DB_NAME}" ]]; then
  echo "Aborted."
  exit 1
fi
turso db shell "${DB_NAME}" < "${DUMP}"
echo "Restore command finished. Run: python migrate.py"
echo "The master key in .env must be the same key that encrypted the wallets."
