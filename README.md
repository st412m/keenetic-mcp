# Keenetic MCP Server

MCP (Model Context Protocol) server for Keenetic routers. It runs directly on the router via Entware, with no dependencies outside the Python standard library. It gives an AI assistant 49 tools for monitoring and managing the router, reachable over MCP and over plain HTTP, plus a background watcher that calls out from the router when a rule matches.

## Requirements

| Item | Requirement |
|---|---|
| Router | Keenetic with Entware support |
| Storage | USB drive formatted as ext4, Entware installed on it |
| Python | Python 3.x, standard library only (`requirements.txt` lists no packages) |
| Port | 9584 by default |

| Architecture | Models | Status |
|---|---|---|
| mipsel | KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810 | Tested |
| mips | KN-2410, KN-2510, KN-2010, KN-2110, KN-3610 | Should work, not tested |

Tested on Keenetic Giga KN-1010 + KN-1011 (Mesh), KeeneticOS 5.1.5 (`5.01.C.5.0-0`), Entware `mipselsf`.

## Installation

### Step 1 — Install Entware

Format a USB drive as ext4 and plug it into the router. In the router web interface go to Applications -> OPKG and make sure the drive is selected as the storage.

Download the installer for the router model and copy it to the `install` folder on the USB drive via SMB (`\\192.168.1.1`):

- KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810: https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz
- KN-2410, KN-2510, KN-2010, KN-2110, KN-3610: https://bin.entware.net/mipssf-k3.4/installer/mips-installer.tar.gz

Entware installs automatically. Check the router system log for:

    [5/5] Installation of the "Entware" package system is complete!

### Step 2 — SSH into the router

    ssh root@192.168.1.1 -p 222

Default password: `keenetic`. Change it immediately:

    passwd

### Step 3 — Install dependencies

    opkg update
    opkg install python3 git git-http nano curl rsync

### Step 4 — Clone and configure

    cd /opt
    git clone https://github.com/st412m/keenetic-mcp.git
    cd keenetic-mcp
    cp .env.example .env
    nano .env

### Step 5 — Set up autostart

    cp init.d/S99keenetic-mcp /opt/etc/init.d/
    chmod +x /opt/etc/init.d/S99keenetic-mcp
    /opt/etc/init.d/S99keenetic-mcp start

Verify it is running:

    /opt/etc/init.d/S99keenetic-mcp status
    curl http://localhost:9584/YOUR_MCP_SECRET

### Step 6 — Configure external HTTPS access

In the Keenetic web interface go to Network Rules -> Domain name -> Web application access and click Add:

- Name: `keenetic-mcp`
- Internet access: Open access
- Device: This Keenetic device
- Protocol: HTTP
- TCP Port: 9584

### Step 7 — Connect to Claude

