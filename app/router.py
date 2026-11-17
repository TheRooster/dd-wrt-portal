"""SSH access to routers: one-time enrolment by password, then key-only."""

import hashlib
import shlex

import asyncssh

from . import config
from .collect import COLLECT_SCRIPT, parse

_portal_key = None


class RouterError(Exception):
    """A router operation failed; the message is safe to show in the UI."""


def portal_key():
    """Load the portal's SSH identity, generating it on first start."""
    global _portal_key
    if _portal_key is None:
        if not config.KEY_PATH.exists():
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            key = asyncssh.generate_private_key("ssh-ed25519", comment="ddwrt-portal")
            config.KEY_PATH.touch(mode=0o600)
            key.write_private_key(str(config.KEY_PATH))
        _portal_key = asyncssh.read_private_key(str(config.KEY_PATH))
    return _portal_key


def public_key():
    return portal_key().export_public_key("openssh").decode().strip()


def _known_hosts(host, port, host_key):
    name = host if port == 22 else f"[{host}]:{port}"
    return asyncssh.import_known_hosts(f"{name} {host_key}\n")


def _explain(exc, host):
    if isinstance(exc, asyncssh.PermissionDenied):
        return f"{host}: login rejected"
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return f"{host}: SSH host key has changed since enrolment; remove and re-add the router"
    if isinstance(exc, (TimeoutError, asyncssh.TimeoutError)):
        return f"{host}: timed out"
    if isinstance(exc, OSError):
        return f"{host}: unreachable ({exc.strerror or exc})"
    return f"{host}: {exc}"


async def _run(conn, script):
    result = await conn.run(script, check=False, timeout=config.SSH_COMMAND_TIMEOUT)
    return result.stdout or ""


# Adds the key to nvram (so it survives reboots) and to the live
# authorized_keys file (so it works without restarting sshd). Idempotent.
ENROLL_SCRIPT = r"""
KEY='%(key)s'
BODY='%(body)s'
cur="$(nvram get sshd_authorized_keys)"
case "$cur" in
  *"$BODY"*) ;;
  *)
    if [ -n "$cur" ]; then
      nvram set sshd_authorized_keys="$cur
$KEY"
    else
      nvram set sshd_authorized_keys="$KEY"
    fi
    nvram commit >/dev/null 2>&1
    ;;
esac
mkdir -p /tmp/root/.ssh
grep -qF "$BODY" /tmp/root/.ssh/authorized_keys 2>/dev/null || echo "$KEY" >> /tmp/root/.ssh/authorized_keys
chmod 700 /tmp/root/.ssh
chmod 600 /tmp/root/.ssh/authorized_keys
echo ENROLL_OK
"""


async def enroll(host, password, port=22):
    """Install the portal's public key using a one-time password login.

    The password is used for this single connection and is not stored or
    logged. The router's host key is trusted on this first use and pinned.
    Returns (host_key, fingerprint).
    """
    pub = public_key()
    script = ENROLL_SCRIPT % {"key": pub, "body": pub.split()[1]}
    try:
        async with asyncssh.connect(
            host,
            port=port,
            username=config.SSH_USER,
            password=password,
            client_keys=None,
            agent_path=None,
            known_hosts=None,
            preferred_auth="password,keyboard-interactive",
            connect_timeout=config.SSH_CONNECT_TIMEOUT,
        ) as conn:
            server_key = conn.get_server_host_key()
            out = await _run(conn, script)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, host)) from None
    if "ENROLL_OK" not in out:
        raise RouterError(f"{host}: could not install the portal's SSH key")
    host_key = server_key.export_public_key("openssh").decode().strip()
    host_key = " ".join(host_key.split()[:2])
    return host_key, server_key.get_fingerprint()


