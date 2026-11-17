"""Channel planning: survey the neighbourhood from each router, then pick channels.

Each router lists the networks it can hear (DD-WRT's `site_survey`). A channel
is scored by the networks on or overlapping it, weighted by signal strength.
The plan gives each router the channel with the least interference while
keeping the fleet's own routers apart.
"""

import itertools
import json
import re
import time

from . import db

# `site_survey` scans the radio named in the `wifi_display` setting, which the
# router's own web page sets the same way. It is changed in memory only and
# put back afterwards; nothing is saved to flash.
SURVEY_SCRIPT = r"""
old=$(nvram get wifi_display)
if command -v iw >/dev/null 2>&1; then
  for i in $(iw dev | awk '$1=="Interface"{print $2}' | grep -E '^wlan[0-9]+$' | sort); do
    echo "@@radio $i $(cat /sys/class/net/$i/address 2>/dev/null)"
    nvram set wifi_display=$i
    site_survey 2>&1
  done
  echo "@@allowed"
  iw phy | awk '/MHz \[/ && !/disabled/ {gsub(/[^0-9]/, "", $4); print $4}'
else
  for x in 0 1 2; do
    i=$(nvram get wl${x}_ifname)
    [ -n "$i" ] || continue
    [ "$(nvram get wl${x}_radio)" = "1" ] || continue
    echo "@@radio wl$x $(cat /sys/class/net/$i/address 2>/dev/null)"
    nvram set wifi_display=wl$x
    site_survey 2>&1
    echo "@@channels $(wl -i $i channels 2>/dev/null)"
  done
fi
nvram set wifi_display="$old"
echo "@@end"
"""

_NETWORK = re.compile(
    r"^\[\s*\d+\] SSID\[(.*)\] BSSID\[([0-9A-Fa-f:]{17})\] channel\[\s*(\d+)\].*?rssi\[\s*(-?\d+)\]"
)
DFS = set(range(52, 145))
# 5 GHz channels grouped the way wider channels bond them.
_BLOCKS_80 = [(36, 40, 44, 48), (52, 56, 60, 64), (100, 104, 108, 112), (116, 120, 124, 128),
              (132, 136, 140, 144), (149, 153, 157, 161)]
SAME_CHANNEL_DEFAULT = -70  # assumed signal between two of our routers that have no survey of each other


def parse_survey(text):
    """{radio name: {bssid, networks: [{ssid, bssid, channel, rssi}], allowed: [channels]}}."""
    if "@@end" not in text:
        raise ValueError("incomplete survey output from router")
    radios, current, everywhere = {}, None, []
    section = None
    for line in text.splitlines():
        if line.startswith("@@radio "):
            parts = line.split()
            current = radios.setdefault(parts[1], {"bssid": (parts[2] if len(parts) > 2 else "").lower(), "networks": [], "allowed": []})
            section = "radio"
        elif line.startswith("@@channels") and current is not None:
            current["allowed"] = [int(c) for c in line.split()[1:] if c.isdigit()]
        elif line.startswith("@@allowed"):
            section = "allowed"
        elif line.startswith("@@"):
            section = None
        elif section == "allowed" and line.strip().isdigit():
            everywhere.append(int(line))
        elif section == "radio":
            m = _NETWORK.match(line)
            if m:
                current["networks"].append(
                    {"ssid": m.group(1), "bssid": m.group(2).lower(), "channel": int(m.group(3)), "rssi": int(m.group(4))}
                )
    for radio in radios.values():
        radio["allowed"] = radio["allowed"] or everywhere
    return radios


def save(device_id, radios):
    db.set_kv(f"survey:{device_id}", json.dumps({"at": time.time(), "radios": radios}))


def load(device_id):
    raw = db.get_kv(f"survey:{device_id}")
    return json.loads(raw) if raw else None


# --- scoring ----------------------------------------------------------------


def weight(rssi):
    """How much a network at this signal strength counts: 1 at the edge of hearing, about 55 next door."""
    return max(1, rssi + 95)


def covers(channel, width):
    """The 20 MHz channels a 5 GHz radio occupies at this width."""
    if width in (40, 80, 160):
        for block in _BLOCKS_80:
            if channel in block:
                if width == 40:
                    half = block.index(channel) // 2 * 2
                    return set(block[half:half + 2])
                return set(block)
    return {channel}


def overlap(a, b, width=20):
    """0..1: how much a radio on channel `a` is disturbed by a network on channel `b`."""
    if a <= 14 and b <= 14:
        return max(0.0, 1 - abs(a - b) / 5)
    if a > 14 and b > 14:
        return 1.0 if b in covers(a, width) else 0.0
    return 0.0


def candidates(band, width, allowed):
    """Channels worth considering: non-overlapping on 2.4 GHz, non-radar block starts on 5 GHz."""
    if band == "2.4 GHz":
        base = [1, 6, 11]
    else:
        base = {20: [36, 40, 44, 48, 149, 153, 157, 161], 40: [36, 44, 149, 157]}.get(width, [36, 149])
    return [c for c in base if not allowed or c in allowed] or base[:1]


def interference(networks, channel, width, ours):
    """(score, number of networks) from neighbours on or overlapping `channel`. `ours` are our own BSSIDs."""
    score, count = 0.0, 0
    for n in networks:
        o = overlap(channel, n["channel"], width)
        if o and n["bssid"] not in ours:
            score += o * weight(n["rssi"])
            count += 1
    return round(score, 1), count


def plan(radios):
    """Choose a channel for each radio of one band.

    `radios`: [{key, channel, width, bssid, networks, allowed, band}]. Returns
    {key: {recommended, options: [{channel, score, networks}], current_score}}.
    Every combination is tried; the cost is each radio's neighbour interference
    plus a penalty for two of our radios overlapping, scaled by how well they
    hear each other.
    """
    if not radios:
        return {}
    ours = {r["bssid"] for r in radios if r["bssid"]}
    heard = {}  # (key, other bssid) -> rssi
    for r in radios:
        for n in r["networks"]:
            heard[(r["key"], n["bssid"])] = n["rssi"]
    options = {}
    for r in radios:
        wanted = candidates(r["band"], r["width"], r["allowed"])
        if r["band"] == "5 GHz" and r["channel"] and r["channel"] not in wanted:
            wanted.append(r["channel"])  # a radar channel already in use may stay if it is the quietest
        options[r["key"]] = {c: interference(r["networks"], c, r["width"], ours) for c in wanted}

    def between(a, b):
        known = [v for v in (heard.get((a["key"], b["bssid"])), heard.get((b["key"], a["bssid"]))) if v is not None]
        return weight(max(known) if known else SAME_CHANNEL_DEFAULT)

    best, best_cost = None, None
    for combo in itertools.product(*(options[r["key"]] for r in radios)):
        cost = sum(options[r["key"]][c][0] for r, c in zip(radios, combo))
        for (a, ca), (b, cb) in itertools.combinations(zip(radios, combo), 2):
            cost += between(a, b) * max(overlap(ca, cb, a["width"]), overlap(cb, ca, b["width"]))
        cost -= 0.5 * sum(c == r["channel"] for r, c in zip(radios, combo))  # prefer not to move
        if best_cost is None or cost < best_cost:
            best, best_cost = combo, cost
    return {
        r["key"]: {
            "recommended": c,
            "options": [{"channel": ch, "score": s, "networks": n} for ch, (s, n) in options[r["key"]].items()],
            "current": interference(r["networks"], r["channel"], r["width"], ours) if r["channel"] else (None, None),
        }
        for r, c in zip(radios, best)
    }