See [Connecting to claude.ai](#connecting-to-claudeai).

### Updating

    cd /opt/keenetic-mcp
    git pull --ff-only
    /opt/etc/init.d/S99keenetic-mcp restart
    /opt/etc/init.d/S99keenetic-mcp status

`.env` and `watch_rules.json` are gitignored, so a pull never touches credentials or rules. The autostart script is not updated by a pull of the working copy — after a release that changes it, copy it over again from `init.d/`. All three are what `backup_mcp_config` preserves.

After adding or removing tools, start a new chat: MCP clients cache `tools/list` for the lifetime of a session.

## Configuration

Settings live in `.env` next to `server.py`, read once at startup. The minimum:

    KEENETIC_HOST=http://192.168.1.1
    KEENETIC_USER=admin
    KEENETIC_PASS=your_router_password
    MCP_SECRET=some_random_secret_string
    MCP_PORT=9584

| Variable | Default | Purpose |
|---|---|---|
| `KEENETIC_HOST` / `KEENETIC_USER` / `KEENETIC_PASS` | `http://192.168.1.1`, `admin` | Router connection for RCI |
| `MCP_SECRET` / `MCP_PORT` | `changeme`, `9584` | Secret token in the URL path, and the listening port |
| `MCP_PROTECTED_PORTS` | empty | External ports the write tools must never forward or remove |
| `MCP_PROTECTED_PROXY_NAMES` | empty | KeenDNS proxy names the write tools must never change |
| `MCP_PROTECTED_UPSTREAMS` | empty | `host:port` upstreams the write tools must never point at |
| `MCP_HTTP_TOOLS` | `true` | Plain-HTTP tool route on or off |
| `MCP_HTTP_TOOL_ALLOWLIST` | empty | State-changing tools allowed over that route |
| `MCP_WATCH` / `MCP_WATCH_RULES` | `true`, `watch_rules.json` | Event watcher, and its rules file |
| `BACKUP_ENABLED` / `BACKUP_SCHEDULE` | `false`, `0 11 * * 0` | Scheduled router config backup, in cron format |
| `BACKUP_RSYNC_HOST` / `_USER` / `_KEY` / `_PATH` | empty | rsync-over-SSH destination; without it backups stay local |
| `BACKUP_MCP_CONFIG` | `true` | Also back up `.env`, `watch_rules.json` and the init script |

Full reference, including the protected-object rules: [docs/configuration.md](docs/configuration.md).

The watcher stays idle until it has rules. To turn it on, copy the example and edit it:

    cp watch_rules.example.json watch_rules.json
    nano watch_rules.json

## Connecting to claude.ai

After Step 6 the server is reachable at:

    https://keenetic-mcp.YOUR_DDNS.keenetic.link/YOUR_MCP_SECRET

In Claude.ai go to Settings -> Integrations -> Add custom connector and paste that URL.

## Tools

49 tools. One line per group; full descriptions in [docs/tools.md](docs/tools.md).

- **System** — `get_system_info`, `get_internet_status`, `get_interfaces`, `get_traffic`, `get_vpn_status`
- **WiFi** — `get_wifi`, `get_wifi_stations`, `get_site_survey`, `get_channel_analysis`
- **Clients** — `get_clients`, `get_unregistered_clients`, `get_dhcp_leases`, `get_dhcp_static`, `register_client`, `update_client`, `block_client`, `unblock_client`
- **Config, read-only** — `get_config`, `get_config_state`, `diff_saved_config`, `get_port_forwarding`, `get_firewall_rules`, `get_keendns_mappings`, `get_dns_proxy`, `get_schedule`, `rci_query`
- **Config, write** — `set_port_forwarding`, `remove_port_forwarding`, `set_keendns_mapping`, `remove_keendns_mapping`, `set_dhcp_host`, `remove_dhcp_host`, `set_dns_host`, `remove_dns_host`
- **Diagnostics** — `get_log`, `get_log_by_device`, `run_ping`, `get_watch_status`, `test_watch_rule`
- **Mesh** — `get_mesh_nodes`, `get_extender_log`
- **Storage** — `get_media`, `get_opkg_status`
- **Backups and management** — `backup_config`, `backup_mcp_config`, `list_backups`, `dump_log`, `reboot`
- **Security** — `get_web_access`

Every write tool takes `dry_run`, defaulting to `true`: it returns the payload it would send and changes nothing.

Beyond MCP, the tools are also reachable as `GET /<MCP_SECRET>/tool/<name>?arg=value` — see [docs/http-api.md](docs/http-api.md). The push side, where the router calls out on a matching event, is [docs/watcher.md](docs/watcher.md). Backups are [docs/backup.md](docs/backup.md).

## Limitations

- The server is single-threaded: it handles one request at a time, so a polling loop blocks MCP calls. Keep poll intervals at 60 s or more
- `get_log` allows the router 30 s to answer and typically takes around ten. `get_site_survey` and `get_channel_analysis` are also slow. None of them belong in a polling loop
- A write tool polls for up to 7 s waiting for the save to land on disk, then answers `pending` rather than claiming success
- MCP clients cache `tools/list` for the lifetime of a session. A changed tool list needs a new chat
- The plain-HTTP route serves read-only tools only. State-changing tools return 403 unless allowlisted
- The watcher keeps its state in `/tmp` (RAM), so a router reboot resets its baseline
- `mips` architecture is untested. `mipsel` is tested on KeeneticOS 5.1.5; other firmware branches are not
- Log timestamps from before NTP syncs are wrong. `get_log` flags them but a `since`/`until` window still matches them

## Troubleshooting

| Symptom | Command |
|---|---|
| Port 9584 does not answer | `/opt/etc/init.d/S99keenetic-mcp status` then `grep ' /opt ' /proc/mounts` |
| `Address already in use`, old version answers | `/opt/etc/init.d/S99keenetic-mcp status` — it reports every live instance |
| Nothing in the log | `cat /tmp/keenetic-mcp.log` |

⚠️ If `/opt` is not mounted, never rebind the OPKG drive over SSH on the router itself: `dropbear` lives on `/opt` and the rebind kills the session before it completes. Use the web interface or another machine.

More symptoms: [docs/troubleshooting.md](docs/troubleshooting.md).

## Security

- The endpoint is protected by a secret token in the URL path. HTTPS is handled by the Keenetic built-in SSL certificate
- Never commit `.env` — it is in `.gitignore`. Change the default SSH password after installation
- `rci_query` is GET-only and cannot modify the router; `crypto`, `ppp`, `user` and `running-config` subtrees are refused outright
- `get_config` masks secrets by default. `include_secrets: true` puts passwords and keys into the chat transcript
- The write tools always protect the server's own port, upstream and proxy name. Add anything else via `MCP_PROTECTED_*`. Protection applies to creating a rule as well as removing one, so listing a port also forbids re-publishing that service to the WAN
- `set_dns_host` is not covered by `MCP_PROTECTED_*`. What guards an existing record is that a name already resolving elsewhere is refused
- The plain-HTTP route serves read-only tools only. Adding a tool to `MCP_HTTP_TOOL_ALLOWLIST` hands out a write key: the URL secret ends up in config files, automation traces and proxy logs. `MCP_HTTP_TOOLS=false` turns the route off
- Treat `watch_rules.json` as credential material — it can carry bot tokens and internal URLs. Prefer `$NAME` placeholders resolved from `.env`
- `backup_mcp_config` copies `.env` and `watch_rules.json` to the backup destination in clear text. Restrict that share to one account
- `test_watch_rule` masks credentials in what it renders, not in what it sends: `dry_run: false` sends the real values

## Links

- [docs/](docs/) — [configuration](docs/configuration.md), [tools](docs/tools.md), [HTTP API](docs/http-api.md), [watcher](docs/watcher.md), [backups](docs/backup.md), [troubleshooting](docs/troubleshooting.md), [internals](docs/internals.md)
- [CHANGELOG.md](CHANGELOG.md) — version history
- [LICENSE](LICENSE) — MIT
