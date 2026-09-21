# Backups

The built-in scheduler for the router configuration, and the separate backup of the server's own unversioned files.

## Router config backup

A built-in scheduler backs up the router configuration (`running-config`) via the RCI API.

- A background thread checks the schedule every minute. No cron required
- The config is fetched via an authenticated RCI call
- With `BACKUP_RSYNC_HOST`, `BACKUP_RSYNC_USER` and `BACKUP_RSYNC_PATH` all set, the config is written to `/tmp` (RAM), synced to the remote host via rsync over SSH, and the temporary file is removed. The flash drive is not written to, and `BACKUP_PATH` is not used
- With no rsync target configured, the config is saved locally in `BACKUP_PATH`, and `BACKUP_KEEP` files are kept. `BACKUP_KEEP=0`, the built-in default, keeps every file

To enable, add to `.env`:

```
BACKUP_ENABLED=true
BACKUP_SCHEDULE=0 11 * * 0
BACKUP_RSYNC_HOST=192.168.1.2
BACKUP_RSYNC_USER=admin
BACKUP_RSYNC_KEY=/opt/etc/keenetic-backup-rsa
BACKUP_RSYNC_PATH=/share/backups/keenetic
```

Without the `BACKUP_RSYNC_*` block the scheduler still runs, on the local fallback branch. Point `BACKUP_PATH` at persistent storage in that case: its built-in default, `/tmp/keenetic-backup`, is cleared on reboot.

### Schedule format

Standard cron: `minute hour day month weekday`.

```
0 11 * * 0   — every Sunday at 11:00
0 3  * * *   — every day at 03:00
0 */6 * * *  — every 6 hours
```

### rsync setup

Install rsync and set up SSH key authentication:

```bash
opkg install rsync
ssh-keygen -t rsa -f /opt/etc/keenetic-backup-rsa
cat /opt/etc/keenetic-backup-rsa.pub | ssh user@nas-host "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Trigger a backup manually at any time with `backup_config`, and verify that files landed on the NAS with `list_backups`.

## Backing up the server's own configuration

A router config backup does not save this server. Three files are restored by neither `git pull` nor the router's own backup:

| File | Why git cannot restore it |
|---|---|
| `.env` | gitignored — it holds the router password and tokens |
| `watch_rules.json` | gitignored — the example file is the one under version control |
| `/opt/etc/init.d/S99keenetic-mcp` | installed by hand; a pull of the working copy never touches the installed copy |

They ride along with the router config on the same schedule. With an rsync destination set, `BACKUP_MCP_CONFIG` defaults to true and no further configuration is needed.

```
BACKUP_MCP_CONFIG=true
BACKUP_MCP_INIT=/opt/etc/init.d/S99keenetic-mcp
```

### Layout on the receiver

A mirror plus a dated snapshot:

```
<BACKUP_RSYNC_PATH>/
├── keenetic-config-YYYY-MM-DD.json     the router config
├── mcp-config/                         always current, rewritten every run
│   ├── .env  watch_rules.json  S99keenetic-mcp  manifest.json
└── mcp-config-YYYY-MM-DD/              written ONLY when the content changed
```

A corrupted file counts as a change and produces its own snapshot; the previous snapshot still holds the last good state.

**Nothing is ever deleted on the receiver.** There is no rotation. If the snapshots pile up, delete them by hand.

### Change detection

`manifest.json` inside the mirror is compared: md5 of each file, plus its true size, mode and mtime.

No state file is written to the USB stick, and mtimes are not used to decide anything. The manifest records when each file was last edited, independently of what the transport preserves.

File copies use `shutil.copy2`, so the mirror carries the originals' mtime and mode rather than the moment of copying.

### Verification

After sending, the mirror is read back off the receiver and md5-compared against what was staged. The result is the `verified` field. `backup_mcp_config` runs synchronously and returns:

```json
{
  "ok": true,
  "first_run": false,
  "changed": false,
  "mirror": "/share/backups/keenetic/mcp-config/",
  "snapshot": null,
  "verified": true,
  "files": [
    {"name": ".env", "size": 1251, "mode": "0600", "mtime": "2026-08-03 22:18:01"}
  ],
  "error": null
}
```

Staging is in `/tmp` (tmpfs/RAM); the flash drive is not written to. The whole set is a few kilobytes, so the run costs a second or two.

The outcome of this transfer does not change the router-config backup's result. A verification automation watching for `keenetic-config-*.json` will not start reporting failure because of this second, unrelated transfer.

### What is excluded

Two things are not in the set:

- The rsync private key (`BACKUP_RSYNC_KEY`)
- `running-config` itself, which has its own file

### Security

⚠️ The set contains credentials in clear text. Restrict the share to a single account. The transfer does not go through the write tools' protection list — it is a file copy, not a router change.

`.env` arrives as a dot-file: busybox `ls -l` does not list it, and Samba hides it from Windows Explorer by default (`hide dot files`), so it can look missing when it is not. Verify with `ls -la`, with `rsync --list-only`, or with `list_backups`.

## Runtime writes

PID file, server log and watcher state live in `/tmp` (RAM). Nothing is written to the USB flash at runtime.
