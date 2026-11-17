"""Taking, storing, comparing, and restoring router config backups."""

import asyncio
import hashlib
import time

from . import config, crypto, db, nvram, router

# What each router is in the middle of, for the UI: {device_id: text}.
busy = {}
# Each router's settings as last fetched: {device_id: {name: value}}.
current = {}


def load_settings(backup_id):
    """Decrypt a stored backup and return (raw file, parsed settings)."""
    blob = db.backup_blob(backup_id)
    if blob is None:
        raise LookupError("backup not found")
    raw = crypto.decrypt(blob)
    return raw, nvram.parse(raw)


async def fetch(dev):
    """Return (raw file, parsed settings) for the router's config right now."""
    raw = await router.backup(dev)
    try:
        settings = nvram.parse(raw)
    except nvram.BackupFormatError as exc:
        raise router.RouterError(f"{dev['host']}: {exc}") from None
    if "id" in dev:
        current[dev["id"]] = settings
    return raw, settings


async def take(dev, reason, fetched=None):
    """Back up one router. Returns (backup row, created).

    If no setting differs from the latest stored backup, nothing new is
    stored: that backup is marked as confirmed now and returned with
    created=False. (The file's bytes can differ with identical settings,
    because the router does not write them in a stable order.)
    """
    raw, settings = fetched or await fetch(dev)
    latest = db.latest_backup(dev["id"])
    changes = None
    if latest:
        _, previous = load_settings(latest["id"])
        changes = len(nvram.diff(previous, settings))
        if changes == 0:
            db.confirm_backup(latest["id"])
            return db.get_backup(latest["id"]), False

    backup_id = db.add_backup(
        dev["id"], reason, dev["status"] or {}, hashlib.sha256(raw).hexdigest(), len(raw),
        len(settings), changes, crypto.encrypt(raw), config.BACKUP_KEEP,
    )
    return db.get_backup(backup_id), True


async def _load_into_router(dev, raw, target):
    """Restore `raw` into the router's memory and return any settings that still differ."""
    await router.restore(dev, raw)
    _, current = await fetch(dev)
    # The router's restore skips its hardware-address settings, leaving them
    # unset until the next boot. Same router, same hardware: put them back.
    skipped = {
        c["name"]: target[c["name"]]
        for c in nvram.diff(target, current)
        if c["kind"] == "removed" and c["name"].endswith("hwaddr")
    }
    if skipped:
        await router.set_values(dev, skipped)
        _, current = await fetch(dev)
    return nvram.diff(target, current)


async def restore(dev, backup):
    """Put a stored backup back on its router, save it, and start a reboot.

    Returns once the reboot has been requested. The router's current config
    is backed up first. If the restored settings do not match the backup
    exactly, the previous settings are loaded back and nothing is rebooted.
    """
    raw, target = load_settings(backup["id"])  # parsing proves the file is complete
    before = await fetch(dev)
    await take(dev, "pre-change", fetched=before)

    leftover = await _load_into_router(dev, raw, target)
    if leftover:
        names = ", ".join(c["name"] for c in leftover[:5])
        undo = await _load_into_router(dev, *before)
        if undo:
            raise router.RouterError(
                f"{dev['host']}: the restore did not apply cleanly ({len(leftover)} settings differ: {names}) "
                "and the previous settings could not be put back automatically. Avoid rebooting the router "
                "until this is fixed; the backup taken just before this attempt is in the list."
            )
        raise router.RouterError(
            f"{dev['host']}: the restore did not apply cleanly ({len(leftover)} settings differ: {names}). "
            "The previous settings were put back and the router was not rebooted."
        )
    await router.commit(dev)
    await router.reboot(dev)


async def wait_for_reboot(dev, uptime_before, timeout=420):
    """Wait until the router answers again with a fresh uptime. Returns its status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            status = await router.collect(dev)
        except router.RouterError:
            continue
        if uptime_before is None or status["uptime_s"] < uptime_before:
            return status
    raise router.RouterError(f"{dev['host']}: did not come back within {timeout // 60} minutes of rebooting")
