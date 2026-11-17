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

FIRMWARE_URL = os.environ.get("FIRMWARE_URL", "https://download1.dd-wrt.com/dd-wrtv2/downloads/betas").rstrip("/")
# Hours between checks of the download site for a newer build.
FIRMWARE_CHECK_HOURS = float(os.environ.get("FIRMWARE_CHECK_HOURS", "12"))
# Days between the running build and the one being installed before the portal warns.
FIRMWARE_MAX_JUMP_DAYS = int(os.environ.get("FIRMWARE_MAX_JUMP_DAYS", "62"))
# Seconds allowed for the router to write an image to flash, and to boot it.
FIRMWARE_WRITE_TIMEOUT = int(os.environ.get("FIRMWARE_WRITE_TIMEOUT", "900"))
FIRMWARE_BOOT_TIMEOUT = int(os.environ.get("FIRMWARE_BOOT_TIMEOUT", "600"))

# Seconds a router must be silent before an offline alert opens.
ALERT_OFFLINE_AFTER = int(os.environ.get("ALERT_OFFLINE_AFTER", "300"))
# Optional URL that receives {"text": "..."} whenever an alert opens or clears.
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK", "")

# Hours a sign-in lasts.
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "168"))
# The user created with a new database. Its password must be changed at first sign-in.
DEFAULT_USER = os.environ.get("DEFAULT_USER", "admin")
DEFAULT_PASSWORD = os.environ.get("DEFAULT_PASSWORD", "admin")
