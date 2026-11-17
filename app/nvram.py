"""DD-WRT `nvram backup` files: parsing, secret detection, and diffing.

File layout: the magic "DD-WRT", a little-endian u16 entry count, then for
each entry a u8 name length, the name, a little-endian u16 value length, and
the value.
"""

import re
import struct

MAGIC = b"DD-WRT"

# Settings whose values are credentials or private keys. Their values are
# never rendered; the UI only says whether they are set or changed.
_SECRET = re.compile(
    r"(passw(or)?d$|passphrase|_pass$|psk\d*$|_key\d*$|wep_buf|secret|private|privkey|_pw$"
    r"|_tlsauth$|_static$|_ca$|_crt$|_client$|^http_username$)",
    re.IGNORECASE,
)


# Bookkeeping the firmware rewrites on every boot (last start and shutdown
# times). Not configuration, so never reported as a change.
VOLATILE = frozenset({"start_time", "end_time"})


class BackupFormatError(ValueError):
    pass


def parse(raw):
    """Return {name: value} from a backup file, or raise BackupFormatError."""
    if not raw.startswith(MAGIC) or len(raw) < 8:
        raise BackupFormatError("not a DD-WRT nvram backup")
    (count,) = struct.unpack_from("<H", raw, 6)
    pos, settings = 8, {}
    try:
        for _ in range(count):
            name_len = raw[pos]
            name = raw[pos + 1 : pos + 1 + name_len].decode("utf-8", "replace")
            pos += 1 + name_len
            (value_len,) = struct.unpack_from("<H", raw, pos)
            pos += 2
            if pos + value_len > len(raw):
                raise BackupFormatError("backup file is truncated")
            settings[name] = raw[pos : pos + value_len].decode("utf-8", "replace")
            pos += value_len
    except (IndexError, struct.error):
        raise BackupFormatError("backup file is truncated") from None
    if pos != len(raw):
        raise BackupFormatError("backup file has trailing data")
    return settings


def is_secret(name):
    return bool(_SECRET.search(name))


def _shown(name, value):
    if value is None:
        return None
    if is_secret(name):
        return "(hidden)" if value else ""
    return value


def diff(old, new):
    """List the changes from `old` to `new`, sorted by setting name.

    Each change is a dict with name, kind (added/removed/changed), old, new,
    and secret. Secret values are replaced with "(hidden)". Settings in
    VOLATILE are ignored.
    """
    changes = []
    for name in sorted((old.keys() | new.keys()) - VOLATILE):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        kind = "added" if a is None else "removed" if b is None else "changed"
        changes.append(
            {
                "name": name,
                "kind": kind,
                "old": _shown(name, a),
                "new": _shown(name, b),
                "secret": is_secret(name),
            }
        )
    return changes


def listing(settings):
    """All settings as (name, displayed value, secret) rows, sorted by name."""
    return [(name, _shown(name, settings[name]), is_secret(name)) for name in sorted(settings)]
