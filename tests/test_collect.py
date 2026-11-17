"""Parser tests against output captured from the real routers."""

from pathlib import Path

import pytest

from app.collect import parse

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return parse((FIXTURES / name).read_text())


def test_broadcom_arm():
    s = load("tew818dru.txt")
    assert s["model"] == "Trendnet TEW818DRU"
    assert s["build"] == 52054
    assert s["release"] == "03/18/23"
    assert [r["name"] for r in s["radios"]] == ["wl0", "wl1"]
    assert [r["band"] for r in s["radios"]] == ["2.4 GHz", "5 GHz"]
    assert s["radios"][0]["ssid"] == "HomeNet"
    assert s["clients"] == sum(r["clients"] for r in s["radios"])
    assert s["telnet_enabled"] and s["ssh_password_auth"]


def test_broadcom_mips():
    s = load("rt-ac66u.txt")
    assert s["model"] == "Asus RT-AC66U"
    assert s["build"] == 55678
    assert len(s["radios"]) == 2
    assert s["mem_total_kb"] > 200_000


def test_mediatek():
    s = load("r6220.txt")
    assert s["model"] == "Netgear R6220"
    assert s["build"] == 53323
    assert {r["name"] for r in s["radios"]} == {"wlan0", "wlan1"}
    assert {r["ssid"] for r in s["radios"]} == {"HomeNet", "HomeNet-5G"}
    assert all(r["width_mhz"] == 20 for r in s["radios"])


@pytest.mark.parametrize("name", ["tew818dru.txt", "rt-ac66u.txt", "r6220.txt"])
def test_common_fields(name):
    s = load(name)
    assert s["name"].startswith("AP-")
    assert s["uptime_s"] > 0
    assert 0 < s["mem_avail_kb"] <= s["mem_total_kb"]
    assert s["nvram_used"] > 0 and s["nvram_free"] > 0
    assert all(r["channel"] for r in s["radios"])


def test_truncated_output_is_rejected():
    text = (FIXTURES / "r6220.txt").read_text().replace("@@end", "")
    with pytest.raises(ValueError):
        parse(text)


def test_ssid_may_contain_separator():
    text = "@@radios\nwl0|6|20|3|psk2|Odd|Name\n@@end\n"
    assert parse(text)["radios"][0]["ssid"] == "Odd|Name"


def test_stations_from_mac80211_and_broadcom():
    s = load("r6220.txt")
    assert s["stations"][0] == {
        "mac": "02:00:00:00:00:a1", "radio": "wlan0", "band": "5 GHz",
        "signal": -58, "rate_mbps": 866.7, "connected_s": 5321,
    }
    b = load("rt-ac66u.txt")
    assert [(c["mac"], c["band"], c["signal"], c["rate_mbps"]) for c in b["stations"]] == [
        ("02:00:00:00:00:b1", "2.4 GHz", -41, None),
        ("02:00:00:00:00:b2", "5 GHz", -36, None),
    ]
    assert load("tew818dru.txt")["stations"] == []
