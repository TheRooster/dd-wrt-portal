"""Managed settings: storage, per-router mapping, drift detection, and applying."""

from dataclasses import dataclass

from . import backups, crypto, db, nvram, profile, router

FLEET = "fleet"  # named fields shared by every router
PLAIN = "plain"  # other managed settings, by portal key
FLAGS = "flags"
SAME_PASSPHRASE = "passphrase_24_same"  # 2.4 GHz uses the 5 GHz passphrase
RADIO = "radio:"  # keymap rows "radio:2.4" / "radio:5" hold a radio prefix
UNIQUE = "unique"  # {"field:<key>" or "plain:<portal key>": "1" unique per router, "0" shared}


def _device_plain_scope(device_id):
    return f"deviceplain:{device_id}"


def _scope(device_id):
    return f"device:{device_id}"


def _load(scope):
    values = {}
    for key, value in db.get_managed(scope).items():
        field = profile.BY_KEY.get(key)
        if field:
            values[key] = crypto.decrypt(value.encode()).decode() if field.kind == "secret" else value
    return values


def _save(scope, values):
    stored = {}
    for key, value in values.items():
        secret = profile.BY_KEY[key].kind == "secret"
        stored[key] = crypto.encrypt(value.encode()).decode() if secret else value
    db.set_managed(scope, stored)


def migrate():
    """Carry settings saved by earlier versions forward."""
    old = db.get_managed(FLEET)
    if "wifi_passphrase" in old:  # one passphrase for both bands
        old["passphrase_5"] = old.pop("wifi_passphrase")
        db.set_managed(FLEET, old)
        db.set_managed(FLAGS, db.get_managed(FLAGS) | {SAME_PASSPHRASE: "1"})


def same_passphrase():
    return db.get_managed(FLAGS).get(SAME_PASSPHRASE) == "1"


# --- which settings are unique per router -------------------------------------------


def unique_fields():
    """Named fields set per router. Channels are, until someone says otherwise."""
    stored = db.get_managed(UNIQUE)
    return {
        f.key for f in profile.FIELDS
        if stored.get(f"field:{f.key}", "1" if f.per_device else "0") == "1"
    }


def unique_plain():
    """Plain settings set per router."""
    return {k[len("plain:"):] for k, v in db.get_managed(UNIQUE).items() if k.startswith("plain:") and v == "1"}


def _save_unique(fields, plain_keys):
    rows = {f"field:{f.key}": "1" if f.key in fields else "0" for f in profile.FIELDS}
    rows |= {f"plain:{k}": "1" for k in plain_keys}
    db.set_managed(UNIQUE, rows)


def set_unique(fields, plain_keys, devices):
    """Make exactly `fields` and `plain_keys` unique per router; returns notes for the user.

    A setting that becomes unique starts with the shared value on every
    router, so nothing changes in effect. One that becomes shared keeps the
    routers' value if they all agree, and is left unmanaged if they do not.
    """
    fields, plain_keys = set(fields), set(plain_keys)
    was_fields, was_plain = unique_fields(), unique_plain()
    notes = []
    shared_named, same = _load(FLEET), same_passphrase()
    shared_plain = plain()
    per_named = {d["id"]: overrides(d["id"]) for d in devices}
    per_plain = {d["id"]: device_plain(d["id"]) for d in devices}

    for key in fields - was_fields:
        value = shared_named.pop(key, None)
        if key == "passphrase_24" and same:
            value, same = shared_named.get("passphrase_5"), False
        for values in per_named.values():
            values.pop(key, None)
            if value is not None:
                values[key] = value
    for key in was_fields - fields:
        found = {v[key] for v in per_named.values() if key in v}
        for values in per_named.values():
            values.pop(key, None)
        if len(found) == 1:
            shared_named[key] = found.pop()
        elif found:
            notes.append(f"{profile.BY_KEY[key].label} differed between routers, so it is no longer managed.")
    for key in plain_keys - was_plain:
        value = shared_plain.pop(key, None)
        for values in per_plain.values():
            values.pop(key, None)
            if value is not None:
                values[key] = value
    for key in was_plain - plain_keys:
        found = {v[key] for v in per_plain.values() if key in v}
        for values in per_plain.values():
            values.pop(key, None)
        if len(found) == 1:
            shared_plain[key] = found.pop()
        else:
            notes.append(f"{key} differed between routers, so it is no longer managed.")

    save_fleet(shared_named, same)
    save_plain(shared_plain)
    for d in devices:
        save_overrides(d["id"], per_named[d["id"]])
        save_device_plain(d["id"], per_plain[d["id"]])
    _save_unique(fields, plain_keys)
    return notes


def stop_unique_plain(key, devices):
    """Stop managing a per-router plain setting on every router."""
    for d in devices:
        values = device_plain(d["id"])
        if values.pop(key, None) is not None:
            save_device_plain(d["id"], values)
    _save_unique(unique_fields(), unique_plain() - {key})


