from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
ENV_PATH = ROOT / ".env"
