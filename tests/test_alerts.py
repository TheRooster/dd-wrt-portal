from app import alerts, config


def device(**over):
    d = {
        "id": 1, "host": "192.0.2.1", "enrolled_at": 0, "last_ok": 1000, "online": True, "drift": 0, "busy": None,
        "status": {"name": "AP-One", "telnet_enabled": False, "ssh_password_auth": False},
    }
    return d | over


def test_a_healthy_router_has_no_conditions():
    assert alerts.conditions(device(), now=1010) == {}


def test_offline_waits_for_the_grace_period():
    d = device(online=False)
    assert alerts.conditions(d, now=1000 + config.ALERT_OFFLINE_AFTER - 1) == {}
    found = alerts.conditions(d, now=1000 + config.ALERT_OFFLINE_AFTER + 60)
    assert list(found) == ["offline"] and "AP-One" in found["offline"]


def test_drift_and_open_services_are_reported_while_online():
    status = {"name": "AP-One", "telnet_enabled": True, "ssh_password_auth": True}
    found = alerts.conditions(device(drift=1, status=status), now=1010)
    assert set(found) == {"drift", "telnet", "ssh-password"}
    assert "1 managed setting differs" in found["drift"]


def test_alerts_open_once_and_clear(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "portal.db")
    db.init()
    dev = db.upsert_device("192.0.2.1", 22, "key", "fp")
    device_id = dev if isinstance(dev, int) else db.list_devices()[0]["id"]
    d = device(id=device_id, drift=2)
    assert len(alerts.evaluate([d], now=1010)[0]) == 1
    assert alerts.evaluate([d], now=1070) == ([], [])
    # Unreachable routers keep their settings alerts; busy routers are left alone.
    assert alerts.evaluate([d | {"online": False}], now=1080) == ([], [])
    assert alerts.evaluate([d | {"busy": "Rebooting", "drift": 0}], now=1090) == ([], [])
    raised, cleared = alerts.evaluate([d | {"drift": 0}], now=1100)
    assert raised == [] and len(cleared) == 1 and db.open_alerts() == []
