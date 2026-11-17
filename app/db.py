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
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS firmware_map (
    device_id   INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    folder      TEXT NOT NULL,    -- model folder on the download site
    pattern     TEXT NOT NULL,    -- image file name, with {build} for the build number
    confirmed   INTEGER NOT NULL DEFAULT 0,
    found_build INTEGER,          -- the build this was last looked up in
    file        TEXT,             -- the image found in that build, if any
    size        INTEGER
);
CREATE TABLE IF NOT EXISTS managed (
    scope TEXT NOT NULL,          -- 'fleet' or 'device:<id>'
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,     -- offline | drift | telnet | ssh-password
    message    TEXT NOT NULL,
    raised_at  REAL NOT NULL,
    cleared_at REAL               -- NULL while the condition holds
);
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password    TEXT NOT NULL,    -- salt:scrypt hash, both hex
    must_change INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    key     TEXT PRIMARY KEY,     -- SHA-256 of the cookie's token
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS client_names (
    mac  TEXT PRIMARY KEY,        -- lower case
    name TEXT NOT NULL
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
        if "user_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}:
            conn.execute("DROP TABLE sessions")  # from before users existed; sessions are disposable
            conn.executescript(SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
        if "mapped" not in columns:  # databases created before settings mapping existed
            conn.execute("ALTER TABLE devices ADD COLUMN mapped INTEGER NOT NULL DEFAULT 1")
    from . import auth  # here, not at the top: auth imports this module

    auth.ensure_default_user()


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


# --- firmware -------------------------------------------------------------------


def get_kv(key):
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_kv(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_firmware_map(device_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM firmware_map WHERE device_id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def set_firmware_map(device_id, folder, pattern, confirmed):
    """Record which image a router uses; clears what was found for the old choice."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO firmware_map (device_id, folder, pattern, confirmed) VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET folder = excluded.folder, pattern = excluded.pattern,
                   confirmed = excluded.confirmed, found_build = NULL, file = NULL, size = NULL""",
            (device_id, folder, pattern, int(confirmed)),
        )


def set_firmware_found(device_id, build, file, size):
    with connect() as conn:
        conn.execute(
            "UPDATE firmware_map SET found_build = ?, file = ?, size = ? WHERE device_id = ?",
            (build, file, size, device_id),
        )


# --- Wi-Fi client names ----------------------------------------------------------


def client_names():
    with connect() as conn:
        return {r["mac"]: r["name"] for r in conn.execute("SELECT mac, name FROM client_names")}


def set_client_name(mac, name):
    """Name a client by its hardware address; an empty name removes it."""
    with connect() as conn:
        if name:
            conn.execute(
                "INSERT INTO client_names (mac, name) VALUES (?, ?) ON CONFLICT(mac) DO UPDATE SET name = excluded.name",
                (mac, name),
            )
        else:
            conn.execute("DELETE FROM client_names WHERE mac = ?", (mac,))


# --- alerts -----------------------------------------------------------------------


def open_alerts():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM alerts WHERE cleared_at IS NULL ORDER BY raised_at").fetchall()
    return [dict(r) for r in rows]


def cleared_alerts(limit=50):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE cleared_at IS NOT NULL ORDER BY cleared_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def raise_alert(device_id, kind, message, at):
    with connect() as conn:
        conn.execute(
            "INSERT INTO alerts (device_id, kind, message, raised_at) VALUES (?, ?, ?, ?)",
            (device_id, kind, message, at),
        )


def clear_alert(alert_id, at):
    with connect() as conn:
        conn.execute("UPDATE alerts SET cleared_at = ? WHERE id = ?", (at, alert_id))


# --- users and sign-in sessions --------------------------------------------------------


def list_users():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY username")]


def get_user(username):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def add_user(username, password_hash, must_change):
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password, must_change, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, int(must_change), time.time()),
        )


def set_user_password(user_id, password_hash):
    """Store a new password hash and end all of the user's sessions."""
    with connect() as conn:
        conn.execute("UPDATE users SET password = ?, must_change = 0 WHERE id = ?", (password_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id):
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def add_session(key, user_id, expires):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        conn.execute("INSERT INTO sessions (key, user_id, expires) VALUES (?, ?, ?)", (key, user_id, expires))


def session_user(key, now):
    with connect() as conn:
        row = conn.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.key = ? AND sessions.expires > ?", (key, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session(key):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