# Removes the portal's key from nvram and from the live authorized_keys file.
# Other keys are left alone. Password login is untouched, so access remains.
UNENROLL_SCRIPT = r"""
BODY='%s'
cur="$(nvram get sshd_authorized_keys)"
new="$(printf '%%s\n' "$cur" | grep -vF "$BODY")"
if [ "$new" != "$cur" ]; then
  nvram set sshd_authorized_keys="$new"
  nvram commit >/dev/null 2>&1
fi
f=/tmp/root/.ssh/authorized_keys
if [ -f $f ]; then
  grep -vF "$BODY" $f > $f.new
  cat $f.new > $f
  rm -f $f.new
fi
echo UNENROLL_OK
"""


async def unenroll(dev):
    """Log in with the portal's key and remove that key from the router."""
    script = UNENROLL_SCRIPT % public_key().split()[1]
    try:
        async with _connect(dev) as conn:
            out = await _run(conn, script)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None
    if "UNENROLL_OK" not in out:
        raise RouterError(f"{dev['host']}: could not remove the portal's SSH key")


def _connect(dev):
    return asyncssh.connect(
        dev["host"],
        port=dev["port"],
        username=config.SSH_USER,
        client_keys=[portal_key()],
        agent_path=None,
        known_hosts=_known_hosts(dev["host"], dev["port"], dev["host_key"]),
        preferred_auth="publickey",
        connect_timeout=config.SSH_CONNECT_TIMEOUT,
    )


