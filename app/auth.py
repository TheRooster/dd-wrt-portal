"""Sign-in for the portal: named users with scrypt-hashed passwords, and session cookies.

Viewing the dashboard needs no sign-in. Every other page, and every request
that changes anything, does. A default user is created with the database and
must choose a new password at first sign-in.
"""

import hashlib
import hmac
import re
import secrets
import time

from . import config, db

COOKIE = "portal_session"
MIN_LENGTH = 10
# Reachable without signing in. Everything else is refused, including routes added later.
PUBLIC = {"/", "/partials/devices", "/api/devices", "/healthz", "/login"}
# All a user may reach while their password still has to be changed.
PASSWORD_CHANGE = {"/account", "/account/password", "/logout"}

_failures = {}  # {username: times of recent wrong passwords}
MAX_FAILURES, WINDOW = 5, 300


def is_public(method, path):
    if path.startswith("/static/"):
        return method in ("GET", "HEAD")
    if path == "/login":
        return True
    return method in ("GET", "HEAD") and path in PUBLIC


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}:{_scrypt(password, salt).hex()}"


def _scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _matches(password, stored):
    salt, digest = (bytes.fromhex(part) for part in stored.split(":"))
    return hmac.compare_digest(_scrypt(password, salt), digest)


def ensure_default_user():
    """Create the default user if there are no users at all. Its password must be changed at first sign-in."""
    if not db.list_users():
        db.add_user(config.DEFAULT_USER, hash_password(config.DEFAULT_PASSWORD), must_change=True)


def valid_username(name):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,32}", name))


def check_new_password(password, again):
    """Raise ValueError with a message for the user if the new password is not acceptable."""
    if password != again:
        raise ValueError("The two passwords do not match.")
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    if password == config.DEFAULT_PASSWORD:
        raise ValueError("Choose a password other than the default.")


def locked_out(username, now=None):
    now = now or time.time()
    recent = [t for t in _failures.get(username, []) if now - t < WINDOW]
    _failures[username] = recent
    return len(recent) >= MAX_FAILURES


def check_password(username, password, now=None):
    """The user's row if the password is right and the name is not locked out, else None."""
    if locked_out(username, now):
        return None
    user = db.get_user(username)
    # Hash even for an unknown name, so the answer takes as long either way.
    if _matches(password, user["password"] if user else hash_password("")) and user:
        return user
    _failures.setdefault(username, []).append(now or time.time())
    return None


def _key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id, now=None):
    """Create a session and return the token for the cookie. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    db.add_session(_key(token), user_id, (now or time.time()) + config.SESSION_HOURS * 3600)
    return token


def session_user(token, now=None):
    """The signed-in user for a cookie token, or None."""
    return db.session_user(_key(token), now or time.time()) if token else None


def end_session(token):
    if token:
        db.delete_session(_key(token))


def safe_next(target):
    """A path on this site to return to after signing in; anything else becomes the dashboard."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
