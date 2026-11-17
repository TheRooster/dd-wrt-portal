"""Web app: dashboard, enrolment, backups, and the background poller."""

import asyncio
import ipaddress
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import backups, config, db, nvram, profile, router, sync

log = logging.getLogger("portal")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")

REASONS = {
    "scheduled": "Automatic",
    "manual": "Manual",
    "enrolled": "On enrolment",
    "pre-change": "Before a change",
    "post-restore": "After a restore",
    "post-change": "After a change",
}
_tasks = set()  # keeps background jobs referenced until they finish


def _duration(seconds):
    if seconds is None:
        return "–"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _ago(ts):
    return "never" if not ts else f"{_duration(time.time() - ts)} ago"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


templates.env.filters["duration"] = _duration
templates.env.filters["ago"] = _ago
templates.env.filters["iso"] = _iso
templates.env.globals["reasons"] = REASONS


# --- polling and scheduled backups -----------------------------------------


async def poll_device(dev):
    try:
        status = await router.collect(dev)
    except router.RouterError as exc:
        db.record_poll(dev["id"], error=str(exc))
        return False
    db.record_poll(dev["id"], status=status)
    return True


async def poll_all():
    devices = [d for d in db.list_devices() if d["id"] not in backups.busy]
    results = await asyncio.gather(*(poll_device(d) for d in devices))
    return [d["id"] for d, ok in zip(devices, results) if ok]


_last_check = {}  # {device_id: time of the last full config check}


async def check_configs(device_ids):
    """Fetch each reachable router's full config when its last check is stale.

    A backup is stored if anything changed, so edits made outside the portal
    show up in the history within CONFIG_INTERVAL.
    """
    now = time.time()
    for device_id in device_ids:
        if device_id in backups.busy or now - _last_check.get(device_id, 0) < config.CONFIG_INTERVAL:
            continue
        dev = db.get_device(device_id)
        try:
            fetched = await backups.fetch(dev)
            _, created = await backups.take(dev, "scheduled", fetched=fetched)
            _last_check[device_id] = now
            if created:
                log.info("config of %s changed; backup stored", dev["host"])
        except (router.RouterError, ValueError) as exc:
            log.warning("config check failed: %s", exc)


