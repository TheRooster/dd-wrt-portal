# DD-WRT Portal

Central management for DD-WRT access points. The portal talks to each router
over SSH; nothing is installed on the routers.

## Run

    docker compose up -d --build

Open http://127.0.0.1:8080 and add each router by IP and root password. The
password is used once to install the portal's SSH public key
(`sshd_authorized_keys` in nvram) and is never stored. After that the portal
logs in with its own key and pins the router's SSH host key.

The portal has no login of its own yet, so it is published on loopback only.

## Managed settings

The portal keeps its own names for settings ("portal keys"). Radio settings
are named by band (`wifi24.ssid`, `wifi5.wpa_psk`); everything else uses its
DD-WRT nvram name. Each router has a mapping, confirmed when it is enrolled,
that says which of its radios serves each band and gives the router's own
name for any setting it calls something else. `app/profile.py` holds this.

The Managed settings page has:

- named settings the portal understands (network names, a passphrase per
  band with "Same as 5 GHz", time, access, logging, reboot schedule) and a
  channel per router and band;
- every other setting that all routers share, any of which can be managed as
  a plain value. Secrets, hardware details, per-router identity, and radio
  settings known to be chipset-specific are not offered.

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
running one. A larger jump, or a downgrade, needs an extra confirmation. The
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
