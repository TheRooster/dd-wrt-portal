"""Watching the DD-WRT download site and matching routers to firmware images.

Layout of the site: <base>/<year>/<MM-DD-YYYY-rNNNNN>/<model folder>/<image files>.
Folder names are not derived from the model name in any fixed way, and the
site publishes no checksums, so each router's folder and file are suggested
here and confirmed by a person.
"""

import asyncio
import difflib
import json
import logging
import re
import time
from datetime import date, datetime, timezone

import httpx

from . import backups, config, db, router

log = logging.getLogger("portal")

_ROW = re.compile(r'<a href="([^"?#]+)">[^<]*</a>\s+(\d{2}-\w{3}-\d{4} \d{2}:\d{2})\s+(\d+|-)')
_BUILD_DIR = re.compile(r"^(\d{2})-(\d{2})-(\d{4})-r(\d+)/$")
_IMAGE = re.compile(r"\.(bin|trx|chk|img)$", re.IGNORECASE)
LATEST = "firmware_latest"
BUILDS = "firmware_builds"
FAILED = "firmware_failed"


class FirmwareError(Exception):
    """The download site could not be read; the message is safe to show."""


def parse_listing(html):
    """Entries of a directory listing as [(name, size or None for a folder)]."""
    return [(name, None if size == "-" else int(size)) for name, _, size in _ROW.findall(html)]


def parse_builds(html, year):
    """Builds in a year's listing as [{build, date, path}], oldest first."""
    builds = []
    for name, _ in parse_listing(html):
        m = _BUILD_DIR.match(name)
        if m:
            month, day, yyyy, build = m.groups()
            builds.append({"build": int(build), "date": f"{yyyy}-{month}-{day}", "path": f"{year}/{name.rstrip('/')}"})
    return sorted(builds, key=lambda b: b["build"])


async def _get(path):
    url = f"{config.FIRMWARE_URL}/{path}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "ddwrt-portal"}) as c:
            response = await c.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise FirmwareError(f"Could not read {url}: {exc}") from None


async def fetch_latest():
    """Find the newest published build and the model folders it contains."""
    year = datetime.now(timezone.utc).year
    for y in (year, year - 1):
        builds = parse_builds(await _get(f"{y}/"), y)
        if builds:
            latest = builds[-1]
            listing = parse_listing(await _get(latest["path"] + "/"))
            latest["folders"] = sorted(name.rstrip("/") for name, size in listing if size is None)
            latest["checked_at"] = time.time()
            return latest
    raise FirmwareError("The download site lists no builds for this year or last.")


async def folder_images(path, folder):
    """Image files in one model folder of a build: [(name, size)]."""
    listing = parse_listing(await _get(f"{path}/{folder}/"))
    return [(name, size) for name, size in listing if size is not None and _IMAGE.search(name)]


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def suggest_folder(model, folders):
    """The folder whose name is closest to the router's model name, or None."""
    want = _squash(model or "")
    if not want:
        return None
    scored = [(difflib.SequenceMatcher(None, want, _squash(f)).ratio(), f) for f in folders]
    score, folder = max(scored, default=(0, None))
    return folder if score >= 0.7 else None


def suggest_file(images):
    """The image for upgrading a router already running DD-WRT, if that is unambiguous.

    "webflash" images are for upgrading from DD-WRT; "factory" images are for
    converting from the vendor's firmware and are never suggested.
    """
    names = [name for name, _ in images if "factory" not in name.lower()]
    webflash = [n for n in names if "webflash" in n.lower()]
    for candidates in (webflash, names):
        if len(candidates) == 1:
            return candidates[0]
    return None


def to_pattern(filename, build):
    """Replace the build number in a file name so it matches future builds."""
    return filename.replace(str(build), "{build}")


def latest():
    """The cached result of the last check, or None."""
    raw = db.get_kv(LATEST)
    return json.loads(raw) if raw else None


async def resolve(device_id, info):
    """Look up the router's matched image in the latest build and store what was found."""
    row = db.get_firmware_map(device_id)
    if not row or not row["confirmed"]:
        return
    file, size = None, None
    if row["folder"] in info["folders"]:
        wanted = row["pattern"].replace("{build}", str(info["build"]))
        for name, bytes_ in await folder_images(info["path"], row["folder"]):
            if name == wanted:
                file, size = name, bytes_
    db.set_firmware_found(device_id, info["build"], file, size)


async def check():
    """Refresh the latest build and every confirmed router's image in it."""
    info = await fetch_latest()
    db.set_kv(LATEST, json.dumps(info))
    devices = db.list_devices()
    for dev in devices:
        await resolve(dev["id"], info)
    try:
        db.set_kv(BUILDS, json.dumps(await fetch_builds(first_year(devices))))
    except FirmwareError as exc:
        log.warning("build list not refreshed: %s", exc)
    return info


def image_url(info, row):
    return f"{config.FIRMWARE_URL}/{info['path']}/{row['folder']}/{row['file']}"


