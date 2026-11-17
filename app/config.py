"""Runtime settings, all from environment variables."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "portal.db"
KEY_PATH = DATA_DIR / "id_ed25519"

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "10"))
SSH_COMMAND_TIMEOUT = int(os.environ.get("SSH_COMMAND_TIMEOUT", "30"))
SSH_USER = os.environ.get("SSH_USER", "root")

BACKUP_KEY_PATH = DATA_DIR / "backup.key"
# Seconds between full config checks. Each check stores a backup if anything
# changed and refreshes what the portal knows for drift detection.
CONFIG_INTERVAL = int(os.environ.get("CONFIG_INTERVAL", "300"))
# Stored backups kept per router. Only changed configs are stored.
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "200"))
