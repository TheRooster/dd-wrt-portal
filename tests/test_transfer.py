import io
import tarfile

import pytest

from app import auth, backups, config, crypto, db, router, transfer

PASS = "a long passphrase"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    def use(folder):
        folder.mkdir(exist_ok=True)
        monkeypatch.setattr(config, "DATA_DIR", folder)
        monkeypatch.setattr(config, "DB_PATH", folder / "portal.db")
        monkeypatch.setattr(config, "KEY_PATH", folder / "id_ed25519")
        monkeypatch.setattr(config, "BACKUP_KEY_PATH", folder / "backup.key")
        monkeypatch.setattr(router, "_portal_key", None)
        monkeypatch.setattr(crypto, "_fernet", None)
        monkeypatch.delenv("BACKUP_KEY", raising=False)
        backups.busy.clear()
        db.init()
        return folder
    use.tmp = tmp_path
    return use


def fill():
    db.upsert_device("192.0.2.7", 22, "host-key", "fingerprint")
    device_id = db.list_devices()[0]["id"]
    db.add_backup(device_id, "manual", {}, "0" * 64, 5, 1, None, crypto.encrypt(b"wifi secret"), 10)
    db.add_session("session-key", db.get_user("admin")["id"], 9e12)
    return device_id


def test_export_is_encrypted_and_round_trips(portal):
    portal(portal.tmp / "old")
    fill()
    blob = transfer.export(PASS)
    assert blob.startswith(transfer.MAGIC)
    for secret in (b"SQLite format", b"OPENSSH PRIVATE KEY", crypto.key(), b"192.0.2.7"):
        assert secret not in blob
    files = transfer.unpack(blob, PASS)
    assert set(files) == set(transfer.NAMES)
    assert files["id_ed25519"] == config.KEY_PATH.read_bytes()


def test_wrong_passphrase_and_damage_are_refused(portal):
    portal(portal.tmp / "old")
    blob = transfer.export(PASS)
    for bad in (lambda: transfer.unpack(blob, "the wrong passphrase"),
                lambda: transfer.unpack(blob[:-10] + b"x" * 10, PASS),
                lambda: transfer.unpack(b"not an export", PASS)):
        with pytest.raises(transfer.TransferError):
            bad()
    with pytest.raises(transfer.TransferError):
        transfer.check_passphrase("short", "short")
    with pytest.raises(transfer.TransferError):
        transfer.check_passphrase(PASS, PASS + "x")


def test_an_archive_with_unexpected_contents_is_refused(portal):
    portal(portal.tmp / "old")
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as tar:
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    salt = b"s" * 16
    blob = transfer.MAGIC + salt + transfer._cipher(PASS, salt).encrypt(packed.getvalue())
    with pytest.raises(transfer.TransferError, match="unexpected"):
        transfer.unpack(blob, PASS)


def test_import_replaces_state_and_keeps_the_old_one_aside(portal):
    portal(portal.tmp / "old")
    device_id = fill()
    db.set_user_password(db.get_user("admin")["id"], auth.hash_password("moved password"))
    old_public = router.public_key()
    blob = transfer.export(PASS)

    new = portal(portal.tmp / "new")
    assert db.list_devices() == [] and router.public_key() != old_public
    aside = transfer.install(transfer.unpack(blob, PASS))

    assert [d["host"] for d in db.list_devices()] == ["192.0.2.7"]
    assert router.public_key() == old_public
    assert crypto.decrypt(db.backup_blob(db.latest_backup(device_id)["id"])) == b"wifi secret"
    assert auth.check_password("admin", "moved password") and not auth.check_password("admin", "admin")
    assert auth.session_user("anything") is None and not db.session_user("session-key", 0)
    # The fresh portal had made no backup key yet, so there was none to move.
    assert sorted(p.name for p in aside.iterdir()) == ["id_ed25519", "portal.db"]
    assert aside.parent == new and not list(new.glob("*.importing"))


def test_import_is_refused_while_a_router_is_busy_or_the_backup_key_is_pinned_elsewhere(portal, monkeypatch):
    portal(portal.tmp / "old")
    blob = transfer.export(PASS)
    files = transfer.unpack(blob, PASS)
    backups.busy[1] = "Rebooting"
    with pytest.raises(transfer.TransferError, match="middle of a change"):
        transfer.install(files)
    backups.busy.clear()
    monkeypatch.setenv("BACKUP_KEY", "c29tZSBvdGhlciBrZXkgc29tZSBvdGhlciBrZXkgMTIzNDU2Nzg=")
    with pytest.raises(transfer.TransferError, match="BACKUP_KEY"):
        transfer.unpack(blob, PASS)


def test_export_and_import_through_the_web_pages(portal):
    from fastapi.testclient import TestClient

    from app.main import app
    origin = {"Origin": "http://testserver"}
    new_password = "correct horse battery"

    def signed_in_client():
        auth._failures.clear()
        c = TestClient(app, follow_redirects=False)
        c.post("/login", data={"username": "admin", "password": "admin"}, headers=origin)
        c.post("/account/password", data={"current": "admin", "password": new_password, "again": new_password}, headers=origin)
        c.cookies.clear()
        c.post("/login", data={"username": "admin", "password": new_password}, headers=origin)
        return c

    portal(portal.tmp / "old")
    fill()
    old = signed_in_client()
    assert old.get("/transfer").status_code == 200
    assert "err=" in old.post("/transfer/export", data={"passphrase": "short", "again": "short"}, headers=origin).headers["location"]
    r = old.post("/transfer/export", data={"passphrase": PASS, "again": PASS}, headers=origin)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    blob = r.content

    portal(portal.tmp / "new")
    new = signed_in_client()
    upload = {"file": ("portal.export", blob, "application/octet-stream")}
    r = new.post("/transfer/import", data={"passphrase": PASS}, files=upload, headers=origin)
    assert "err=" in r.headers["location"] and db.list_devices() == []          # box not ticked
    r = new.post("/transfer/import", data={"passphrase": "wrong passphrase", "confirm": "1"}, files=upload, headers=origin)
    assert "err=" in r.headers["location"] and db.list_devices() == []
    r = new.post("/transfer/import", data={"passphrase": PASS, "confirm": "1"}, files=upload, headers=origin)
    assert r.headers["location"].startswith("/login?msg=")
    assert [d["host"] for d in db.list_devices()] == ["192.0.2.7"]
    assert new.get("/transfer").status_code == 303                              # signed out by the import