def state(dev, info=None):
    """Where one router stands: {status, ...} for the UI.

    status is one of: unknown (no check yet), unmatched (no confirmed image),
    missing (latest build has no image for it), current, or available.
    """
    info = info or latest()
    row = db.get_firmware_map(dev["id"])
    running = (dev["status"] or {}).get("build")
    result = {"running": running, "row": row, "latest": info}
    if not info:
        return result | {"status": "unknown"}
    if not row or not row["confirmed"]:
        suggestion = suggest_folder((dev["status"] or {}).get("model"), info["folders"])
        return result | {"status": "unmatched", "suggestion": (row or {}).get("folder") or suggestion}
    if row["found_build"] != info["build"] or not row["file"]:
        return result | {"status": "missing"}
    result |= {"url": image_url(info, row), "size": row["size"]}
    return result | {"status": "current" if running and running >= info["build"] else "available"}


# --- choosing a build to install ------------------------------------------------


def release_date(status):
    """The date in a router's login banner ("07/15/23") as YYYY-MM-DD, or None."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", (status or {}).get("release") or "")
    return f"20{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None


def first_year(devices):
    """The earliest year any router's firmware dates from; last year if unknown."""
    this_year = datetime.now(timezone.utc).year
    years = [int(d[:4]) for d in (release_date(dev["status"]) for dev in devices) if d]
    return min(years + [this_year - 1])


async def fetch_builds(from_year):
    """Every published build from `from_year` to now, oldest first."""
    builds = []
    for year in range(from_year, datetime.now(timezone.utc).year + 1):
        builds += parse_builds(await _get(f"{year}/"), year)
    return sorted(builds, key=lambda b: b["build"])


def builds():
    """The cached build list, oldest first; empty before the first check."""
    raw = db.get_kv(BUILDS)
    return json.loads(raw) if raw else []


def find_build(number, index=None):
    return next((b for b in (builds() if index is None else index) if b["build"] == number), None)


def running_date(status, index):
    """When the router's running build was published: from the site, else its banner."""
    found = find_build((status or {}).get("build"), index)
    return found["date"] if found else release_date(status)


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def assess(running, running_on, target, limit=None):
    """Judge a jump from the running build to `target` (a build-list entry).

    Returns {gap_days, blocked, warnings}. A warning must be acknowledged
    before the install starts; `blocked` is a reason it cannot start at all.
    """
    limit = config.FIRMWARE_MAX_JUMP_DAYS if limit is None else limit
    gap = _days(running_on, target["date"]) if running_on else None
    result = {"gap_days": gap, "blocked": None, "warnings": []}
    if running == target["build"]:
        result["blocked"] = f"The router already runs r{running}."
    elif running and target["build"] < running:
        result["warnings"].append(
            f"r{target['build']} is older than the running r{running}. Downgrading can leave settings "
            "the older build does not understand."
        )
    elif gap is None:
        result["warnings"].append(
            "The age of the running build is unknown, so the size of this jump cannot be judged."
        )
    elif gap > limit:
        result["warnings"].append(
            f"r{target['build']} was published {gap} days after the running build. Jumps of more than "
            f"{limit} days are not recommended: a jump of three years left one router unbootable. "
            "Install an intermediate build first."
        )
    return result


def failures(model=None):
    """Installs after which a router did not boot: [{model, build, from, at}]."""
    rows = json.loads(db.get_kv(FAILED) or "[]")
    return [r for r in rows if model is None or r["model"] == model]


def record_failure(model, build, came_from):
    rows = [r for r in failures() if (r["model"], r["build"]) != (model, build)]
    rows.append({"model": model, "build": build, "from": came_from, "at": time.time()})
    db.set_kv(FAILED, json.dumps(rows))


def failure_warning(model, target, failed):
    """A warning if `target` is at or past the oldest build that failed on this model."""
    builds_ = sorted(r["build"] for r in failed)
    if not builds_ or target < builds_[0]:
        return None
    if target in builds_:
        return f"r{target} did not boot on a {model} when it was installed from this portal."
    return (
        f"r{builds_[0]} did not boot on a {model}, and r{target} is newer. The fault may still be present. "
        "Have the bootloader recovery ready."
    )


def suggest_next(running, running_on, index, limit=None, below=None):
    """The newest build within the jump limit, else the next newer one, else None.

    Builds numbered `below` or higher are never suggested.
    """
    limit = config.FIRMWARE_MAX_JUMP_DAYS if limit is None else limit
    newer = [b for b in index if (running is None or b["build"] > running) and (below is None or b["build"] < below)]
    if not newer:
        return None
    if running_on:
        near = [b for b in newer if _days(running_on, b["date"]) <= limit]
        if near:
            return near[-1]
    return newer[0]


async def find_image(build, row):
    """The router's image in `build`: {file, size, url}. Raises if the build has none."""
    wanted = row["pattern"].replace("{build}", str(build["build"]))
    for name, size in await folder_images(build["path"], row["folder"]):
        if name == wanted:
            return {"file": name, "size": size, "url": f"{config.FIRMWARE_URL}/{build['path']}/{row['folder']}/{name}"}
    raise FirmwareError(f"r{build['build']} has no {row['folder']}/{wanted} on the download site.")


