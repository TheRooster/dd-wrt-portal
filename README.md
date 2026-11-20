# DD-WRT Portal

Central management for DD-WRT access points. The portal talks to each router
over SSH; nothing is installed on the routers.

## Run

    docker compose up -d --build

Open http://127.0.0.1:8080 and add each router by IP and root password. The
password is used once to install the portal's SSH public key
(`sshd_authorized_keys` in nvram) and is never stored. After that the portal
logs in with its own key and pins the router's SSH host key.

## Signing in

The dashboard can be viewed without signing in. Every other page, and every
request that changes anything, needs a signed-in user. Routes are refused by
default: only the paths listed in `app/auth.py` are open.

A new database gets one user, `admin` with password `admin` (`DEFAULT_USER`,
`DEFAULT_PASSWORD`). That password must be changed at first sign-in; until
then the account can reach nothing but the change-password page. Sign in and
change it straight after the first start. More users are added on the Account
page, each with the same forced change. All users have full access.

Passwords are stored as salted scrypt hashes. Five wrong passwords lock that
user name for five minutes. Sessions last `SESSION_HOURS`; changing a
password or removing a user ends that user's sessions.

The portal serves plain HTTP, so it is still published on loopback only. Put
it behind a TLS-terminating proxy before exposing it to a network.

## Managed settings

The portal keeps its own names for settings ("portal keys"). Radio settings
are named by band (`wifi24.ssid`, `wifi5.wpa_psk`); everything else uses its
DD-WRT nvram name. Each router has a mapping, confirmed when it is enrolled,
that says which of its radios serves each band and gives the router's own
name for any setting it calls something else. `app/profile.py` holds this.

The Managed settings page has:

- named settings the portal understands (network names, a passphrase per
  band with "Same as 5 GHz", time, access, logging, reboot schedule,
  channels);
- every other setting that all routers share, any of which can be managed as
  a plain value. Secrets, hardware details, per-router identity, and radio
  settings known to be chipset-specific are not offered.

Every row has a "Unique per router" box. Ticking it moves the row to
Router unique settings, where each router gets its own value; channels start
there. A setting made unique starts with its shared value on every router.
One made shared again keeps the routers' value if they all agree and is left
unmanaged if they do not.

A setting left blank is not managed and is never touched. A managed setting a
router has no place for is flagged as not supported on that router and is
skipped there, never written.

Each router shows how many managed settings differ. Applying shows the exact
changes and the connected clients first, then backs up, writes, verifies,
saves, and reboots the router. "Apply to all" does one router at a time and
stops if one does not come back in sync.

## Firmware

The portal checks the DD-WRT download site every `FIRMWARE_CHECK_HOURS` for
the newest build. Each router is matched to a model folder and image file on
the Firmware page; the portal suggests both from the model name and a person
confirms. Routers with a newer image available are flagged. DD-WRT publishes
no checksums, so images are trusted only as far as the HTTPS download.

Installing is done one build at a time. Enter the build to install; the page
suggests the newest build published within `FIRMWARE_MAX_JUMP_DAYS` of the
running one. A larger jump, or a downgrade, needs an extra confirmation.
Any published build can be entered, older ones included, so an unstable
build can be rolled back even on a router that is up to date. A rollback
keeps the settings (no NVRAM wipe), which the confirmation warns is
dangerous: the older build can misread settings the newer one wrote. The
install backs up the settings, downloads the image, copies it to the router's
RAM disk, checksums it there, and runs DD-WRT's `write <image> linux`. The
router reboots only if the write succeeds, and the portal then confirms the
new build number. An image larger than the router's firmware partition or its
free memory is refused. A build that fails to boot needs the model's
bootloader recovery (for Netgear, `nmrpflash`); the portal cannot undo it. A
build that fails to boot is recorded for that model: it and newer builds are
no longer suggested and carry a warning.

## Wi-Fi clients

The Clients page lists every connected Wi-Fi client with its router, band and
signal, taken from the regular status poll. Clients are identified by hardware
address and can be given a name, which the portal stores. Link rate and
connection time are shown on routers whose driver reports them.

## Channels

The Channels page surveys the neighbourhood on request (DD-WRT's
`site_survey` on each radio) and scores each candidate channel by the
networks on or overlapping it, weighted by signal. Candidates are 1, 6 and 11
on 2.4 GHz and the non-radar channels on 5 GHz. The plan tries every
combination and picks the one with the least interference while keeping the
fleet's own routers on separate channels. It only recommends; channels are
set on the Managed settings page.

## Alerts and logs

An alert opens when a router is silent for `ALERT_OFFLINE_AFTER` seconds,
differs from the managed settings, or has Telnet or SSH password login on. It
clears by itself. Open alerts show on the dashboard; the Alerts page keeps the
history. If `ALERT_WEBHOOK` is set, each change is posted to it as
`{"text": "..."}`, the format Slack-compatible webhooks accept.

