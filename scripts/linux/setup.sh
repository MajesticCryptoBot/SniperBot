#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Install Python 3.11 or newer, then run this script again."
  exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Fill it in before migrating."
fi
mkdir -p logs backups
echo "Next: edit .env"
echo "Then: source .venv/bin/activate && python migrate.py && python bot.py"
