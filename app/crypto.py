"""Encryption for stored backups, which contain Wi-Fi and admin credentials.

The key comes from BACKUP_KEY when set (a Kubernetes Secret, say). Otherwise
one is generated next to the database, which keeps backups unreadable in a
copy of the database alone but not from someone holding the whole volume.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet = None


def _load():
    global _fernet
    if _fernet is None:
        key = os.environ.get("BACKUP_KEY", "").strip()
        if not key:
            if not config.BACKUP_KEY_PATH.exists():
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                config.BACKUP_KEY_PATH.touch(mode=0o600)
                config.BACKUP_KEY_PATH.write_bytes(Fernet.generate_key())
            key = config.BACKUP_KEY_PATH.read_text().strip()
        _fernet = Fernet(key)
    return _fernet


def key():
    """The key in use, as stored in backup.key."""
    _load()
    pinned = os.environ.get("BACKUP_KEY", "").strip()
    return pinned.encode() if pinned else config.BACKUP_KEY_PATH.read_bytes().strip()


def encrypt(data):
    return _load().encrypt(data)


def decrypt(token):
    try:
        return _load().decrypt(token)
    except InvalidToken:
        raise ValueError("backup cannot be decrypted with the current BACKUP_KEY") from None
