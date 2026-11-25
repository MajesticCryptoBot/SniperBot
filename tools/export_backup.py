"""Export Turso rows to a local JSON file, including encrypted wallet blobs.

The file is secret: with MASTER_ENCRYPTION_KEY it can be turned back into private keys.
This does not replace the Turso CLI dump, but it works with only the values in .env.

Usage:
    python tools/export_backup.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from sniper.config import Config
from sniper.db import Database
from sniper.errors import ConfigError
from sniper.paths import BACKUP_DIR

TABLES = (
    "users",
    "settings",
    "trades",
    "positions",
    "position_ledger",
    "snipe_watch",
    "bot_state",
)


def main() -> None:
    try:
        cfg = Config.from_env("export")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    db = Database.connect(cfg)
    try:
        payload = {"exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tables": {}}
        for table in TABLES:
            payload["tables"][table] = db.fetchall_sync(f"SELECT * FROM {table}")
    finally:
        db.close()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = payload["exported_at"].replace(":", "").replace("-", "")
    path = BACKUP_DIR / f"turso-export-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {path}")
    print("This file contains encrypted private keys. Keep it with the master key, offline.")


if __name__ == "__main__":
    main()
