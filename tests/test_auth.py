import time

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db

ORIGIN = {"Origin": "http://testserver"}
NEW = "correct horse battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "portal.db")
    monkeypatch.setattr(config, "KEY_PATH", tmp_path / "id_ed25519")
    db.init()
    auth._failures.clear()
    from app.main import app
    return TestClient(app, follow_redirects=False)  # no lifespan: nothing polls


def post(client, path, **data):
    return client.post(path, data=data, headers=ORIGIN)


def sign_in(client, username="admin", password="admin"):
    return post(client, "/login", username=username, password=password)


def ready(client):
    """Sign in as the default user and get past the forced password change."""
    sign_in(client)
    post(client, "/account/password", current="admin", password=NEW, again=NEW)
    client.cookies.clear()
    assert sign_in(client, password=NEW).headers["location"] == "/"


def test_only_the_dashboard_is_public():
    assert auth.is_public("GET", "/") and auth.is_public("GET", "/partials/devices")
    assert auth.is_public("GET", "/static/style.css") and auth.is_public("POST", "/login")
    for method, path in [("POST", "/"), ("GET", "/settings"), ("GET", "/backups/1/download"), ("POST", "/users"),
                         ("POST", "/poll"), ("GET", "/devices/1/logs"), ("POST", "/static/x"), ("GET", "/anything-new")]:
        assert not auth.is_public(method, path)


def test_return_path_cannot_leave_the_site():
    assert auth.safe_next("/settings?x=1") == "/settings?x=1"
    for bad in ("//evil.example", "https://evil.example", "", "/\\evil"):
        assert auth.safe_next(bad) == "/"


def test_database_starts_with_one_default_user_and_no_plain_password(client):
    users = db.list_users()
    assert [(u["username"], u["must_change"]) for u in users] == [("admin", 1)]
    assert "admin" not in users[0]["password"]
    db.init()
    assert len(db.list_users()) == 1


def test_dashboard_is_open_and_everything_else_redirects(client):
    assert client.get("/").status_code == 200
    assert "Add a router" not in client.get("/").text
    r = client.get("/settings")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsettings"
    for path in ("/poll", "/devices", "/sync/all", "/channels/scan", "/clients/name", "/users", "/logout"):
        r = post(client, path, host="192.0.2.1", password="x", username="x", again="x")
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_default_password_must_be_changed_before_anything_else(client):
    r = sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/settings").headers["location"] == "/account"
    assert post(client, "/poll").headers["location"] == "/account"
    assert post(client, "/users", username="eve", password=NEW, again=NEW).headers["location"] == "/account"
    for password, again in (("short", "short"), ("admin", "admin"), (NEW, NEW + "x")):
        r = post(client, "/account/password", current="admin", password=password, again=again)
        assert "err=" in r.headers["location"]
    r = post(client, "/account/password", current="admin", password=NEW, again=NEW)
    assert r.headers["location"].startswith("/login")
    assert client.get("/settings").headers["location"].startswith("/login")  # old session ended
    client.cookies.clear()
    assert sign_in(client).status_code == 401
    assert sign_in(client, password=NEW).headers["location"] == "/"
    assert client.get("/settings").status_code == 200
    assert "Add a router" in client.get("/").text


def test_users_can_be_added_and_removed(client):
    ready(client)
    assert "err=" in post(client, "/users", username="bad name!", password=NEW, again=NEW).headers["location"]
    assert "msg=" in post(client, "/users", username="sam", password="first password", again="first password").headers["location"]
    assert "err=" in post(client, "/users", username="SAM", password=NEW, again=NEW).headers["location"]
    sam = db.get_user("sam")
    assert sam["must_change"] == 1
    me = db.get_user("admin")
    assert "err=" in post(client, f"/users/{me['id']}/delete").headers["location"]
    other = TestClient(client.app, follow_redirects=False)
    assert sign_in(other, "sam", "first password").headers["location"] == "/account"
    post(client, f"/users/{sam['id']}/delete")
    assert db.get_user("sam") is None
    assert other.get("/account").headers["location"].startswith("/login")  # their session went with them


def test_wrong_passwords_lock_that_user_only(client):
    ready(client)
    client.cookies.clear()
    for _ in range(auth.MAX_FAILURES):
        assert sign_in(client, password="nope nope nope").status_code == 401
    assert sign_in(client, password=NEW).status_code == 429
    assert sign_in(client, "nobody", "whatever").status_code == 401
    assert client.get("/settings").status_code == 303


def test_a_guessed_or_expired_cookie_does_nothing(client):
    sign_in(client)
    token = client.cookies.get(auth.COOKIE)
    assert auth.session_user(token)["username"] == "admin"
    assert auth.session_user("guess") is None and auth.session_user(None) is None
    assert auth.session_user(token, now=time.time() + config.SESSION_HOURS * 3600 + 1) is None