async def collect(dev):
    """Log in with the portal's key and return the parsed status."""
    try:
        async with _connect(dev) as conn:
            out = await _run(conn, COLLECT_SCRIPT)
        return parse(out)
    except (OSError, asyncssh.Error, TimeoutError, ValueError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None


# The temporary file sits in the router's RAM disk and is removed either way.
BACKUP_SCRIPT = (
    "f=/tmp/.portal-backup.$$; umask 077; "
    "nvram backup $f >/dev/null 2>&1 && cat $f; rc=$?; rm -f $f; exit $rc"
)


async def backup(dev):
    """Return the router's `nvram backup` file as bytes."""
    try:
        async with _connect(dev) as conn:
            result = await conn.run(
                BACKUP_SCRIPT, check=False, encoding=None, timeout=config.SSH_COMMAND_TIMEOUT
            )
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None
    if result.exit_status != 0 or not result.stdout:
        raise RouterError(f"{dev['host']}: the router could not produce a backup")
    return bytes(result.stdout)


# `nvram restore` erases every setting before loading the file, and it accepts
# a truncated file without complaint. So the file must be fully parsed before
# it gets here, and the copy on the router is checksummed before it is used.
RESTORE_SCRIPT = r"""
f=/tmp/.portal-restore.$$; umask 077
cat > $f
if [ "$(md5sum < $f | cut -d' ' -f1)" != "%s" ]; then rm -f $f; echo BAD_TRANSFER; exit 1; fi
nvram restore $f >/dev/null 2>&1; rc=$?
rm -f $f
[ $rc -eq 0 ] || { echo RESTORE_FAILED; exit 1; }
echo RESTORE_OK
"""


async def restore(dev, raw):
    """Load a validated backup file into the router's nvram (not yet saved to flash)."""
    script = RESTORE_SCRIPT % hashlib.md5(raw).hexdigest()
    try:
        async with _connect(dev) as conn:
            result = await conn.run(
                script, input=raw, encoding=None, check=False, timeout=config.SSH_COMMAND_TIMEOUT
            )
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None
    out = (result.stdout or b"").decode("utf-8", "replace")
    if "BAD_TRANSFER" in out:
        raise RouterError(f"{dev['host']}: the backup was damaged in transfer; nothing was changed")
    if "RESTORE_OK" not in out:
        raise RouterError(f"{dev['host']}: the router rejected the backup file")


async def run(dev, command, timeout=None):
    """Run a shell command on the router; raise RouterError on a non-zero exit."""
    try:
        async with _connect(dev) as conn:
            result = await conn.run(command, check=False, timeout=timeout or config.SSH_COMMAND_TIMEOUT)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None
    if result.exit_status != 0:
        raise RouterError(f"{dev['host']}: command failed on the router")
    return result.stdout or ""


async def set_values(dev, values):
    """Set nvram values in memory (no commit)."""
    if values:
        await run(dev, "; ".join(f"nvram set {shlex.quote(f'{k}={v}')}" for k, v in values.items()))


async def unset_values(dev, names):
    """Remove nvram settings in memory (no commit)."""
    if names:
        await run(dev, "; ".join(f"nvram unset {shlex.quote(n)}" for n in names))


async def commit(dev):
    await run(dev, "nvram commit >/dev/null 2>&1", timeout=90)


async def reboot(dev):
    """Start a reboot two seconds from now, so this SSH session can close cleanly."""
    await run(dev, "(sleep 2; reboot) </dev/null >/dev/null 2>&1 &")


# --- firmware ---------------------------------------------------------------

FW = "/tmp/.portal-firmware"

FLASH_FACTS_SCRIPT = (
    'command -v write >/dev/null 2>&1 && echo write=yes; cat /proc/mtd; '
    "grep -E '^(MemFree|MemAvailable):' /proc/meminfo; true"
)

# The image is checksummed on the router before anything can write it.
UPLOAD_SCRIPT = f"""
umask 077; rm -f {FW}.rc {FW}.log {FW}.bin
cat > {FW}.bin
if [ "$(md5sum < {FW}.bin | cut -d' ' -f1)" = "%s" ]; then echo UPLOAD_OK; else rm -f {FW}.bin; echo BAD_TRANSFER; fi
"""

# `write <file> linux` is what DD-WRT's own web upgrade runs. It is detached so
# that a dropped SSH session or a portal restart cannot interrupt it.
START_FLASH_SCRIPT = f"""
[ -s {FW}.bin ] || {{ echo NO_IMAGE; exit 0; }}
(write {FW}.bin linux > {FW}.log 2>&1; echo $? > {FW}.rc; rm -f {FW}.bin) </dev/null >/dev/null 2>&1 &
echo STARTED
"""

FLASH_RESULT_SCRIPT = f"cat {FW}.rc 2>/dev/null; echo @@; tail -n 3 {FW}.log 2>/dev/null; true"


async def flash_facts(dev):
    return await run(dev, FLASH_FACTS_SCRIPT)


async def upload_firmware(dev, data):
    """Copy a firmware image to the router's RAM disk and verify it arrived intact."""
    script = UPLOAD_SCRIPT % hashlib.md5(data).hexdigest()
    try:
        async with _connect(dev) as conn:
            result = await conn.run(script, input=data, encoding=None, check=False, timeout=600)
    except (OSError, asyncssh.Error, TimeoutError) as exc:
        raise RouterError(_explain(exc, dev["host"])) from None
    if b"UPLOAD_OK" not in (result.stdout or b""):
        raise RouterError(f"{dev['host']}: the image was damaged in transfer and was removed from the router")


async def start_flash(dev):
    if "STARTED" not in await run(dev, START_FLASH_SCRIPT):
        raise RouterError(f"{dev['host']}: the image is no longer on the router; nothing was written")


async def flash_result(dev):
    """(exit code or None while still writing, last lines of the writer's output)."""
    code, _, tail = (await run(dev, FLASH_RESULT_SCRIPT)).partition("@@")
    tail = " ".join(tail.split())[-300:]
    return (int(code) if code.strip().lstrip("-").isdigit() else None), tail


async def survey(dev, script):
    """Run a site survey on every radio. Clients may pause for a second or two."""
    return await run(dev, script, timeout=120)


LOG_LINES = 200
LOGS_SCRIPT = (
    f'echo "syslog=$(nvram get syslogd_enable)"; echo "@@system"; tail -n {LOG_LINES} /var/log/messages 2>/dev/null; '
    f'echo "@@kernel"; dmesg | tail -n {LOG_LINES}; true'
)


async def logs(dev):
    """Recent system and kernel log lines: {syslog_on, system, kernel}."""
    out = await run(dev, LOGS_SCRIPT)
    head, _, rest = out.partition("@@system\n")
    system, _, kernel = rest.partition("@@kernel\n")
    return {"syslog_on": "syslog=1" in head, "system": system.strip(), "kernel": kernel.strip()}
