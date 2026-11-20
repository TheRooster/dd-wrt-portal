"""The managed settings and how they map onto each router.

The portal keeps its own names for settings ("portal keys"):

* Radio settings are named by band, not by interface: `wifi24.ssid`,
  `wifi5.wpa_psk`. Each router's mapping says which of its radios
  (`wl0`, `wlan1`, ...) serves each band, so `wifi24.ssid` becomes
  `wl0_ssid` on one router and `wlan1_ssid` on another.
* Every other setting uses its DD-WRT nvram name, unless a router's mapping
  gives it a different name there (an alias).

Named fields (below) are settings the portal understands: they have labels,
validation, and sometimes span several keys. Any other portal key that every
router has can be managed as a plain value.
"""

import ipaddress
import re
from dataclasses import dataclass, field as dc_field

from . import nvram

BANDS = ("5", "2.4")
CHANNELS_24 = list(range(1, 12))
CHANNELS_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
              149, 153, 157, 161, 165]
DFS_CHANNELS = frozenset(c for c in CHANNELS_5 if 52 <= c <= 144)

_RADIO_KEY = re.compile(r"^((?:wl|wlan|ath|ra|rai)\d+)(\.\d+)?_(.+)$")
_PORTAL_RADIO_KEY = re.compile(r"^wifi(24|5)\.(.+)$")
_TAG = {"2.4": "24", "5": "5"}
_BAND = {tag: band for band, tag in _TAG.items()}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str  # text | secret | onoff | time | channel
    keys: tuple  # the portal keys this field is stored in
    help: str = ""
    band: str = ""
    per_device: bool = False  # unique per router unless the user makes it shared


_REBOOT_KEYS = ("schedule_hour_time", "schedule_hours", "schedule_minutes", "schedule_weekdays")

FIELDS = [
    Field("ssid_5", "5 GHz network name", "Wi-Fi", "text", ("wifi5.ssid",), band="5"),
    Field("passphrase_5", "5 GHz passphrase", "Wi-Fi", "secret", ("wifi5.wpa_psk",), "8 to 63 characters.", band="5"),
    Field("ssid_24", "2.4 GHz network name", "Wi-Fi", "text", ("wifi24.ssid",), band="2.4"),
    Field("passphrase_24", "2.4 GHz passphrase", "Wi-Fi", "secret", ("wifi24.wpa_psk",), "8 to 63 characters.",
          band="2.4"),
    Field("time_zone", "Time zone", "Time", "text", ("time_zone",), "For example America/Los_Angeles."),
    Field("ntp_server", "Time server", "Time", "text", ("ntp_server",), "Host name or IP address."),
    Field("telnet", "Telnet", "Access", "onoff", ("telnetd_enable",)),
    Field("ssh_password", "SSH password login", "Access", "onoff", ("sshd_passwd_auth",),
          "The portal logs in with its own key either way."),
    Field("syslog", "System log", "Logging", "onoff", ("syslogd_enable",)),
    Field("syslog_server", "Send logs to", "Logging", "text", ("syslogd_rem_ip",), "IP address of a syslog server."),
    Field("scheduled_reboot", "Scheduled reboot", "Maintenance", "onoff", ("schedule_enable",)),
    Field("reboot_time", "Reboot time", "Maintenance", "time", _REBOOT_KEYS, "Daily, in the router's time zone."),
    Field("channel_5", "5 GHz channel", "Channels", "channel", ("wifi5.channel",), band="5", per_device=True),
    Field("channel_24", "2.4 GHz channel", "Channels", "channel", ("wifi24.channel",), band="2.4", per_device=True),
]
BY_KEY = {f.key: f for f in FIELDS}
FIELD_PORTAL_KEYS = [k for f in FIELDS for k in f.keys]
_OWNED = frozenset(FIELD_PORTAL_KEYS)

# Settings that identify one router, describe its hardware, or control how the
# portal reaches it. Giving every router the same value would break them, so
# they are not offered for fleet-wide management.
_UNSHARED = re.compile(
    r"(hwaddr|macaddr|_mac$|_ipaddr$|^lan_gateway$|^router_name$|^wan_hostname$|^DD_BOARD|^sshd_|^http_"
    r"|^nvram_ver|^os_|^dist_type$|boot|^et\d|^pci/|^sb/|^\d+:|^board|^sdram|^clk|^cfe|^def_|^wl_"
    r"|_done$|^ntp_success$|^flash_|^probe_|^gpio|ifnames?$|^landevs$|^wandevs$|^vlan\d|^port\d"
    r"|^need_commit$|^sw_|^portvlanlist$|^trunking$|^wan_iface$|^wan_get_|_buf$|^wifi_display$|^ezc_version$"
    # Radio settings whose values mean different things on different Wi-Fi chipsets.
    r"|^wifi(24|5)\.(ifname|unit|hw|radioids?|vifs|phytype|nband|security_mode|akm|auth_?mode|crypto|wep|key))"
)


