# Configuration

Every setting the server reads, where it comes from and what it defaults to.

Configuration lives in `.env` next to `server.py`. The file is read once at startup: a change needs a restart. `.env` is gitignored, so `git pull` never touches it. `.env.example` is the annotated template — copy it and edit.

The defaults below are the ones in `core.py`. Where `.env.example` ships a different value, that is noted.

## Router connection

| Variable | Default | Description |
|---|---|---|
| `KEENETIC_HOST` | `http://192.168.1.1` | Router base URL, used for RCI |
| `KEENETIC_USER` | `admin` | Router account with RCI access |
| `KEENETIC_PASS` | `password` | That account's password. The shipped default is a placeholder |

## Server

| Variable | Default | Description |
|---|---|---|
| `MCP_SECRET` | `changeme` | Secret token in the URL path. All routes are under `/<MCP_SECRET>`. The shipped default is a placeholder |
| `MCP_PORT` | `9584` | TCP port the server listens on. Always self-protected from the write tools |

## Router config backup

| Variable | Default | Description |
|---|---|---|
| `BACKUP_ENABLED` | `false` | `true` activates the scheduled backup thread |
| `BACKUP_SCHEDULE` | `0 11 * * 0` | Cron format: `minute hour day month weekday` |
| `BACKUP_RSYNC_HOST` | empty | Remote host. The rsync branch runs when `HOST`, `USER` and `PATH` are all set: the config is staged in `/tmp` (RAM), synced, and the temporary file removed, so the flash drive is not written to |
| `BACKUP_RSYNC_USER` | empty | SSH user on that host |
| `BACKUP_RSYNC_KEY` | empty | Path to the SSH private key. `.env.example` ships `/opt/etc/keenetic-backup-rsa` |
| `BACKUP_RSYNC_PATH` | empty | Destination directory on that host. `.env.example` ships `/share/backups/keenetic` |
| `BACKUP_PATH` | `/tmp/keenetic-backup` | Where the local fallback writes. It applies only when no rsync target is configured, and is never touched on the rsync branch. The built-in default is a RAM path, so fallback backups do not survive a reboot unless it points at persistent storage; `.env.example` ships `/opt/var/backup/keenetic` on the USB flash for that reason |
| `BACKUP_KEEP` | `0` | How many local backup files the fallback branch keeps. `0` keeps every file. It has no effect when the rsync target is configured — nothing on the receiver is ever rotated or deleted. `.env.example` ships `8` |

## Backup of the server's own files

| Variable | Default | Description |
|---|---|---|
| `BACKUP_MCP_CONFIG` | `true` | Back up `.env`, `watch_rules.json` and the init script alongside the router config. Requires an rsync destination |
| `BACKUP_MCP_INIT` | `/opt/etc/init.d/S99keenetic-mcp` | Path to the installed Entware init script |

See [backup.md](backup.md) for the layout on the receiver, change detection and verification.

## Protected objects

The write tools refuse to touch anything listed here. Values are comma-separated.

| Variable | Default | Description |
|---|---|---|
| `MCP_PROTECTED_PORTS` | empty | External ports that must not be forwarded or removed |
| `MCP_PROTECTED_PROXY_NAMES` | empty | KeenDNS proxy names that must not be changed or removed |
| `MCP_PROTECTED_UPSTREAMS` | empty | `host:port` upstreams that must not be pointed at |

The server's own port (`MCP_PORT`), its `127.0.0.1:<MCP_PORT>` upstream and the `keenetic-mcp` proxy name are always protected. They need no entry here and cannot be switched off. Leaving all three variables empty protects only that channel.

**The refusal runs in both directions.** A protected port cannot be removed, and a rule for that port cannot be created either. Listing a port that is not forwarded today therefore also blocks publishing it to the WAN later.

`80` and `443` belong in `MCP_PROTECTED_PORTS` as soon as anything is published through a reverse proxy. KeenDNS setups are unaffected: cloud mode needs no forwarding, and in direct mode the router itself holds 80 and 443.

Review the list when the network topology changes.

`set_dns_host` is not covered by any `MCP_PROTECTED_*` variable; there is no protected-names list for DNS records. What guards an existing record is that a name already resolving elsewhere is refused.

A write tool that refuses names the port or the object in its message.

## Plain-HTTP tool route

| Variable | Default | Description |
|---|---|---|
| `MCP_HTTP_TOOLS` | `true` | `false` turns the whole `/tool/` and `/tools` route off. `/reboot` and the MCP protocol are unaffected |
| `MCP_HTTP_TOOL_ALLOWLIST` | empty | Comma-separated tool names served over HTTP even though they change state. Empty means every read-only tool and no mutating one |

See [http-api.md](http-api.md) for the route itself and the list of tools it refuses.

## Event watcher

| Variable | Default | Description |
|---|---|---|
| `MCP_WATCH` | `true` | `false` stops the watcher thread from starting. With no rules file it does not start either way |
| `MCP_WATCH_RULES` | `watch_rules.json` | Rules file. A relative path is resolved next to `server.py` |
| `MCP_WATCH_STATE` | `/tmp/keenetic-mcp-watch.json` | Last seen log position, previous RCI snapshots, cooldowns. Keep it in `/tmp` (RAM); this project does not write to the USB stick |
| `MCP_WATCH_INTERVAL` | `10` | Default seconds between log polls, minimum 2. A rule overrides it with `interval` |

The watcher reads the router over its own RCI session against `KEENETIC_HOST` with the same credentials. There is nothing extra to configure for it.

See [watcher.md](watcher.md) for the rules file format.

## Secrets referenced from watcher rules

Any `UPPERCASE` name set in `.env` can be referenced from `watch_rules.json` as `$NAME` in a rule's `url`, `headers`, `proxy` or `body`. Only uppercase names are taken from the environment; event fields are lowercase, so the two namespaces cannot collide.

```
TG_TOKEN=123456:AA...
TG_CHAT_ID=100000000
TG_PROXY=http://user:password@203.0.113.10:14346
```

## Save confirmation

After a write, the save-confirmation loop polls `show/last-change`'s checksum every 250 ms for at most 7 s, then returns `save_detail.status: "pending"`. Polling blocks this single-threaded server, so a write call can take up to 7 s. Read tools are unaffected.

Both values are module constants in `helpers.py` — `SAVE_VERIFY_TIMEOUT_MS = 7000` and `SAVE_VERIFY_INTERVAL_MS = 250`. They are not settings: nothing reads them from the environment, and changing either means editing `helpers.py`.
