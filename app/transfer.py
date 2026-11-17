"""Export and import of the portal's whole state, for moving it to another host.

An export holds the database (routers, backups, settings, users), the SSH key
the routers trust, and the key that decrypts stored backups. Whoever can read
it controls the routers, so it is always encrypted with a passphrase.
"""

import base64
import hashlib
import io
import os
import secrets
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

import asyncssh
from cryptography.fernet import Fernet, InvalidToken

from . import backups, config, crypto, db, router

MAGIC = b"DDWRT-PORTAL-EXPORT-1\n"
MIN_PASSPHRASE = 10
MAX_MEMBER = 1 << 30
NAMES = ("portal.db", "id_ed25519", "backup.key")


class TransferError(Exception):
    """The export or import cannot proceed; the message is safe to show."""


def _cipher(passphrase, salt):
    key = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=2**26, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def check_passphrase(passphrase, again):
    if passphrase != again:
        raise TransferError("The two passphrases do not match.")
    if len(passphrase) < MIN_PASSPHRASE:
        raise TransferError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")


def _snapshot():
    """A consistent copy of the database, without sign-in sessions."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "portal.db"
        source, copy = sqlite3.connect(config.DB_PATH), sqlite3.connect(path)
        try:
            source.backup(copy)
            copy.execute("DELETE FROM sessions")
            copy.commit()
            copy.execute("VACUUM")
        finally:
            source.close()
            copy.close()
        return path.read_bytes()


def export(passphrase):
    """The portal's state as one passphrase-encrypted file."""
    router.portal_key()  # make sure the key file exists
    files = {
        "portal.db": _snapshot(),
        "id_ed25519": config.KEY_PATH.read_bytes(),
        "backup.key": crypto.key(),
    }
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    salt = secrets.token_bytes(16)
    return MAGIC + salt + _cipher(passphrase, salt).encrypt(packed.getvalue())


def unpack(blob, passphrase):
    """Decrypt and check an export. Returns {file name: bytes}; raises TransferError."""
    if not blob.startswith(MAGIC):
        raise TransferError("That file is not a portal export.")
    body = blob[len(MAGIC):]
    try:
        packed = _cipher(passphrase, body[:16]).decrypt(body[16:])
    except InvalidToken:
        raise TransferError("Wrong passphrase, or the file is damaged.") from None
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.name not in NAMES or member.size > MAX_MEMBER:
                    raise TransferError(f"The export contains something unexpected: {member.name!r}.")
                files[member.name] = tar.extractfile(member).read()
    except tarfile.TarError:
        raise TransferError("The export is damaged.") from None
    missing = [n for n in NAMES if n not in files]
    if missing:
        raise TransferError(f"The export is missing {', '.join(missing)}.")
    _check(files)
    return files


def _check(files):
    try:
        asyncssh.import_private_key(files["id_ed25519"])
        Fernet(files["backup.key"].strip())
    except (asyncssh.KeyImportError, ValueError):
        raise TransferError("A key in the export is not valid.") from None
    pinned = os.environ.get("BACKUP_KEY", "").strip().encode()
    if pinned and pinned != files["backup.key"].strip():
        raise TransferError(
            "This portal has BACKUP_KEY set to a different key than the export's, so the imported "
            "backups could not be read. Unset BACKUP_KEY, or set it to the exporting portal's key."
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "portal.db"
        path.write_bytes(files["portal.db"])
        try:
            conn = sqlite3.connect(path)
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            ok, tables = False, set()
    if not ok or not {"devices", "backups", "users"} <= tables:
        raise TransferError("The database in the export is damaged or is not a portal database.")


def install(files, now=None):
    """Replace this portal's state with an unpacked export. Returns where the old state was moved."""
    if backups.busy:
        raise TransferError("A router is in the middle of a change. Try again when it has finished.")
    targets = {"portal.db": config.DB_PATH, "id_ed25519": config.KEY_PATH, "backup.key": config.BACKUP_KEY_PATH}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    staged = {}
    for name, target in targets.items():
        staged[name] = target.with_name(target.name + ".importing")
        staged[name].touch(mode=0o600)
        staged[name].write_bytes(files[name])
    aside = config.DATA_DIR / time.strftime("before-import-%Y%m%d-%H%M%S", time.gmtime(now))
    aside.mkdir(mode=0o700)
    for name, target in targets.items():
        if target.exists():
            target.rename(aside / target.name)
        staged[name].rename(target)
    # Drop everything remembered from the old state, then bring an older export's schema up to date.
    router._portal_key = None
    crypto._fernet = None
    backups.current.clear()
    db.init()
    return aside
