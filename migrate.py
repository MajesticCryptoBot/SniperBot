"""Apply schema.sql to the Turso database named in .env.

Usage (from the project directory, after the virtualenv is active):
    python migrate.py
"""

from sniper.config import Config
from sniper.db import Database
from sniper.errors import ConfigError


def main() -> None:
    try:
        cfg = Config.from_env("migrate")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    db = Database.connect(cfg)
    try:
        count = db.apply_schema()
        tables = db.fetchall_sync("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        names = ", ".join(row["name"] for row in tables)
        print(f"Applied {count} schema statements.")
        print(f"Tables: {names}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