async def download(image):
    """Fetch an image and check its length against the site's listing."""
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers={"User-Agent": "ddwrt-portal"}) as c:
            response = await c.get(image["url"])
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FirmwareError(f"Could not download {image['url']}: {exc}") from None
    if len(response.content) != image["size"]:
        raise FirmwareError(
            f"The download of {image['file']} is {len(response.content)} bytes; the site lists {image['size']}."
        )
    return response.content


# --- installing -----------------------------------------------------------------

# Memory that must stay free on the router beyond the image held in /tmp.
HEADROOM = 24 * 1024 * 1024


def parse_flash_facts(text):
    """Read the preflight script's output: {partition, mem_avail, has_write}."""
    facts = {"partition": None, "mem_avail": None, "has_write": "write=yes" in text}
    m = re.search(r'^mtd\d+:\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"linux"', text, re.MULTILINE)
    if m:
        facts["partition"] = int(m.group(1), 16)
    avail = re.search(r"^MemAvailable:\s+(\d+)", text, re.MULTILINE)
    free = re.search(r"^MemFree:\s+(\d+)", text, re.MULTILINE)
    if avail or free:
        facts["mem_avail"] = int((avail or free).group(1)) * 1024
    return facts


def flash_problems(facts, size):
    """Reasons this image must not be written to this router; empty if none."""
    problems = []
    if not facts["has_write"]:
        problems.append("The router has no `write` command, so the portal cannot flash it.")
    if facts["partition"] is None:
        problems.append("The router has no flash partition named \"linux\".")
    elif size > facts["partition"]:
        problems.append(
            f"The image is {size:,} bytes; the router's firmware partition holds {facts['partition']:,}."
        )
    if facts["mem_avail"] is None:
        problems.append("The router's free memory could not be read.")
    elif facts["mem_avail"] < size + HEADROOM:
        problems.append(
            f"The router has {facts['mem_avail'] // 1048576} MB of memory free; holding the image needs "
            f"{(size + HEADROOM) // 1048576} MB."
        )
    return problems


async def install(dev, target, image, started):
    """Flash `image` onto one router and report the outcome as an event.

    Order: back up, download, copy to the router's RAM disk, checksum there,
    write to flash, reboot only if the write succeeded, then confirm the build.
    """
    device_id, host = dev["id"], dev["host"]
    before = (dev["status"] or {}).get("build")
    what = f"Firmware r{before} to r{target['build']}"
    busy = backups.busy
    flashing = False
    try:
        busy[device_id] = "Backing up before the update"
        await backups.take(dev, "pre-change")

        busy[device_id] = f"Downloading r{target['build']}"
        data = await download(image)

        problems = flash_problems(parse_flash_facts(await router.flash_facts(dev)), len(data))
        if problems:
            raise FirmwareError(" ".join(problems))

        busy[device_id] = "Copying the image to the router"
        await router.upload_firmware(dev, data)

        busy[device_id] = "Writing firmware. Do not power off the router"
        status = await router.collect(dev)
        flashing = True
        await router.start_flash(dev)
        deadline = time.time() + config.FIRMWARE_WRITE_TIMEOUT
        while True:
            await asyncio.sleep(5)
            try:
                code, tail = await router.flash_result(dev)
            except router.RouterError:
                code, tail = None, ""
            if code is not None:
                break
            if time.time() > deadline:
                raise FirmwareError(
                    f"{host}: the write has not finished after {config.FIRMWARE_WRITE_TIMEOUT // 60} minutes. "
                    "Do not power off the router."
                )
        if code != 0:
            raise FirmwareError(
                f"{host}: the write failed (exit {code}): {tail or 'no output'}. The router was not rebooted. "
                "Do not power it off; run the install again."
            )

        busy[device_id] = "Rebooting into the new firmware"
        await router.reboot(dev)
        try:
            status = await backups.wait_for_reboot(dev, status["uptime_s"], timeout=config.FIRMWARE_BOOT_TIMEOUT)
        except router.RouterError:
            record_failure(status["model"], target["build"], before)
            raise
        db.record_poll(device_id, status=status)
        took = int(time.time() - started)
        if status["build"] != target["build"]:
            db.add_event(
                device_id,
                f"{what}: the router is back after {took}s but reports r{status['build']}. The update did not take.",
                ok=False,
            )
            return
        fresh = db.get_device(device_id)
        _, created = await backups.take(fresh, "post-change")
        db.add_event(
            device_id,
            f"{what}: installed {image['file']} and back online after {took}s."
            + (" The new build changed some settings; see the latest backup." if created else ""),
        )
        await resolve(device_id, latest())
    except (router.RouterError, FirmwareError, ValueError) as exc:
        hint = (
            " If the router stays offline, recover it through its bootloader with the previous build."
            if flashing else " Nothing was written to the router's flash."
        )
        db.add_event(device_id, f"{what} failed: {exc}{hint}", ok=False)
    except Exception:
        log.exception("firmware install failed")
        db.add_event(device_id, f"{what} failed unexpectedly; see the portal log.", ok=False)
    finally:
        busy.pop(device_id, None)