# --- validation ---------------------------------------------------------------


def validate(field, raw):
    """Return the normalised value for a named field, or raise ValueError with a message."""
    value = raw.strip() if field.kind != "secret" else raw
    if field.kind == "onoff":
        if value not in ("0", "1"):
            raise ValueError(f"{field.label}: choose On or Off")
    elif field.kind == "time":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            raise ValueError(f"{field.label}: enter a time such as 03:00")
        value = f"{int(m[1]):02d}:{m[2]}"
    elif field.kind == "channel":
        allowed = CHANNELS_24 if field.band == "2.4" else CHANNELS_5
        if not value.isdigit() or int(value) not in allowed:
            raise ValueError(f"{field.label}: {value} is not a valid channel")
    elif field.kind == "secret":
        if not 8 <= len(value) <= 63 or not all(32 <= ord(c) < 127 for c in value):
            raise ValueError(f"{field.label}: use 8 to 63 letters, digits, and symbols")
    elif field.key in ("ssid_24", "ssid_5"):
        if not 1 <= len(value.encode()) <= 32 or any(ord(c) < 32 for c in value):
            raise ValueError(f"{field.label}: use 1 to 32 characters")
    elif field.key == "time_zone":
        if not re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}", value):
            raise ValueError(f"{field.label}: use a name such as America/Los_Angeles")
    elif field.key == "ntp_server":
        if not re.fullmatch(r"[A-Za-z0-9.\-:]{1,253}", value):
            raise ValueError(f"{field.label}: enter a host name or IP address")
    elif field.key == "syslog_server":
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise ValueError(f"{field.label}: enter an IP address") from None
    return value


def validate_plain(key, value):
    """Check a value for a plain (unnamed) managed setting."""
    if len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{key}: use a single line of at most 1024 characters")
    return value


# --- per-router mapping ---------------------------------------------------------


@dataclass
class Mapping:
    """How one router names the portal's settings."""

    radios: dict = dc_field(default_factory=dict)  # {"2.4": "wl0", "5": "wl1"}
    aliases: dict = dc_field(default_factory=dict)  # {portal key: this router's key}

    def device_key(self, portal_key):
        """This router's nvram name for a portal key, or None if it has no radio for it."""
        if portal_key in self.aliases:
            return self.aliases[portal_key]
        m = _PORTAL_RADIO_KEY.match(portal_key)
        if m:
            prefix = self.radios.get(_BAND[m[1]])
            return f"{prefix}_{m[2]}" if prefix else None
        return portal_key

    def portal_keys(self, current):
        """{portal key: this router's key} for every setting the router has."""
        by_prefix = {prefix: _TAG[band] for band, prefix in self.radios.items()}
        found = {}
        for key in current:
            m = _RADIO_KEY.match(key)
            if not m:
                found[key] = key
            elif m[1] in by_prefix and not m[2]:
                found[f"wifi{by_prefix[m[1]]}.{m[3]}"] = key
            # other radio keys (unmapped radios, virtual interfaces) have no portal name
        for portal_key, key in self.aliases.items():
            if key in current:
                found[portal_key] = key
        return found


def radio_prefixes(current):
    """Every radio the router has settings for, e.g. ["wl0", "wl1"]."""
    return sorted({m[1] for k in current if (m := _RADIO_KEY.match(k)) and not m[2] and m[3] == "ssid"})


def detect_radios(current, status=None):
    """Best guess at which radio serves each band: {"2.4": prefix, "5": prefix}."""
    live = {r["name"]: (r.get("band") or "").replace(" GHz", "") for r in (status or {}).get("radios", [])}
    prefixes = radio_prefixes(current)
    if any(p.startswith("wlan") for p in prefixes):
        # mac80211 routers also carry unused wlN_* defaults; ignore those.
        prefixes = [p for p in prefixes if p.startswith("wlan")]
    bands = {}
    for prefix in prefixes:
        band = ""
        channel = current.get(f"{prefix}_channel", "")
        if channel.isdigit() and int(channel) > 1000:
            band = "2.4" if int(channel) < 3000 else "5"
        if not band:
            band = {"2": "2.4", "1": "5"}.get(current.get(f"{prefix}_nband", ""), "")
        if not band:
            band = live.get(prefix, "")
        if band in _TAG:
            bands.setdefault(band, prefix)
    return bands


