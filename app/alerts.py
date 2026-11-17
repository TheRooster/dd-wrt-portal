"""Alerts: conditions that stay open until they clear.

Each poll cycle recomputes which conditions hold for every router. A condition
that newly holds raises an alert; one that no longer holds clears it. Raising
and clearing are sent to ALERT_WEBHOOK when it is set.
"""

import logging
import time

import httpx

from . import config, db

log = logging.getLogger("portal")


def conditions(d, now=None):
    """{kind: message} for one router, as prepared by the dashboard context."""
    now = now or time.time()
    name = (d["status"] or {}).get("name") or d["host"]
    found = {}
    silent = now - (d["last_ok"] or d["enrolled_at"])
    if not d["online"] and silent > config.ALERT_OFFLINE_AFTER:
        found["offline"] = f"{name} has not answered for {int(silent // 60)} minutes."
    if d["online"]:
        if d.get("drift"):
            n = d["drift"]
            found["drift"] = f"{name}: {n} managed setting{'s' if n != 1 else ''} differ{'s' if n == 1 else ''} from the portal."
        if d["status"].get("telnet_enabled"):
            found["telnet"] = f"{name} has Telnet switched on."
        if d["status"].get("ssh_password_auth"):
            found["ssh-password"] = f"{name} accepts SSH password logins."
    return found


def evaluate(devices, now=None):
    """Bring stored alerts in line with the current conditions. Returns (raised, cleared) messages."""
    now = now or time.time()
    raised, cleared = [], []
    open_now = {(a["device_id"], a["kind"]): a for a in db.open_alerts()}
    for d in devices:
        if d.get("busy"):
            continue  # a restore, sync or firmware install explains itself
        found = conditions(d, now)
        if not d["online"]:
            # What an unreachable router's settings are is unknown: leave those alerts as they were.
            found |= {k: a["message"] for (dev, k), a in open_now.items() if dev == d["id"] and k != "offline"}
        for kind, message in found.items():
            if (d["id"], kind) not in open_now:
                db.raise_alert(d["id"], kind, message, now)
                raised.append(message)
        for (dev, kind), alert in open_now.items():
            if dev == d["id"] and kind not in found:
                db.clear_alert(alert["id"], now)
                cleared.append(alert["message"])
    return raised, cleared


async def notify(raised, cleared):
    """Post each change to the webhook as {"text": ...}; failures are logged, never raised."""
    if not config.ALERT_WEBHOOK or not (raised or cleared):
        return
    lines = [f"Alert: {m}" for m in raised] + [f"Cleared: {m}" for m in cleared]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for line in lines:
                (await client.post(config.ALERT_WEBHOOK, json={"text": line})).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("alert webhook failed: %s", exc)