Each router card links to its logs, read over SSH on request and not stored.
The system log needs "System log" switched on in Managed settings; the kernel
log is always there.

## Kubernetes

`k8s/` holds a kustomize base: a namespace, a 1 Gi volume claim for `/data`,
a Deployment and a Service. An Ingress is included but not enabled.

    docker buildx build --platform linux/amd64,linux/arm64 \
        -t registry.example.internal/ddwrt-portal:0.1.0 --push .
    # set the image (and, for the Ingress, host and class) in k8s/
    kubectl apply -k k8s

- One replica, `Recreate` strategy: the database is SQLite on a
  ReadWriteOnce volume, and in-progress router changes are tracked in memory.
- The pod runs as uid 10001 with a read-only root filesystem and no
  capabilities. It needs to reach the routers on TCP 22 and
  `download1.dd-wrt.com` on 443; it never uses the Kubernetes API.
- Optional Secret `ddwrt-portal` supplies `DEFAULT_PASSWORD`, `BACKUP_KEY`
  or `ALERT_WEBHOOK`:

      kubectl -n ddwrt-portal create secret generic ddwrt-portal \
          --from-literal=DEFAULT_PASSWORD='...'

- Expose it over HTTPS only. With the Ingress terminating TLS the session
  cookie is marked Secure (`FORWARDED_ALLOW_IPS` is set for that).
- To move from Docker, export there and import here (see Moving the portal).

## Moving the portal

Account → Export and import. An export is one file holding the database
(routers, backups, managed settings, users), the SSH key the routers trust
and the backup key. It is encrypted with a passphrase chosen at export time,
which is not stored. Sign-in sessions are left out.

Importing replaces everything in the receiving portal and signs you out; sign
in with a user from the export. The replaced data is moved to
`/data/before-import-<time>/`, not deleted. An import is refused while a
router is mid-change, or if `BACKUP_KEY` is set to a different key than the
export's. Stop the old portal afterwards so two portals do not manage the
same routers.

## State

Everything lives in the `/data` volume: `portal.db` (SQLite), `id_ed25519`
(the portal's SSH key) and `backup.key`. Deleting the volume means re-enrolling
the routers and losing backup history.

## Backups

Each router's full config (`nvram backup`) is fetched on enrolment, on demand,
and every `CONFIG_INTERVAL` seconds. A new copy is stored only if the config
changed, so edits made outside the portal appear in the history. Backups are encrypted in the database;
the UI shows a setting-by-setting diff with credentials hidden, and can
download the original file for restoring on the router.

A stored backup can be restored to its router from the Backups page. The
portal shows what would change and how many Wi-Fi clients are connected,
backs up the current config first, restores, checks every setting, saves, and
reboots the router. `nvram restore` erases all settings before loading a file
and accepts truncated files, so never run it by hand on a file that has not
been fully validated; the portal parses and checksums the file first.

Set `BACKUP_KEY` (a Fernet key) to keep the encryption key outside the volume.
Without it a key is generated at `/data/backup.key`, which protects a copied
database but not a copied volume. Changing the key makes existing backups
unreadable.

## Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` | Where the database and SSH key are kept |
| `POLL_INTERVAL` | `60` | Seconds between status polls |
| `CONFIG_INTERVAL` | `300` | Seconds between full config checks (backup if changed, drift refresh) |
| `FIRMWARE_CHECK_HOURS` | `12` | Hours between checks for a newer build |
| `FIRMWARE_URL` | DD-WRT betas | Base URL of the download site |
| `FIRMWARE_MAX_JUMP_DAYS` | `62` | Days between running and requested build before a warning |
| `FIRMWARE_WRITE_TIMEOUT` | `900` | Seconds allowed for the router to write an image |
| `FIRMWARE_BOOT_TIMEOUT` | `600` | Seconds allowed for the router to boot the new build |
| `SESSION_HOURS` | `168` | Hours a sign-in lasts |
| `DEFAULT_USER` | `admin` | User created with a new database |
| `DEFAULT_PASSWORD` | `admin` | Its first password; must be changed at first sign-in |
| `ALERT_OFFLINE_AFTER` | `300` | Seconds of silence before an offline alert |
| `ALERT_WEBHOOK` | unset | URL that receives alert changes as `{"text": ...}` |
| `BACKUP_KEEP` | `200` | Stored backups kept per router |
| `BACKUP_KEY` | generated | Fernet key for encrypting stored backups |
| `SSH_CONNECT_TIMEOUT` | `10` | Seconds |
| `SSH_COMMAND_TIMEOUT` | `30` | Seconds |

## Develop

    uv sync
    uv run pytest
    DATA_DIR=./data uv run uvicorn app.main:app --reload --port 8080

`tests/fixtures/` holds real collector output from each hardware platform
(Broadcom ARM, Broadcom MIPS, MediaTek).