# --- channel units --------------------------------------------------------------


def _mhz(channel):
    channel = int(channel)
    if channel == 14:
        return 2484
    return (2407 if channel <= 13 else 5000) + 5 * channel


def _from_mhz(mhz):
    mhz = int(mhz)
    if mhz == 2484:
        return 14
    return (mhz - 2407) // 5 if mhz < 3000 else (mhz - 5000) // 5


def _stores_mhz(key, current):
    """Whether this router stores the channel in `key` as a frequency rather than a number."""
    value = current.get(key, "")
    if value.isdigit() and int(value) > 0:
        return int(value) > 1000
    return key.startswith(("wlan", "ath"))


# --- reading and writing named fields --------------------------------------------


def _device_keys(field, current, mapping):
    """This router's keys for a field, in the order of field.keys, or None if any is missing."""
    keys = [mapping.device_key(k) for k in field.keys]
    if any(k is None or k not in current for k in keys):
        return None
    return keys


def supported(field, current, mapping):
    return _device_keys(field, current, mapping) is not None


def encode(field, value, current, mapping):
    """{this router's key: value} that gives `field` the value `value`; {} if unsupported."""
    keys = _device_keys(field, current, mapping)
    if keys is None:
        return {}
    if field.kind == "time":
        hours, minutes = value.split(":")
        # "2" selects "at a set time" rather than "every N seconds"
        return dict(zip(keys, ("2", str(int(hours)), str(int(minutes)), "*")))
    if field.kind == "channel":
        return {keys[0]: str(_mhz(value)) if _stores_mhz(keys[0], current) else str(int(value))}
    return {keys[0]: value}


def decode(field, current, mapping):
    """The value `field` has on this router now; None if unsupported or unclear."""
    keys = _device_keys(field, current, mapping)
    if keys is None:
        return None
    try:
        if field.kind == "time":
            mode, hours, minutes, _ = (current[k] for k in keys)
            return f"{int(hours):02d}:{int(minutes):02d}" if mode == "2" else None
        value = current[keys[0]]
        if field.kind == "channel":
            if not value.isdigit():
                return None
            if int(value) == 0:
                return "0"
            return str(_from_mhz(value)) if _stores_mhz(keys[0], current) else value
        return value
    except ValueError:
        return None


def display(field, value):
    """A value as shown in the UI. Secrets are never shown."""
    if value is None:
        return "–"
    if field.kind == "secret":
        return "Set" if value else "Not set"
    if field.kind == "onoff":
        return {"1": "On", "0": "Off"}.get(value, value)
    if field.kind == "channel":
        return "Auto" if value == "0" else value
    return value if value != "" else "(empty)"


def read(current, mapping):
    """{field key: current value} for every named field."""
    return {f.key: decode(f, current, mapping) for f in FIELDS}


def resolve(fleet, overrides, plain, current, mapping):
    """Work out what the managed settings mean for one router.

    `fleet` and `overrides` hold named-field values (an override wins); `plain` holds
    {portal key: value} for other managed settings. Returns (wanted,
    unsupported): the nvram keys to pin with their values, and the labels of
    managed settings this router has no place for. Unsupported settings are
    never written.
    """
    wanted, unsupported = {}, []
    for field in FIELDS:
        value = overrides.get(field.key, fleet.get(field.key))
        if value is None:
            continue
        written = encode(field, value, current, mapping)
        if written:
            wanted.update(written)
        else:
            unsupported.append(field.label)
    for portal_key, value in plain.items():
        key = mapping.device_key(portal_key)
        if key is not None and key in current:
            wanted[key] = value
        else:
            unsupported.append(portal_key)
    return wanted, unsupported


# --- settings every router shares ---------------------------------------------------


def manageable(portal_key, value=""):
    """Whether a plain setting may be managed fleet-wide."""
    return not (
        portal_key in _OWNED
        or portal_key in nvram.VOLATILE
        or nvram.is_secret(portal_key.replace(".", "_"))
        or _UNSHARED.search(portal_key)
        or "\n" in value
    )


def shared(routers):
    """Plain settings every router has, as {portal key: [value on each router]}.

    `routers` is a list of (current settings, Mapping). Named fields, secrets,
    and per-router or hardware settings are left out.
    """
    if not routers:
        return {}
    tables = [(current, mapping.portal_keys(current)) for current, mapping in routers]
    common = set(tables[0][1])
    for _, keys in tables[1:]:
        common &= set(keys)
    result = {}
    for portal_key in sorted(common):
        values = [current[keys[portal_key]] for current, keys in tables]
        if all(manageable(portal_key, v) for v in values):
            result[portal_key] = values
    return result
