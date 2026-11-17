import pytest

from app import profile
from app.profile import Mapping

BROADCOM = {
    "wl0_ssid": "Home", "wl0_nband": "2", "wl0_channel": "4", "wl0_wpa_psk": "pass-for-24", "wl0_txpwr": "71",
    "wl1_ssid": "Home-5", "wl1_nband": "1", "wl1_channel": "60", "wl1_wpa_psk": "pass-for-5", "wl1_txpwr": "71",
    "wl0.1_ssid": "guest", "telnetd_enable": "1", "time_zone": "America/Los_Angeles", "upnp_enable": "0",
    "lan_ipaddr": "192.168.1.3", "http_passwd": "hash", "router_name": "A",
    "schedule_enable": "1", "schedule_hour_time": "2", "schedule_hours": "3", "schedule_minutes": "0",
    "schedule_weekdays": "*",
}
# The MediaTek router also carries unused Broadcom-style defaults, as the real one does.
MAC80211 = {
    "wl0_ssid": "dd-wrt", "wl0_nband": "0",
    "wlan0_ssid": "Home-5", "wlan0_channel": "5600", "wlan0_wpa_psk": "pass-for-5",
    "wlan1_ssid": "Home", "wlan1_channel": "2412", "wlan1_wpa_psk": "pass-for-24",
    "telnetd_enable": "1", "time_zone": "America/Los_Angeles", "upnp_enable": "1",
    "lan_ipaddr": "192.168.1.7", "http_passwd": "hash", "router_name": "B",
}
B = Mapping(profile.detect_radios(BROADCOM))
M = Mapping(profile.detect_radios(MAC80211))


def test_radio_detection():
    assert B.radios == {"2.4": "wl0", "5": "wl1"}
    assert M.radios == {"5": "wlan0", "2.4": "wlan1"}
    assert profile.radio_prefixes(MAC80211) == ["wl0", "wlan0", "wlan1"]


def test_band_falls_back_to_live_status_on_auto_channel():
    current = MAC80211 | {"wlan0_channel": "0"}
    status = {"radios": [{"name": "wlan0", "band": "5 GHz"}, {"name": "wlan1", "band": "2.4 GHz"}]}
    assert profile.detect_radios(current, status) == {"5": "wlan0", "2.4": "wlan1"}
    assert profile.detect_radios(current) == {"2.4": "wlan1"}


def test_portal_keys_name_radios_by_band():
    assert B.device_key("wifi24.ssid") == "wl0_ssid"
    assert M.device_key("wifi24.ssid") == "wlan1_ssid"
    assert Mapping({"5": "wl1"}).device_key("wifi24.ssid") is None
    keys = M.portal_keys(MAC80211)
    assert keys["wifi5.channel"] == "wlan0_channel" and keys["upnp_enable"] == "upnp_enable"
    assert "wl0_ssid" not in keys and "wl0_ssid" not in keys.values()  # unmapped radio has no portal name
    assert "wl0.1_ssid" not in B.portal_keys(BROADCOM).values()  # nor do virtual interfaces


def test_alias_renames_a_setting_for_one_router():
    current = {k: v for k, v in BROADCOM.items() if k != "upnp_enable"} | {"upnp": "0"}
    assert profile.resolve({}, {}, {"upnp_enable": "1"}, current, B) == ({}, ["upnp_enable"])
    aliased = Mapping(B.radios, {"upnp_enable": "upnp"})
    assert profile.resolve({}, {}, {"upnp_enable": "1"}, current, aliased) == ({"upnp": "1"}, [])
    assert aliased.portal_keys(current)["upnp_enable"] == "upnp"


def test_read_is_platform_independent():
    for current, mapping, ch5, ch24 in ((BROADCOM, B, "60", "4"), (MAC80211, M, "120", "1")):
        got = profile.read(current, mapping)
        assert (got["ssid_24"], got["ssid_5"], got["channel_24"], got["channel_5"]) == ("Home", "Home-5", ch24, ch5)
        assert (got["passphrase_24"], got["passphrase_5"], got["telnet"]) == ("pass-for-24", "pass-for-5", "1")
    assert profile.read(BROADCOM, B)["reboot_time"] == "03:00"


