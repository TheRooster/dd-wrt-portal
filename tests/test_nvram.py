import struct

import pytest

from app import nvram


def build(settings):
    out = nvram.MAGIC + struct.pack("<H", len(settings))
    for name, value in settings.items():
        n, v = name.encode(), value.encode()
        out += bytes([len(n)]) + n + struct.pack("<H", len(v)) + v
    return out


def test_roundtrip_including_multiline_and_empty_values():
    settings = {"router_name": "AP", "rc_startup": "line one\nline two", "wan_domain": ""}
    assert nvram.parse(build(settings)) == settings


@pytest.mark.parametrize("raw", [b"", b"NOTDDWRT", build({"a": "b"})[:-1], build({"a": "b"}) + b"x"])
def test_bad_files_are_rejected(raw):
    with pytest.raises(nvram.BackupFormatError):
        nvram.parse(raw)


@pytest.mark.parametrize(
    "name", ["wl0_wpa_psk", "wlan1_psk2", "http_passwd", "http_username", "wl0_key1",
             "sshd_ed25519_host_key", "radius_passphrase", "pptp_pass", "openvpn_key", "secret_code"]
)
def test_credentials_are_secret(name):
    assert nvram.is_secret(name)


@pytest.mark.parametrize(
    "name", ["wl0_ssid", "wl0_crypto", "sshd_authorized_keys", "sshd_passwd_auth", "wl0_channel", "radius_port"]
)
def test_ordinary_settings_are_not_secret(name):
    assert not nvram.is_secret(name)


def test_diff_kinds_and_masking():
    old = {"wl0_channel": "4", "wl0_wpa_psk": "old-pass", "gone": "1", "same": "x"}
    new = {"wl0_channel": "6", "wl0_wpa_psk": "new-pass", "fresh": "", "same": "x"}
    changes = {c["name"]: c for c in nvram.diff(old, new)}
    assert set(changes) == {"wl0_channel", "wl0_wpa_psk", "gone", "fresh"}
    assert (changes["wl0_channel"]["old"], changes["wl0_channel"]["new"]) == ("4", "6")
    assert changes["gone"]["kind"] == "removed" and changes["fresh"]["kind"] == "added"
    psk = changes["wl0_wpa_psk"]
    assert psk["kind"] == "changed" and psk["old"] == psk["new"] == "(hidden)"
    assert "pass" not in str(nvram.listing(new))


def test_boot_timestamps_are_not_changes():
    assert nvram.diff({"start_time": "1", "end_time": "2", "a": "x"}, {"start_time": "9", "end_time": "8", "a": "x"}) == []
