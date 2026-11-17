"""SQLite storage. One row per enrolled router, holding its latest status."""

import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    host        TEXT NOT NULL UNIQUE,
    port        INTEGER NOT NULL DEFAULT 22,
    host_key    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    enrolled_at REAL NOT NULL,
    last_poll   REAL,
    last_ok     REAL,
    error       TEXT,
    status      TEXT,
    mapped      INTEGER NOT NULL DEFAULT 1   -- 0 until the settings mapping is confirmed
);
CREATE TABLE IF NOT EXISTS keymap (
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    portal_key TEXT NOT NULL,     -- a portal key, or 'radio:2.4' / 'radio:5'
    device_key TEXT NOT NULL,     -- this router's nvram key, or a radio prefix
    PRIMARY KEY (device_id, portal_key)
);
CREATE TABLE IF NOT EXISTS backups (
    id           INTEGER PRIMARY KEY,
    device_id    INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    taken_at     REAL NOT NULL,
    confirmed_at REAL NOT NULL,   -- last time the router still had exactly this config
    reason       TEXT NOT NULL,   -- scheduled | manual | enrolled | pre-change
    name         TEXT,
    model        TEXT,
    build        INTEGER,
    sha256       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    settings     INTEGER NOT NULL,
    changes      INTEGER,         -- settings changed since the previous backup
    blob         BLOB NOT NULL    -- encrypted nvram backup file
);
CREATE INDEX IF NOT EXISTS backups_device ON backups (device_id, taken_at);
CREATE TABLE IF NOT EXISTS managed (
    scope TEXT NOT NULL,          -- 'fleet' or 'device:<id>'
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    at        REAL NOT NULL,
    ok        INTEGER NOT NULL,
    message   TEXT NOT NULL
);
"""

BACKUP_COLUMNS = "id, device_id, taken_at, confirmed_at, reason, name, model, build, sha256, size, settings, changes"


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        if "mapped" not in columns:  # databases created before settings mapping existed
            conn.execute("ALTER TABLE devices ADD COLUMN mapped INTEGER NOT NULL DEFAULT 1")


def _device(row):
    dev = dict(row)
    dev["status"] = json.loads(dev["status"]) if dev["status"] else None
    return dev


def list_devices():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY host").fetchall()
    return [_device(r) for r in rows]


def get_device(device_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _device(row) if row else None


def upsert_device(host, port, host_key, fingerprint):
    with connect() as conn:
        conn.execute(
            """INSERT INTO devices (host, port, host_key, fingerprint, enrolled_at, mapped)
               VALUES (?, ?, ?, ?, ?, 0)
               ON CONFLICT(host) DO UPDATE SET
                   port = excluded.port,
                   host_key = excluded.host_key,
                   fingerprint = excluded.fingerprint,
                   enrolled_at = excluded.enrolled_at""",
            (host, port, host_key, fingerprint, time.time()),
        )
        return conn.execute("SELECT id FROM devices WHERE host = ?", (host,)).fetchone()["id"]


def record_poll(device_id, status=None, error=None):
    now = time.time()
    with connect() as conn:
        if error is None:
            conn.execute(
                "UPDATE devices SET last_poll = ?, last_ok = ?, error = NULL, status = ? WHERE id = ?",
                (now, now, json.dumps(status), device_id),
            )
        else:
            conn.execute(
                "UPDATE devices SET last_poll = ?, error = ? WHERE id = ?",
                (now, error, device_id),
            )


def delete_device(device_id):
    with connect() as conn:
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        conn.execute("DELETE FROM managed WHERE scope = ?", (f"device:{device_id}",))


# --- backups ---------------------------------------------------------------


def list_backups(device_id):
    """Backup metadata (no file contents), newest first."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {BACKUP_COLUMNS} FROM backups WHERE device_id = ? ORDER BY taken_at DESC, id DESC",
            (device_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_backup(device_id):
    backups = list_backups(device_id)
    return backups[0] if backups else None


def get_backup(backup_id):
    with connect() as conn:
        row = conn.execute(f"SELECT {BACKUP_COLUMNS} FROM backups WHERE id = ?", (backup_id,)).fetchone()
    return dict(row) if row else None


def backup_blob(backup_id):
    with connect() as conn:
        row = conn.execute("SELECT blob FROM backups WHERE id = ?", (backup_id,)).fetchone()
    return row["blob"] if row else None


def previous_backup(backup):
    with connect() as conn:
        row = conn.execute(
            f"""SELECT {BACKUP_COLUMNS} FROM backups
                WHERE device_id = ? AND (taken_at, id) < (?, ?)
                ORDER BY taken_at DESC, id DESC LIMIT 1""",
            (backup["device_id"], backup["taken_at"], backup["id"]),
        ).fetchone()
    return dict(row) if row else None


def add_backup(device_id, reason, status, sha256, size, settings, changes, blob, keep):
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO backups (device_id, taken_at, confirmed_at, reason, name, model, build,
                                    sha256, size, settings, changes, blob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_id, now, now, reason, status.get("name"), status.get("model"), status.get("build"),
             sha256, size, settings, changes, blob),
        )
        conn.execute(
            """DELETE FROM backups WHERE device_id = ? AND id NOT IN (
                   SELECT id FROM backups WHERE device_id = ? ORDER BY taken_at DESC, id DESC LIMIT ?)""",
            (device_id, device_id, keep),
        )
        return cur.lastrowid


def confirm_backup(backup_id):
    with connect() as conn:
        conn.execute("UPDATE backups SET confirmed_at = ? WHERE id = ?", (time.time(), backup_id))


def backup_summary():
    """{device_id: {count, taken_at, confirmed_at}} for the dashboard."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT device_id, COUNT(*) AS count, MAX(taken_at) AS taken_at,
                      MAX(confirmed_at) AS confirmed_at
               FROM backups GROUP BY device_id"""
        ).fetchall()
    return {r["device_id"]: dict(r) for r in rows}


# --- activity log -----------------------------------------------------------


def add_event(device_id, message, ok=True):
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (device_id, at, ok, message) VALUES (?, ?, ?, ?)",
            (device_id, time.time(), int(ok), message),
        )


def list_events(device_id, limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE device_id = ? ORDER BY at DESC, id DESC LIMIT ?", (device_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# --- managed settings ---------------------------------------------------------


def get_managed(scope):
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM managed WHERE scope = ?", (scope,)).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_managed(scope, values):
    """Replace every managed value in a scope."""
    with connect() as conn:
        conn.execute("DELETE FROM managed WHERE scope = ?", (scope,))
        conn.executemany(
            "INSERT INTO managed (scope, key, value) VALUES (?, ?, ?)",
            [(scope, k, v) for k, v in values.items()],
        )


# --- per-router settings mapping ------------------------------------------------


def get_keymap(device_id):
    with connect() as conn:
        rows = conn.execute("SELECT portal_key, device_key FROM keymap WHERE device_id = ?", (device_id,)).fetchall()
    return {r["portal_key"]: r["device_key"] for r in rows}


def set_keymap(device_id, rows):
    """Replace the router's mapping and mark it as confirmed."""
    with connect() as conn:
        conn.execute("DELETE FROM keymap WHERE device_id = ?", (device_id,))
        conn.executemany(
            "INSERT INTO keymap (device_id, portal_key, device_key) VALUES (?, ?, ?)",
            [(device_id, k, v) for k, v in rows.items()],
        )
        conn.execute("UPDATE devices SET mapped = 1 WHERE id = ?", (device_id,))
