"""Drop every table in the Turso database named in .env.

DANGEROUS: deletes ALL data (users, wallets, trades, settings, everything).
Use this ONLY to clean out tables left behind by an older bot version.

How to use (from the bot folder, same place as bot.py):
    python reset_db.py
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
        tables = db.fetchall_sync(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        if not tables:
            print("No tables found - nothing to drop.")
            return
        names = ", ".join(row["name"] for row in tables)
        answer = input(f"About to DROP these {len(tables)} tables:\n  {names}\nType DELETE to confirm: ")
        if answer.strip() != "DELETE":
            print("Aborted - nothing was dropped.")
            return
        for row in tables:
            db.execute_sync(f'DROP TABLE "{row["name"]}"')
            print("dropped:", row["name"])
    finally:
        db.close()
    print("All tables dropped. Now run: python migrate.py")


if __name__ == "__main__":
    main()