async def _poller():
    while True:
        try:
            await check_configs(await poll_all())
        except Exception:
            log.exception("poll cycle failed")
        await asyncio.sleep(config.POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    db.init()
    sync.migrate()
    router.portal_key()
    task = asyncio.create_task(_poller())
    yield
    task.cancel()


app = FastAPI(title="DD-WRT Portal", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Refuse state-changing requests that another website sent the browser to make."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("cross-origin request refused", status_code=403)
    return await call_next(request)


def _context():
    devices = db.list_devices()
    summary = db.backup_summary()
    now = time.time()
    managed = sync.anything_managed(devices)
    for d in devices:
        stale = not d["last_ok"] or now - d["last_ok"] > 3 * config.POLL_INTERVAL
        d["online"] = bool(d["status"]) and not d["error"] and not stale
        d["backups"] = summary.get(d["id"])
        d["busy"] = backups.busy.get(d["id"])
        plan = sync.plan(d) if managed else None
        d["drift"] = None if plan is None else len(plan.changes)
        d["unsupported"] = plan.unsupported if plan else []
    return {
        "devices": devices,
        "managed": managed,
        "poll_interval": config.POLL_INTERVAL,
        "online": sum(d["online"] for d in devices),
        "clients": sum(d["status"]["clients"] for d in devices if d["online"]),
        "builds": sorted({d["status"]["build"] for d in devices if d["status"] and d["status"]["build"]}),
    }


def _back(to="/", **message):
    return RedirectResponse(to + ("?" + urlencode(message) if message else ""), status_code=303)


def _device_or_404(device_id):
    dev = db.get_device(device_id)
    if not dev:
        raise HTTPException(404, "No such router")
    return dev


def _label(dev):
    return (dev["status"] or {}).get("name") or dev["host"]


# --- dashboard and enrolment ------------------------------------------------


@app.get("/")
async def index(request: Request, msg: str = "", err: str = ""):
    ctx = _context() | {"msg": msg, "err": err, "public_key": router.public_key()}
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/partials/devices")
async def devices_partial(request: Request):
    return templates.TemplateResponse(request, "_devices.html", _context())


@app.post("/devices")
async def add_device(host: str = Form(...), password: str = Form(...)):
    host = host.strip()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return _back(err=f"'{host}' is not an IP address")
    try:
        host_key, fingerprint = await router.enroll(host, password)
        status = await router.collect({"host": host, "port": 22, "host_key": host_key})
    except router.RouterError as exc:
        return _back(err=str(exc))
    finally:
        del password
    device_id = db.upsert_device(host, 22, host_key, fingerprint)
    db.record_poll(device_id, status=status)
    note = ""
    try:
        await backups.take(db.get_device(device_id), "enrolled")
    except (router.RouterError, ValueError) as exc:
        note = f" The first backup failed: {exc}"
    message = f"Enrolled {status['name'] or host}. Host key {fingerprint}.{note}"
    if not db.get_device(device_id)["mapped"]:
        return _back(f"/devices/{device_id}/mapping", msg=message + " Next, confirm how its settings map to the portal's.")
    return _back(msg=message)


@app.post("/devices/{device_id}/delete")
async def remove_device(device_id: int, force: str = Form("")):
    """Take the portal's key off the router, then forget the router.

    If the key cannot be removed the router stays enrolled, unless `force` is
    set (offered only for routers that are offline).
    """
    dev = db.get_device(device_id)
    if not dev:
        return _back()
    if device_id in backups.busy:
        return _back(err=f"{_label(dev)} is busy: {backups.busy[device_id]}")
    if force:
        db.delete_device(device_id)
        return _back(
            msg=f"Removed {_label(dev)} and its backups from the portal. "
            "The portal's key is still installed on the router."
        )
    try:
        await router.unenroll(dev)
    except router.RouterError as exc:
        return _back(err=f"{_label(dev)} was not removed, because the portal's key could not be taken off it. {exc}")
    db.delete_device(device_id)
    return _back(msg=f"Removed the portal's key from {_label(dev)}, then removed the router and its backups from the portal.")


@app.post("/poll")
async def poll_now():
    await poll_all()
    return _back()


# --- backups ------------------------------------------------------------------


@app.get("/devices/{device_id}/backups")
async def backups_page(request: Request, device_id: int, msg: str = "", err: str = ""):
    dev = _device_or_404(device_id)
    ctx = {
        "dev": dev,
        "label": _label(dev),
        "backups": db.list_backups(device_id),
        "events": db.list_events(device_id),
        "busy": backups.busy.get(device_id),
        "msg": msg,
        "err": err,
    }
    return templates.TemplateResponse(request, "backups.html", ctx)


@app.post("/devices/{device_id}/backups")
async def backup_now(device_id: int):
    dev = _device_or_404(device_id)
    here = f"/devices/{device_id}/backups"
    if device_id in backups.busy:
        return _back(here, err=f"{_label(dev)} is busy: {backups.busy[device_id]}")
    try:
        backup, created = await backups.take(dev, "manual")
    except (router.RouterError, ValueError) as exc:
        return _back(here, err=str(exc))
    if created:
        return _back(here, msg="Backup stored.")
    return _back(here, msg="No settings have changed since the latest backup, so nothing new was stored.")


def _backup_or_404(backup_id):
    backup = db.get_backup(backup_id)
    if not backup:
        raise HTTPException(404, "No such backup")
    return backup


@app.get("/backups/{backup_id}")
async def backup_detail(request: Request, backup_id: int):
    backup = _backup_or_404(backup_id)
    dev = _device_or_404(backup["device_id"])
    _, settings = backups.load_settings(backup_id)
    previous = db.previous_backup(backup)
    changes = None
    if previous:
        _, before = backups.load_settings(previous["id"])
        changes = nvram.diff(before, settings)
    ctx = {
        "dev": dev,
        "label": backup["name"] or _label(dev),
        "backup": backup,
        "previous": previous,
        "changes": changes,
        "settings": nvram.listing(settings),
    }
    return templates.TemplateResponse(request, "backup.html", ctx)


@app.get("/backups/{backup_id}/download")
async def backup_download(backup_id: int):
    backup = _backup_or_404(backup_id)
    raw, _ = backups.load_settings(backup_id)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", backup["name"] or f"router-{backup['device_id']}")
    stamp = datetime.fromtimestamp(backup["taken_at"], timezone.utc).strftime("%Y%m%d-%H%MZ")
    filename = f"nvram-{name}-r{backup['build']}-{stamp}.bin"
    return Response(
        raw,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@app.get("/backups/{backup_id}/restore")
async def restore_confirm(request: Request, backup_id: int):
    """Show what restoring would change, using the router's settings as they are right now."""
    backup = _backup_or_404(backup_id)
    dev = _device_or_404(backup["device_id"])
    here = f"/devices/{dev['id']}/backups"
    if dev["id"] in backups.busy:
        return _back(here, err=f"{_label(dev)} is busy: {backups.busy[dev['id']]}")
    try:
        status = await router.collect(dev)
        _, current = await backups.fetch(dev)
    except router.RouterError as exc:
        return _back(here, err=f"Cannot restore right now. {exc}")
    _, target = backups.load_settings(backup_id)
    ctx = {
        "dev": dev,
        "label": status["name"] or dev["host"],
        "backup": backup,
        "status": status,
        "changes": nvram.diff(current, target),
        "wrong_model": bool(backup["model"]) and backup["model"] != status["model"],
        "other_build": backup["build"] != status["build"],
    }
    return templates.TemplateResponse(request, "restore.html", ctx)


async def _after_restore(dev, backup, uptime_before, started):
    """Background half of a restore: wait for the reboot, then check the result."""
    device_id = dev["id"]
    try:
        status = await backups.wait_for_reboot(dev, uptime_before)
        db.record_poll(device_id, status=status)
        _, target = backups.load_settings(backup["id"])
        fetched = await backups.fetch(dev)
        differ = nvram.diff(target, fetched[1])
        await backups.take(db.get_device(device_id), "post-restore", fetched=fetched)
        took = int(time.time() - started)
        if differ:
            names = ", ".join(c["name"] for c in differ[:5])
            db.add_event(
                device_id,
                f"Restored the backup from {_when(backup['taken_at'])}; back online after {took}s. "
                f"{len(differ)} setting{'s' if len(differ) != 1 else ''} differ from the backup after the reboot: {names}.",
            )
        else:
            db.add_event(
                device_id,
                f"Restored the backup from {_when(backup['taken_at'])}; back online after {took}s "
                "with every setting matching the backup.",
            )
    except (router.RouterError, ValueError) as exc:
        db.add_event(device_id, f"Restore of the backup from {_when(backup['taken_at'])}: {exc}", ok=False)
    except Exception:
        log.exception("restore follow-up failed")
        db.add_event(device_id, "Restore follow-up failed unexpectedly; see the portal log.", ok=False)
    finally:
        backups.busy.pop(device_id, None)


@app.post("/backups/{backup_id}/restore")
async def restore_backup(backup_id: int):
    backup = _backup_or_404(backup_id)
    dev = _device_or_404(backup["device_id"])
    device_id = dev["id"]
    here = f"/devices/{device_id}/backups"
    if device_id in backups.busy:
        return _back(here, err=f"{_label(dev)} is busy: {backups.busy[device_id]}")
    backups.busy[device_id] = "Restoring"
    started = time.time()
    try:
        status = await router.collect(dev)
        if backup["model"] and backup["model"] != status["model"]:
            raise router.RouterError(f"This backup is from a {backup['model']}, not a {status['model']}.")
        await backups.restore(dev, backup)
    except (router.RouterError, ValueError) as exc:
        backups.busy.pop(device_id, None)
        db.add_event(device_id, f"Restore of the backup from {_when(backup['taken_at'])} failed: {exc}", ok=False)
        return _back(here, err=str(exc))
    except BaseException:
        backups.busy.pop(device_id, None)
        raise
    backups.busy[device_id] = "Rebooting"
    task = asyncio.create_task(_after_restore(dev, backup, status["uptime_s"], started))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return _back(here, msg="Backup restored and saved. The router is rebooting; this page updates when it is back.")


# --- managed settings -----------------------------------------------------------


def _spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _known(devices):
    """[(device, its settings, its mapping)] for routers whose settings are known."""
    known = []
    for d in devices:
        settings = sync.current(d)
        if settings is not None:
            known.append((d, settings, sync.mapping(d, settings)))
    return known


def _shared(known):
    """Plain settings every router has and the portal is not managing yet."""
    managed = sync.plain()
    table = profile.shared([(settings, mapping) for _, settings, mapping in known])
    return {k: v for k, v in table.items() if k not in managed}


@app.get("/settings")
async def settings_page(request: Request, msg: str = "", err: str = ""):
    ctx = _context()
    plain = sync.plain()
    for d in ctx["devices"]:
        d["read"], d["unsupported_fields"], d["plain_now"] = {}, set(), {}
        d["overrides"] = sync.overrides(d["id"])
    known = _known(ctx["devices"])
    for d, settings, mapping in known:
        d["read"] = profile.read(settings, mapping)
        d["unsupported_fields"] = {f.key for f in profile.FIELDS if not profile.supported(f, settings, mapping)}
        d["plain_now"] = {k: settings.get(mapping.device_key(k) or "") for k in plain}
    groups = {}
    for field in profile.FLEET_FIELDS:
        groups.setdefault(field.group, []).append(field)
    ctx |= {
        "fleet": sync.stored_fleet(),
        "effective": sync.fleet(),
        "same_passphrase": sync.same_passphrase(),
        "plain": plain,
        "groups": groups,
        "device_fields": profile.DEVICE_FIELDS,
        "display": profile.display,
        "channels": {"2.4": profile.CHANNELS_24, "5": profile.CHANNELS_5},
        "dfs": profile.DFS_CHANNELS,
        "shared": _shared(known),
        "shared_names": [(d["status"] or {}).get("name") or d["host"] for d, _, _ in known],
        "msg": msg,
        "err": err,
    }
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings")
async def save_settings(request: Request):
    form = await request.form()
    old = sync.stored_fleet()
    same = bool(form.get(sync.SAME_PASSPHRASE))
    fleet, plain, per_device = {}, {}, {}
    try:
        for field in profile.FLEET_FIELDS:
            raw = str(form.get(field.key, ""))
            if field.kind == "secret":
                if form.get(f"stop_{field.key}") or (field.key == "passphrase_24" and same):
                    continue
                if raw:
                    fleet[field.key] = profile.validate(field, raw)
                elif field.key in old:
                    fleet[field.key] = old[field.key]
            elif raw.strip():
                fleet[field.key] = profile.validate(field, raw)
        for key in sync.plain():
            if not form.get(f"stopplain__{key}"):
                plain[key] = profile.validate_plain(key, str(form.get(f"plain__{key}", "")))
        for dev in db.list_devices():
            values = {}
            for field in profile.DEVICE_FIELDS:
                raw = str(form.get(f"{field.key}__{dev['id']}", "")).strip()
                if raw:
                    values[field.key] = profile.validate(field, raw)
            per_device[dev["id"]] = values
    except ValueError as exc:
        return _back("/settings", err=f"Nothing was saved. {exc}")
    sync.save_fleet(fleet, same)
    sync.save_plain(plain)
    for device_id, values in per_device.items():
        sync.save_overrides(device_id, values)
    return _back("/settings", msg="Managed settings saved. Nothing has been changed on the routers yet.")


@app.post("/settings/manage")
async def manage_setting(key: str = Form(...), value: str = Form("")):
    """Start managing one of the settings every router shares."""
    if key not in _shared(_known(db.list_devices())):
        return _back("/settings", err=f"{key} is not a setting every router shares.")
    try:
        value = profile.validate_plain(key, value)
    except ValueError as exc:
        return _back("/settings", err=str(exc))
    sync.save_plain(sync.plain() | {key: value})
    return _back("/settings", msg=f"Now managing {key}. Nothing has been changed on the routers yet.")


@app.post("/settings/adopt")
async def adopt_settings(device_id: int = Form(...)):
    """Start managing every named setting at the value it has now, taking shared ones from one router."""
    source = _device_or_404(device_id)
    settings = sync.current(source)
    if settings is None:
        return _back("/settings", err=f"The settings of {_label(source)} are not known yet.")

    def valid(fields, values):
        kept = {}
        for field in fields:
            value = values.get(field.key)
            if value:
                try:
                    kept[field.key] = profile.validate(field, value)
                except ValueError:
                    pass
        return kept

    fleet = valid(profile.FLEET_FIELDS, profile.read(settings, sync.mapping(source, settings)))
    same = "passphrase_5" in fleet and fleet.get("passphrase_24") == fleet["passphrase_5"]
    if same:
        del fleet["passphrase_24"]
    sync.save_fleet(fleet, same)
    for dev, own, mapping in _known(db.list_devices()):
        sync.save_overrides(dev["id"], valid(profile.DEVICE_FIELDS, profile.read(own, mapping)))
    return _back(
        "/settings",
        msg=f"Now managing the current settings of {_label(source)}, and each router's current channels. "
        "Nothing has been changed on the routers.",
    )


# --- per-router settings mapping ----------------------------------------------------


def _mapping_rows(dev, settings, mapping):
    """One row per portal key the portal understands or manages, resolved for this router."""
    labels = {k: f.label for f in profile.FIELDS for k in f.keys}
    managed = set(sync.plain())
    values = sync.fleet() | sync.overrides(dev["id"])
    managed |= {k for f in profile.FIELDS if f.key in values for k in f.keys}
    rows = []
    for key in dict.fromkeys(profile.FIELD_PORTAL_KEYS + sorted(sync.plain())):
        resolved = mapping.device_key(key)
        rows.append(
            {
                "key": key,
                "label": labels.get(key, ""),
                "alias": mapping.aliases.get(key, ""),
                "resolved": resolved,
                "found": resolved is not None and resolved in settings,
                "managed": key in managed,
            }
        )
    return rows


@app.get("/devices/{device_id}/mapping")
async def mapping_page(request: Request, device_id: int, msg: str = "", err: str = ""):
    dev = _device_or_404(device_id)
    settings = sync.current(dev)
    if settings is None:
        try:
            _, settings = await backups.fetch(dev)
        except router.RouterError as exc:
            return _back(err=f"Cannot read the settings of {_label(dev)} right now. {exc}")
    mapping = sync.mapping(dev, settings)
    radios = [
        {"prefix": p, "ssid": settings.get(f"{p}_ssid", ""), "channel": settings.get(f"{p}_channel", "")}
        for p in profile.radio_prefixes(settings)
    ]
    ctx = {
        "dev": dev,
        "label": _label(dev),
        "mapping": mapping,
        "radios": radios,
        "bands": profile.BANDS,
        "rows": _mapping_rows(dev, settings, mapping),
        "device_keys": sorted(settings),
        "msg": msg,
        "err": err,
    }
    return templates.TemplateResponse(request, "mapping.html", ctx)


@app.post("/devices/{device_id}/mapping")
async def save_mapping(request: Request, device_id: int):
    dev = _device_or_404(device_id)
    here = f"/devices/{device_id}/mapping"
    settings = sync.current(dev)
    if settings is None:
        return _back(here, err="The router's settings are not known yet.")
    form = await request.form()
    prefixes = profile.radio_prefixes(settings)
    radios = {}
    for band in profile.BANDS:
        prefix = str(form.get(f"radio__{band}", ""))
        if prefix and prefix not in prefixes:
            return _back(here, err=f"{prefix} is not a radio on this router.")
        if prefix and prefix in radios.values():
            return _back(here, err="The same radio cannot serve both bands.")
        if prefix:
            radios[band] = prefix
    aliases = {}
    for key in dict.fromkeys(profile.FIELD_PORTAL_KEYS + sorted(sync.plain())):
        alias = str(form.get(f"alias__{key}", "")).strip()
        if alias and alias not in settings:
            return _back(here, err=f"This router has no setting named {alias}.")
        if alias:
            aliases[key] = alias
    sync.save_mapping(device_id, radios, aliases)
    plan = sync.plan(db.get_device(device_id), settings)
    note = ""
    if plan.unsupported:
        note = f" {len(plan.unsupported)} managed setting(s) are not supported on it: {', '.join(plan.unsupported)}."
    return _back(msg=f"Settings mapping saved for {_label(dev)}.{note}")


@app.get("/devices/{device_id}/sync")
async def sync_preview(request: Request, device_id: int):
    """Show what applying the managed settings would change, from the router's live config."""
    dev = _device_or_404(device_id)
    if not dev["mapped"]:
        return _back(f"/devices/{device_id}/mapping", err="Confirm this router's settings mapping first.")
    if device_id in backups.busy:
        return _back(err=f"{_label(dev)} is busy: {backups.busy[device_id]}")
    try:
        status = await router.collect(dev)
        _, settings = await backups.fetch(dev)
    except router.RouterError as exc:
        return _back(err=f"Cannot check {_label(dev)} right now. {exc}")
    dev["status"] = status
    ctx = {"dev": dev, "label": status["name"] or dev["host"], "status": status, "plan": sync.plan(dev, settings)}
    return templates.TemplateResponse(request, "sync.html", ctx)


async def _apply_one(dev):
    """Apply managed settings to one router and see it through its reboot. Returns success."""
    device_id = dev["id"]
    started = time.time()
    try:
        backups.busy[device_id] = "Applying settings"
        status = await router.collect(dev)
        dev = dev | {"status": status}
        count = await sync.apply(dev)
        if not count:
            db.add_event(device_id, "Managed settings checked: already in sync, nothing was changed.")
            return True
        backups.busy[device_id] = "Rebooting"
        status = await backups.wait_for_reboot(dev, status["uptime_s"])
        db.record_poll(device_id, status=status)
        dev = db.get_device(device_id)
        fetched = await backups.fetch(dev)
        left = sync.plan(dev, fetched[1]).changes
        await backups.take(dev, "post-change", fetched=fetched)
        took = int(time.time() - started)
        plural = "s" if count != 1 else ""
        if left:
            names = ", ".join(c["name"] for c in left[:5])
            db.add_event(
                device_id,
                f"Applied {count} managed setting{plural}; back online after {took}s, "
                f"but {len(left)} still differ after the reboot: {names}.",
                ok=False,
            )
            return False
        db.add_event(device_id, f"Applied {count} managed setting{plural}; back online after {took}s and in sync.")
        return True
    except (router.RouterError, ValueError) as exc:
        db.add_event(device_id, f"Applying managed settings failed: {exc}", ok=False)
        return False
    except Exception:
        log.exception("apply failed")
        db.add_event(device_id, "Applying managed settings failed unexpectedly; see the portal log.", ok=False)
        return False
    finally:
        backups.busy.pop(device_id, None)


async def _apply_many(devices):
    """Apply to routers one at a time, stopping at the first that does not finish cleanly."""
    for dev in devices:
        backups.busy[dev["id"]] = "Queued"
    for i, dev in enumerate(devices):
        if await _apply_one(dev):
            continue
        for rest in devices[i + 1 :]:
            backups.busy.pop(rest["id"], None)
            db.add_event(
                rest["id"],
                f"Skipped applying managed settings because {_label(dev)} did not finish cleanly.",
                ok=False,
            )
        break


@app.post("/devices/{device_id}/sync")
async def sync_apply(device_id: int):
    dev = _device_or_404(device_id)
    if not dev["mapped"]:
        return _back(f"/devices/{device_id}/mapping", err="Confirm this router's settings mapping first.")
    if device_id in backups.busy:
        return _back(err=f"{_label(dev)} is busy: {backups.busy[device_id]}")
    backups.busy[device_id] = "Queued"
    _spawn(_apply_many([dev]))
    return _back(f"/devices/{device_id}/backups", msg="Applying managed settings. This page updates as it goes.")


@app.post("/sync/all")
async def sync_apply_all():
    ctx = _context()
    todo = [d for d in ctx["devices"] if d["online"] and d["mapped"] and not d["busy"] and d["drift"]]
    if not todo:
        return _back("/settings", msg="No online router needs changes.")
    for dev in todo:
        backups.busy[dev["id"]] = "Queued"
    _spawn(_apply_many(todo))
    names = ", ".join(_label(d) for d in todo)
    return _back(msg=f"Applying managed settings to {names}, one router at a time.")


@app.get("/api/devices")
async def api_devices():
    return JSONResponse(db.list_devices())


@app.get("/healthz")
async def healthz():
    return {"ok": True}
