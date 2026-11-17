"""Status collection: one shell script run on the router, one parser here.

The script must run under DD-WRT's BusyBox ash on every platform, so it avoids
anything beyond POSIX sh (notably `head -N`, which newer builds reject). It
reads only named nvram keys and never emits passphrases or keys.
"""

import re

NVRAM_KEYS = (
    "router_name DD_BOARD os_version lan_ipaddr time_zone "
    "sshd_passwd_auth telnetd_enable syslogd_enable"
)

COLLECT_SCRIPT = r"""
echo "@@nvram"
for k in %s; do echo "$k=$(nvram get $k)"; done
echo "@@release"
grep -i '^Release' /tmp/loginprompt 2>/dev/null
echo "@@kernel"
uname -srm
echo "@@uptime"
cat /proc/uptime
echo "@@loadavg"
cat /proc/loadavg
echo "@@meminfo"
grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached):' /proc/meminfo
echo "@@nvram_usage"
nvram show 2>&1 >/dev/null | tail -n 1
echo "@@radios"
if command -v iw >/dev/null 2>&1; then
  # mac80211 platforms (MediaTek, Atheros): settings live under wlanN_*
  for i in $(iw dev | awk '$1=="Interface"{print $2}' | grep -E '^wlan[0-9]+$' | sort); do
    info="$(iw dev $i info)"
    ch=$(echo "$info" | awk '$1=="channel"{print $2; exit}')
    w=$(echo "$info" | sed -n 's/.*width: \([0-9]*\) MHz.*/\1/p')
    n=$(iw dev $i station dump | grep -c '^Station')
    echo "$i|$ch|$w|$n|$(nvram get ${i}_akm)|$(nvram get ${i}_ssid)"
  done
else
  # Broadcom platforms: settings live under wlN_*, interface is ethN
  for x in 0 1 2; do
    i=$(nvram get wl${x}_ifname)
    [ -n "$i" ] || continue
    [ "$(nvram get wl${x}_radio)" = "1" ] || continue
    ch=$(wl -i $i chanspec 2>/dev/null | sed 's/[^0-9].*//')
    n=$(wl -i $i assoclist 2>/dev/null | grep -c .)
    echo "wl$x|$ch|$(nvram get wl${x}_nbw)|$n|$(nvram get wl${x}_akm)|$(nvram get wl${x}_ssid)"
  done
fi
echo "@@stations"
# One line per connected Wi-Fi client: radio|mac|signal dBm|tx Mbit/s|seconds connected
if command -v iw >/dev/null 2>&1; then
  for i in $(iw dev | awk '$1=="Interface"{print $2}' | grep -E '^wlan[0-9]+$' | sort); do
    iw dev $i station dump | awk -v r=$i '
      function out() { if (m) print r "|" m "|" s "|" t "|" c }
      $1=="Station" { out(); m=$2; s=""; t=""; c="" }
      $1=="signal:" { s=$2 }
      $1=="tx" && $2=="bitrate:" { t=$3 }
      $1=="connected" && $2=="time:" { c=$3 }
      END { out() }'
  done
else
  for x in 0 1 2; do
    i=$(nvram get wl${x}_ifname)
    [ -n "$i" ] || continue
    for m in $(wl -i $i assoclist 2>/dev/null | awk '{print $2}'); do
      echo "wl$x|$m|$(wl -i $i rssi $m 2>/dev/null)||"
    done
  done
fi
echo "@@end"
""" % NVRAM_KEYS


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line)
    return sections


def parse(text):
    """Turn the collect script's output into a status dict."""
    s = _sections(text)
    if "end" not in s:
        raise ValueError("incomplete status output from router")

    nv = dict(line.split("=", 1) for line in s.get("nvram", []) if "=" in line)

    mem = {}
    for line in s.get("meminfo", []):
        key, _, rest = line.partition(":")
        mem[key] = _int(rest.split()[0]) if rest.split() else None
    mem_total = mem.get("MemTotal")
    mem_avail = mem.get("MemAvailable")
    if mem_avail is None and mem.get("MemFree") is not None:
        mem_avail = mem["MemFree"] + (mem.get("Buffers") or 0) + (mem.get("Cached") or 0)

    nvram_used = nvram_free = None
    usage = re.search(r"size:\s*(\d+)\s*bytes\s*\((\d+)\s*left\)", " ".join(s.get("nvram_usage", [])))
    if usage:
        nvram_used, nvram_free = int(usage.group(1)), int(usage.group(2))

    radios = []
    for line in s.get("radios", []):
        parts = line.split("|", 5)
        if len(parts) != 6:
            continue
        name, channel, width, clients, security, ssid = parts
        channel = _int(channel)
        radios.append(
            {
                "name": name,
                "ssid": ssid,
                "channel": channel,
                "band": None if channel is None else ("2.4 GHz" if channel <= 14 else "5 GHz"),
                "width_mhz": _int(width),
                "clients": _int(clients, 0),
                "security": security,
            }
        )
    radios.sort(key=lambda r: (r["channel"] is None, r["channel"] or 0))

    bands = {r["name"]: r["band"] for r in radios}
    stations = []
    for line in s.get("stations", []):
        parts = line.split("|")
        if len(parts) != 5 or not re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", parts[1]):
            continue
        radio, mac, signal, rate, connected = parts
        try:
            rate = float(rate)
        except ValueError:
            rate = None
        stations.append(
            {
                "mac": mac.lower(),
                "radio": radio,
                "band": bands.get(radio),
                "signal": _int(signal),
                "rate_mbps": rate,
                "connected_s": _int(connected),
            }
        )

    release = " ".join(s.get("release", []))
    uptime = s.get("uptime", ["0"])[0].split()
    load = s.get("loadavg", [""])[0].split()

    return {
        "name": nv.get("router_name") or None,
        "model": nv.get("DD_BOARD") or None,
        "build": _int(nv.get("os_version")),
        "release": release.split(":", 1)[1].strip() if ":" in release else None,
        "kernel": " ".join(s.get("kernel", [])) or None,
        "lan_ip": nv.get("lan_ipaddr") or None,
        "time_zone": nv.get("time_zone") or None,
        "uptime_s": int(float(uptime[0])) if uptime else None,
        "load1": float(load[0]) if load else None,
        "mem_total_kb": mem_total,
        "mem_avail_kb": mem_avail,
        "nvram_used": nvram_used,
        "nvram_free": nvram_free,
        "radios": radios,
        "clients": sum(r["clients"] for r in radios),
        "stations": stations,
        "telnet_enabled": nv.get("telnetd_enable") == "1",
        "ssh_password_auth": nv.get("sshd_passwd_auth") == "1",
        "remote_syslog": nv.get("syslogd_enable") == "1",
    }


if __name__ == "__main__":
    print(COLLECT_SCRIPT)
