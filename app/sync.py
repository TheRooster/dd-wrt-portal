"""Managed settings: storage, per-router mapping, drift detection, and applying."""

from dataclasses import dataclass

from . import backups, crypto, db, nvram, profile, router

FLEET = "fleet"  # named fields shared by every router
PLAIN = "plain"  # other managed settings, by portal key
FLAGS = "flags"
SAME_PASSPHRASE = "passphrase_24_same"  # 2.4 GHz uses the 5 GHz passphrase
RADIO = "radio:"  # keymap rows "radio:2.4" / "radio:5" hold a radio prefix


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


def stored_fleet():
    """Named fleet values exactly as saved."""
    return _load(FLEET)


def fleet():
    """Named fleet values in effect, with "same as 5 GHz" filled in."""
    values = _load(FLEET)
    if same_passphrase():
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
    return db.get_managed(PLAIN)


def save_plain(values):
    db.set_managed(PLAIN, values)


def overrides(device_id):
    return _load(_scope(device_id))


def save_overrides(device_id, values):
    _save(_scope(device_id), values)


def anything_managed(devices):
    return bool(_load(FLEET) or plain() or any(overrides(d["id"]) for d in devices))


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
    wanted, unsupported = profile.resolve(
        fleet(), overrides(dev["id"]), plain(), settings, mapping(dev, settings)
    )
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