def stored_fleet():
    """Shared named values exactly as saved."""
    unique = unique_fields()
    return {k: v for k, v in _load(FLEET).items() if k not in unique}


def named(device_id):
    """The named values in effect on one router: shared ones, its own unique ones,
    and "same as 5 GHz" filled in."""
    unique = unique_fields()
    values = {k: v for k, v in _load(FLEET).items() if k not in unique}
    values |= {k: v for k, v in overrides(device_id).items() if k in unique}
    if same_passphrase() and "passphrase_24" not in unique:
        values.pop("passphrase_24", None)
        if "passphrase_5" in values:
            values["passphrase_24"] = values["passphrase_5"]
    return values


def save_fleet(values, same):
    _save(FLEET, values)
    flags = db.get_managed(FLAGS)
    flags[SAME_PASSPHRASE] = "1" if same else "0"
    db.set_managed(FLAGS, flags)


def plain():
    """Shared plain values."""
    unique = unique_plain()
    return {k: v for k, v in db.get_managed(PLAIN).items() if k not in unique}


def device_plain(device_id):
    """One router's own values for plain settings that are unique per router."""
    unique = unique_plain()
    return {k: v for k, v in db.get_managed(_device_plain_scope(device_id)).items() if k in unique}


def save_device_plain(device_id, values):
    db.set_managed(_device_plain_scope(device_id), values)


def plain_keys():
    """Every plain setting the portal manages, shared or unique."""
    return sorted(set(plain()) | unique_plain())


def plain_for(device_id):
    """The plain values in effect on one router."""
    return plain() | device_plain(device_id)


def save_plain(values):
    db.set_managed(PLAIN, values)


def overrides(device_id):
    return _load(_scope(device_id))


def save_overrides(device_id, values):
    _save(_scope(device_id), values)


def anything_managed(devices):
    return bool(stored_fleet() or plain() or any(named(d["id"]) or device_plain(d["id"]) for d in devices))


# --- per-router state -------------------------------------------------------------


def current(dev):
    """The router's settings as last seen: from memory, else from its latest backup."""
    settings = backups.current.get(dev["id"])
    if settings is None:
        latest = db.latest_backup(dev["id"])
        if not latest:
            return None
        _, settings = backups.load_settings(latest["id"])
        backups.current[dev["id"]] = settings
    return settings


def mapping(dev, settings):
    """The router's saved mapping; radios are auto-detected until one is saved."""
    rows = db.get_keymap(dev["id"])
    radios = {k[len(RADIO):]: v for k, v in rows.items() if k.startswith(RADIO) and v}
    if not any(k.startswith(RADIO) for k in rows):
        radios = profile.detect_radios(settings, dev.get("status"))
    aliases = {k: v for k, v in rows.items() if not k.startswith(RADIO)}
    return profile.Mapping(radios, aliases)


def save_mapping(device_id, radios, aliases):
    rows = {f"{RADIO}{band}": radios.get(band, "") for band in profile.BANDS}
    db.set_keymap(device_id, rows | aliases)


@dataclass
class Plan:
    wanted: dict  # {nvram key: value} the managed settings pin on this router
    changes: list  # what applying would change, in nvram.diff's shape
    unsupported: list  # labels of managed settings this router has no place for


def plan(dev, settings=None):
    """What the managed settings mean for this router; None if its settings are unknown."""
    settings = settings if settings is not None else current(dev)
    if settings is None:
        return None
    wanted, unsupported = profile.resolve(named(dev["id"]), {}, plain_for(dev["id"]), settings, mapping(dev, settings))
    return Plan(wanted, nvram.diff({k: settings[k] for k in wanted}, wanted), unsupported)


async def apply(dev):
    """Write the managed settings to the router, save them, and start a reboot.

    Returns how many settings were changed; 0 means the router already
    matched and nothing was touched. The current config is backed up first.
    If the router does not show the new values after they are written, the
    old values are put back, nothing is saved, and the router is not rebooted.
    """
    fetched = await backups.fetch(dev)
    before = fetched[1]
    wanted = plan(dev, before).wanted
    new = {k: v for k, v in wanted.items() if before[k] != v}
    if not new:
        return 0
    await backups.take(dev, "pre-change", fetched=fetched)

    await router.set_values(dev, new)
    _, after = await backups.fetch(dev)
    wrong = [k for k, v in new.items() if after.get(k) != v]
    if wrong:
        await router.set_values(dev, {k: before[k] for k in new})
        raise router.RouterError(
            f"{dev['host']}: the router did not accept {len(wrong)} setting(s) ({', '.join(wrong[:5])}). "
            "The old values were put back and the router was not rebooted."
        )
    await router.commit(dev)
    await router.reboot(dev)
    return len(new)