def test_resolve_per_platform_with_separate_passphrases():
    fleet = {"ssid_24": "New", "passphrase_24": "new-pass-24", "passphrase_5": "new-pass-5", "telnet": "0",
             "reboot_time": "04:30"}
    over = {"channel_24": "6", "channel_5": "149"}
    wanted, unsupported = profile.resolve(fleet, over, {"upnp_enable": "1"}, BROADCOM, B)
    assert unsupported == []
    assert wanted == {
        "wl0_ssid": "New", "wl0_wpa_psk": "new-pass-24", "wl1_wpa_psk": "new-pass-5", "telnetd_enable": "0",
        "schedule_hour_time": "2", "schedule_hours": "4", "schedule_minutes": "30", "schedule_weekdays": "*",
        "wl0_channel": "6", "wl1_channel": "149", "upnp_enable": "1",
    }
    wanted, unsupported = profile.resolve(fleet, over, {"upnp_enable": "1"}, MAC80211, M)
    assert unsupported == ["Reboot time"]  # this router has no schedule_* settings
    assert (wanted["wlan1_ssid"], wanted["wlan1_channel"], wanted["wlan0_channel"]) == ("New", "2437", "5745")
    assert (wanted["wlan1_wpa_psk"], wanted["wlan0_wpa_psk"]) == ("new-pass-24", "new-pass-5")
    assert not any(k.startswith("schedule_") or k.startswith("wl0_") for k in wanted)


def test_unsupported_settings_are_reported_and_never_written():
    no_5ghz = {k: v for k, v in BROADCOM.items() if not k.startswith("wl1_")}
    mapping = Mapping(profile.detect_radios(no_5ghz))
    wanted, unsupported = profile.resolve(
        {"ssid_5": "X", "passphrase_5": "whatever-5"}, {"channel_5": "36"}, {"no_such_setting": "1"}, no_5ghz, mapping
    )
    assert wanted == {}
    assert unsupported == ["5 GHz network name", "5 GHz passphrase", "5 GHz channel", "no_such_setting"]


def test_unmanaged_settings_are_never_touched():
    assert profile.resolve({}, {}, {}, BROADCOM, B) == ({}, [])
    assert profile.resolve({"telnet": "0"}, {}, {}, BROADCOM, B) == ({"telnetd_enable": "0"}, [])


def test_encode_decode_roundtrip():
    for current, mapping in ((BROADCOM, B), (MAC80211, M)):
        for field in profile.FIELDS:
            value = profile.decode(field, current, mapping)
            if value is not None:
                written = profile.encode(field, value, current, mapping)
                assert written and all(current[k] == v for k, v in written.items()), field.key


def test_shared_settings():
    shared = profile.shared([(BROADCOM, B), (MAC80211, M)])
    assert shared["upnp_enable"] == ["0", "1"]
    # named fields, secrets, identity, and settings only one router has are left out
    for key in ("telnetd_enable", "time_zone", "wifi24.ssid", "wifi5.wpa_psk", "http_passwd", "lan_ipaddr",
                "router_name", "wifi24.txpwr", "schedule_enable"):
        assert key not in shared
    assert "wifi24.txpwr" in profile.shared([(BROADCOM, B), (BROADCOM, B)])


@pytest.mark.parametrize("key,raw,ok", [
    ("passphrase_5", "short", False), ("passphrase_24", "long enough!", True),
    ("ssid_24", "x" * 33, False), ("ssid_24", "Home Net", True),
    ("channel_24", "12", False), ("channel_24", "6", True), ("channel_5", "36", True), ("channel_5", "37", False),
    ("reboot_time", "3:05", True), ("reboot_time", "25:00", False),
    ("syslog_server", "192.168.1.9", True), ("syslog_server", "logs; rm -rf", False),
    ("time_zone", "America/Los_Angeles", True), ("time_zone", "bad zone!", False),
    ("telnet", "0", True), ("telnet", "maybe", False),
])
def test_validation(key, raw, ok):
    field = profile.BY_KEY[key]
    if ok:
        profile.validate(field, raw)
    else:
        with pytest.raises(ValueError):
            profile.validate(field, raw)


def test_plain_values_must_be_single_line():
    assert profile.validate_plain("x", "") == ""
    with pytest.raises(ValueError):
        profile.validate_plain("x", "a\nb")


def test_display_never_shows_a_secret():
    assert profile.display(profile.BY_KEY["passphrase_5"], "secret-pass") == "Set"


@pytest.mark.parametrize("key", ["wifi24.key", "wifi5.radius_key", "wifi24.security_mode", "sw_wan", "need_commit",
                                 "wan_ipaddr", "et0macaddr", "sshd_port", "http_lanport"])
def test_not_offered_for_fleet_management(key):
    assert not profile.manageable(key)


@pytest.mark.parametrize("key", ["upnp_enable", "wifi24.txpwr", "wifi5.closed", "dnsmasq_enable", "stylus"])
def test_offered_for_fleet_management(key):
    assert profile.manageable(key)
