# Keenetic MCP Server

MCP (Model Context Protocol) server for Keenetic routers. Runs directly on the router via Entware. Allows Claude AI to monitor and manage your router.

Tested on: **Keenetic Giga KN-1010 + KN-1011 (Mesh)**, KeeneticOS **5.1.1** (`5.01.C.1.0-0`), Entware `mipselsf`.

Current version: **2.6.0** — 42 tools, no dependencies outside the Python standard library. Tools are reachable over MCP and, since 2.6.0, over plain HTTP for clients that do not speak the protocol (Home Assistant, curl, shell scripts).

## Available Tools

### System Monitoring
- `get_system_info` — firmware version, uptime, CPU load, memory usage. Also returns an `mcp` block with `mcp_server_version`, `uptime_human` and `boot_time` (exact reboot timestamp — useful because KN-1010 has no RTC and the log clock jumps at boot)
- `get_internet_status` — internet connection status and external IP address
- `get_interfaces` — all network interfaces status and configuration
- `get_traffic` — top clients by traffic with total rx/tx summary
- `get_vpn_status` — status of all VPN interfaces (WireGuard, IPsec, L2TP, PPTP) with peer details

### WiFi
- `get_wifi` — WiFi radio status: channel, bandwidth, bitrate, temperature, connected stations count
- `get_wifi_stations` — currently connected WiFi stations with signal strength (RSSI), speed, traffic and mesh node (controller/extender)
- `get_site_survey` — scan nearby WiFi networks
- `get_channel_analysis` — analyze WiFi channel congestion and recommend the least busy channel for 2.4GHz and 5GHz

### Clients
- `get_clients` — all devices in the network with IP, MAC, signal, traffic and mesh node (controller/extender)
- `get_unregistered_clients` — active devices not yet registered in the router (unknown devices)
- `get_dhcp_leases` — devices with an active DHCP lease from the pool, including expiry time
- `get_dhcp_static` — static DHCP reservations (`ip dhcp host`). Complements `get_dhcp_leases`: a device with a fixed binding does not appear as a pool lease
- `register_client` — register a device by MAC, assign a name and optionally a static IP
- `update_client` — update name or static IP of a registered device
- `block_client` — block a device by MAC address (works for both registered and unregistered devices)
- `unblock_client` — unblock a previously blocked device by MAC address

### Configuration & network rules (read-only)
- `get_config` — the router's `running-config` with an optional case-insensitive regex filter. Secrets (`md5`, `nthash`, `psk`, `password`, `private-key`, long base64 keys) are masked unless `include_secrets: true`
- `get_port_forwarding` — port forwarding / static NAT rules (`ip static`)
- `get_firewall_rules` — access-lists with their entries, `ip firewall` settings and interface access-groups
- `get_keendns_mappings` — KeenDNS / web application access mappings (`ip http proxy`) with their upstreams
- `get_dns_proxy` — DNS proxy status: upstream resolvers with their DoT SNI, and the static A/AAAA records the router serves
- `get_schedule` — router schedules (the firmware auto-update window, and any others) with their name, weekday/time actions and seconds until the next fire. The auto-update window is otherwise a hardcoded assumption in recovery automations — this reads it from the router
- `rci_query` — raw **read-only** query against the RCI tree: `GET /rci/show/<path>`, or `GET /rci/<path>` with `config_tree: true`. It cannot write, by construction — writing to RCI requires a POST body and this tool never sends one. Use the default tree for state (`ntp`, `components`, `ndns`, `interface/GigabitEthernet1`) and `config_tree` to inspect the exact write-shape of a settings branch (`ip/static`, `ip/http/proxy`, `ip/dhcp/host`). Schedules and the DNS proxy have their own named tools now. Blacklisted subtrees: `running-config` (use `get_config`), `crypto`, `ppp`, `user`. Output is capped at 40 000 characters

### Configuration changes (write)

Every tool below takes `dry_run`, and **it defaults to `true`**: the tool returns the exact payload it would send and changes nothing. A real write is followed by `system configuration save` (raw RCI writes do not survive a reboot without it) and by a re-read of the affected branch, so the answer contains a before/after diff rather than a "command sent" claim.

- `set_port_forwarding` — create or update an `ip static` rule. The target is addressed by **MAC**, not IP: pass `to_host` as a MAC, or as the IP of a registered host, which is resolved for you and refused if the router does not know it (a typo would otherwise create a rule forwarding nowhere). Supports `to_port`, `end_port` (ranges), `comment` and `enable` — the last one being the per-rule `disable` flag
- `remove_port_forwarding` — delete a rule by `index` (from `get_port_forwarding`) or by `port`. Ambiguous matches are refused rather than guessed
- `set_keendns_mapping` / `remove_keendns_mapping` — manage `ip http proxy` entries: name → upstream host:port, published on the ndns domain with ssl redirect
- `set_dhcp_host` / `remove_dhcp_host` — manage `ip dhcp host` reservations. An IP already reserved for a different MAC is refused. Note that the device *name* lives in the known-host tree — use `register_client` / `update_client` for that

**Guard rails are in code, not in the description — and the list is yours to configure.** The server always protects *itself*, regardless of configuration and with no way to switch it off: its own port (`MCP_PORT`), the `127.0.0.1:<MCP_PORT>` upstream and the `keenetic-mcp` proxy name. A mistake therefore can never close the channel this server is reached through. Everything else you want shielded from the write tools goes in `.env`, comma-separated:

- `MCP_PROTECTED_PORTS` — external ports that must not be forwarded or removed
- `MCP_PROTECTED_PROXY_NAMES` — KeenDNS proxy names that must not be changed or removed
- `MCP_PROTECTED_UPSTREAMS` — `host:port` upstreams that must not be pointed at

A protected object is refused outright; if you genuinely need to change one, do it in the web interface. Leave the variables empty to protect only the server's own channel. See `.env.example` for the annotated list.

### Diagnostics
- `get_log` — system log with timestamps, optional line count, text filter and time window (`since` / `until`, accepting `HH:MM`, `HH:MM:SS` or `Jul 24 08:00`)
- `get_log_by_device` — system log filtered by device MAC address, IP address or name
- `run_ping` — ping a host directly from the router, returns latency and packet loss

### Mesh
- `get_mesh_nodes` — Mesh Wi-Fi nodes: controller and extenders with firmware, uptime and connection speed
- `get_extender_log` — system log directly from mesh extender(s); extenders are discovered automatically, optional filter by IP, line count and text

### Storage & Entware
- `get_media` — internal flash and USB drives: partition UUID, label, filesystem, state, free space and which subsystem uses the partition (e.g. `opkg`). Use it to check whether the Entware drive is healthy
- `get_opkg_status` — which drive OPKG is bound to, the initrc path, and whether `/opt` is **actually mounted**. If `opt_mounted` is false, the server you are talking to is running on borrowed time

### Backups & management
- `backup_config` — trigger a router config backup right now
- `list_backups` — list backup files already present on the NAS (`rsync --list-only`) — confirms the scheduled backups actually arrive
- `dump_log` — snapshot the current router log and rsync it to the NAS backup path (RAM staging, no flash writes)
- `reboot` — reboot the router

### Security
- `get_web_access` — web applications exposed to the internet via Keenetic DDNS, read from the generated nginx config. `get_keendns_mappings` shows the same thing from the other side (running-config) — comparing the two catches stale entries

## Project structure

As of 2.5.0 the server is split into flat modules in the repository root. This is a flat layout on purpose, not a Python package: `init.d` runs `python server.py` directly, so the script's directory is on `sys.path` and plain imports work with no change to how the addon starts.

- `core.py` — `.env` loading, router authentication, the RCI client, and all mutable state (session cookie, protected-object sets)
- `backup.py` — the config-backup scheduler and rsync
- `helpers.py` — running-config parsing, secret masking, log formatting
- `tools_network.py` — read tools for clients, WiFi, interfaces, logs, VPN, DNS proxy, mesh
- `tools_system.py` — system info, client management, ping, media/opkg, reboot, schedules
- `tools_config.py` — running-config readers and the write tools, with the protection guards
- `registry.py` — the tool table and dispatcher
- `http_tools.py` — the plain-HTTP access policy and query-string argument coercion (added in 2.6.0; keeps the HTTP surface out of the registry and the tool modules)
- `server.py` — the HTTP / MCP transport and entry point

Behaviour is identical across the split. Adding a tool means writing the function in the relevant `tools_*` module and registering it in `registry.py`.

## Config Backup

The server includes a built-in scheduler that automatically backs up the router configuration (`running-config`) via the RCI API.

**How it works:**
- A background thread checks the schedule every minute (no cron required)
- Config is fetched via authenticated RCI API call
- If `BACKUP_RSYNC_HOST` is set: config is written to `/tmp` (RAM) and synced to the remote host via rsync over SSH — the flash drive is never written to
- If no rsync host is set: config is saved locally in `BACKUP_PATH` with rotation

**To enable**, add to your `.env`:

```
BACKUP_ENABLED=true
BACKUP_SCHEDULE=0 11 * * 0
BACKUP_RSYNC_HOST=192.168.1.2
BACKUP_RSYNC_USER=admin
BACKUP_RSYNC_KEY=/opt/etc/keenetic-backup-rsa
BACKUP_RSYNC_PATH=/share/backups/keenetic
```

**Schedule format** is standard cron: `minute hour day month weekday`

```
0 11 * * 0   — every Sunday at 11:00
0 3  * * *   — every day at 03:00
0 */6 * * *  — every 6 hours
```

**If using rsync**, install it first and set up SSH key authentication:

```bash
opkg install rsync
ssh-keygen -t rsa -f /opt/etc/keenetic-backup-rsa
cat /opt/etc/keenetic-backup-rsa.pub | ssh user@nas-host "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

You can trigger a backup manually at any time via `backup_config`, and verify that files really landed on the NAS via `list_backups`.

## Plain HTTP endpoints

Besides the MCP protocol (POST `/<MCP_SECRET>`), the server answers a few plain
`GET` requests, authenticated by the same secret token in the URL path. They
exist for clients that cannot speak MCP — Home Assistant's `rest` sensor,
`rest_command` and `command_line`, cron jobs, plain `curl`.

### Calling tools over HTTP

```
GET /<MCP_SECRET>/tool/<name>?arg=value
GET /<MCP_SECRET>/tools
```

`/tool/<name>` runs the same function the MCP client would and answers with

```json
{"ok": true, "tool": "get_system_info", "args": {}, "result": { ... }}
```

Most tools return a JSON document; it is parsed into `result` rather than nested
as a string, so a template can index into it directly. Add `&raw=1` to get the
tool's own text as `text/plain` instead — convenient for `command_line` sensors
and for reading logs by eye. `/tools` lists what is servable right now, with
parameter names, declared types and a `mutating` flag.

**This route is read-only by default, and that is deliberate.** A secret in a
URL is a weak credential: it lands in `configuration.yaml`, in automation
traces, in shell history and in any proxy log along the way. So the 13 tools
that change state — all six write tools plus `reboot`, `register_client`,
`update_client`, `block_client`, `unblock_client`, `backup_config` and
`dump_log` — answer **403** here no matter what. The remaining 29 read tools are
served. To lift the gate for a specific tool, name it explicitly:

    MCP_HTTP_TOOL_ALLOWLIST=register_client

Setting `MCP_HTTP_TOOLS=false` turns the whole route off; `/reboot` and the MCP
protocol are unaffected by both variables.

Arguments are coerced using each tool's declared `inputSchema`, not by guessing
from the text — a value that does not fit its declared type is a **400** with an
explanation, never a silently wrong call:

```
$ curl 'http://192.168.1.1:9584/SECRET/tool/get_log?lines=zzz'
{"ok": false, "tool": "get_log", "error": "parameter 'lines' must be an integer, got 'zzz'"}
```

Unknown and missing parameters are 400 as well, and the error lists what the
tool accepts. Booleans take `true/false`, `1/0`, `yes/no` or `on/off`.

⚠️ The server is single-threaded: it handles one request at a time, so a busy
polling loop will block MCP calls. Keep `scan_interval` at 60 s or more, and
keep the slow tools (`get_log`, which allows the router 30 s to answer,
`get_site_survey`, `get_channel_analysis`) out of anything that polls.

### Reboot endpoint

```
GET /<MCP_SECRET>/reboot
```

It runs the same `reboot` tool (`system reboot` over RCI) and returns
`{"ok": true, "log_synced": <bool>, "result": "Reboot command sent"}`. Protected
by the same secret token in the URL path. This endpoint predates the tool route
and stays separate from it — rebooting a router is not something that should
share a URL shape with reading a sensor.

Before rebooting, the endpoint first snapshots the current router log and rsyncs
it to the NAS backup path (see Config Backup) so the pre-reboot log survives the
reboot. The snapshot is staged in `/tmp` (tmpfs/RAM) — no writes to the USB
flash. If the NAS backup is not configured the dump is skipped and the reboot
still proceeds; `log_synced` reports the result. The same snapshot can be taken
on demand, without rebooting, via the `dump_log` MCP tool.

Intended for automated recovery — e.g. a Home Assistant `rest_command` that
reboots the router on a WAN outage. Use the **LAN IP**, not the DDNS host, so it
works while the uplink is down:

```
curl http://192.168.1.1:9584/YOUR_MCP_SECRET/reboot
```

⚠️ Reboots the router immediately — no confirmation step.

### Home Assistant examples

A REST sensor that tracks the WAN address, and a shell-style sensor that reads
the log:

```yaml
sensor:
  - platform: rest
    name: Router WAN
    resource: http://192.168.1.1:9584/YOUR_MCP_SECRET/tool/get_internet_status
    value_template: "{{ value_json.result[0].address }}"
    json_attributes_path: "$.result[0]"
    json_attributes: [uptime, defaultgw, priority]
    scan_interval: 300

rest_command:
  router_reboot:
    url: http://192.168.1.1:9584/YOUR_MCP_SECRET/reboot
```

Use the **LAN IP** rather than the DDNS host in automations that are meant to
survive an outage — the DDNS name goes through the uplink you are trying to
recover.

## Requirements

- Keenetic router with Entware support
- USB drive formatted as ext4
- Entware installed on the USB drive
- Python 3.x (standard library only — `requirements.txt` lists no external packages)

Tested arch **mipsel** (KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810). Should also work on **mips** arch (KN-2410, KN-2510, KN-2010, KN-2110, KN-3610).

## Installation

### Step 1 — Install Entware

Format a USB drive as ext4 and plug it into the router. In the router web interface go to Applications -> OPKG and make sure the drive is selected as the storage.

Download the installer for your router model and copy it to the `install` folder on the USB drive via SMB (\\192.168.1.1):

For KN-1010/1011, KN-1810, KN-1910, KN-2310, KN-3810:
https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz

For KN-2410, KN-2510, KN-2010, KN-2110, KN-3610:
https://bin.entware.net/mipssf-k3.4/installer/mips-installer.tar.gz

Entware installs automatically. Check the router system log for:
[5/5] Installation of the "Entware" package system is complete!

### Step 2 — SSH into the router

After Entware is installed, connect via SSH on port 222:

    ssh root@192.168.1.1 -p 222

Default password: keenetic. Change it immediately:

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

Fill in your credentials in `.env`:

    KEENETIC_HOST=http://192.168.1.1
    KEENETIC_USER=admin
    KEENETIC_PASS=your_router_password
    MCP_SECRET=some_random_secret_string
    MCP_PORT=9584

Optionally, tell the write tools what else to protect besides their own channel
(see the *Configuration changes* section and `.env.example`):

    MCP_PROTECTED_PORTS=8123,9583
    MCP_PROTECTED_PROXY_NAMES=ha-mcp,homeassistant

The plain-HTTP tool route is on by default and read-only. Both variables below
can be omitted entirely; set them only to narrow or widen that (see *Plain HTTP
endpoints*):

    MCP_HTTP_TOOLS=true
    MCP_HTTP_TOOL_ALLOWLIST=

### Step 5 — Set up autostart

    cp init.d/S99keenetic-mcp /opt/etc/init.d/
    chmod +x /opt/etc/init.d/S99keenetic-mcp
    /opt/etc/init.d/S99keenetic-mcp start

Verify it is running:

    /opt/etc/init.d/S99keenetic-mcp status
    curl http://localhost:9584/YOUR_MCP_SECRET

### Step 6 — Configure external HTTPS access

In the Keenetic web interface go to Network Rules -> Domain name -> Web application access and click Add:

- Name: keenetic-mcp
- Internet access: Open access
- Device: This Keenetic device
- Protocol: HTTP
- TCP Port: 9584

Your MCP server will be available at:
https://keenetic-mcp.YOUR_DDNS.keenetic.link/YOUR_MCP_SECRET

### Step 7 — Connect to Claude

In Claude.ai go to Settings -> Integrations -> Add custom connector and paste the URL from Step 6.

### Updating

    cd /opt/keenetic-mcp
    git pull --ff-only
    /opt/etc/init.d/S99keenetic-mcp restart
    /opt/etc/init.d/S99keenetic-mcp status

`.env` is in `.gitignore`, so a pull never touches your credentials. The
autostart script itself is not updated by a pull of the working copy — after a
release that changes it, copy it over again from `init.d/`.

After adding or removing tools, **start a new chat** — MCP clients cache
`tools/list` for the lifetime of a session, and toggling the connector inside an
existing chat is not enough. To check the tool list without a client:

    curl -s -X POST http://localhost:9584/YOUR_MCP_SECRET \
         -H 'Content-Type: application/json' \
         -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

## How Client Management Works

- `get_unregistered_clients` shows devices that connected to your network but were never named or registered
- `get_dhcp_leases` shows devices that received an IP from the DHCP pool with time until lease expires; `get_dhcp_static` shows fixed bindings, which never appear as leases
- `register_client` assigns a name and optional static IP to a device
- `block_client` denies network access to a device. If the device is not yet registered, it will be registered automatically as "Blocked Device" before blocking
- `unblock_client` restores access with permit rule
- Blocking does not disconnect the device from WiFi and does not stop it from getting a DHCP lease — it cuts off internet and LAN access at the firewall level
- Registration lives in the `known host` tree (`Core::KnownHosts`), not in `ip hotspot host`, and every mutation is followed by `system configuration save` — otherwise the change would not survive a reboot

## Troubleshooting

**The connector is dead and port 9584 does not answer.** First check whether the
server is even running — most often `/opt` itself is not mounted, which means
`server.py` was never started:

    /opt/etc/init.d/S99keenetic-mcp status
    grep ' /opt ' /proc/mounts

If `/opt` is not mounted, rebind the OPKG drive: web interface -> OPKG package
manager -> Storage -> select "not selected" -> save -> select the drive again ->
save. This remounts `/opt` and re-runs `rc.unslung`. A router reboot is not
needed. The same thing over RCI, from another host:

    POST /rci/  {"opkg":{"disk":{"no":true}}}
    POST /rci/  {"opkg":{"disk":{"disk":"USB:/"}}}

⚠️ **Never rebind over SSH on the router itself.** `dropbear` lives on `/opt` and
is started from `rc.unslung`; the first command kills your own session and the
second one never runs, leaving `/opt` unmounted. Do it from the web interface or
from another machine.

**`Address already in use` in the log, and an old version answers on the port.**
An orphaned instance is still holding the socket — `/opt` being rebound spawns a
second `server.py` and overwrites the pid-file, so the previous process survives
(python keeps its inode across the unmount). The init script handles this
itself: `stop` and `restart` also kill processes matched in `/proc` (by a python
`argv[0]` whose command line contains the server path — a shell that merely
mentions it is never matched), and `start` cleans up before spawning. Check
with:

    /opt/etc/init.d/S99keenetic-mcp status

`status` reports every live instance and warns if more than one is running. If
you are upgrading from a build whose init script predates this, copy the current
`init.d/S99keenetic-mcp` over by hand once — a `git pull` of the working copy
does not replace the one already installed under `/opt/etc/init.d/`.

**Nothing in the log at all.** Server output goes to `/tmp/keenetic-mcp.log`
(RAM, truncated at 256 KB, marked with `=== <date> start ===`), and python runs
with `-u` so a crashing process does not take its traceback with it. `/tmp` is
cleared on reboot by design — nothing is ever written to the USB flash.

## Notes

- All 42 tools tested on NDMS 5.1.1
- `get_wifi` uses `show interface` (`show wireless` endpoint removed in NDMS 5.x)
- `get_traffic` aggregates rx/tx from active clients and shows top 10 by usage
- `get_channel_analysis` uses site survey data to recommend least congested channel
- `get_log_by_device` resolves device name/IP to MAC for more accurate log matching
- `get_log` timestamps can lie right after a reboot, and the tool now says so. The router logs from the moment it powers on, but its clock is only correct once NTP answers — everything in between carries a time restored from flash, which can be days off. Since 2.6.0 `get_log` compares timestamps against the router's uptime and prefixes its output with `[!] N of these entries are stamped BEFORE this boot` when it finds impossible dates. Look for `Ntp::Client: time synchronized` in the log to see exactly where real time starts. A `since`/`until` window will happily match those bogus dates, because as far as the filter is concerned they are just older entries
- A log line with no parsable timestamp inherits the timestamp of the line above it rather than being dropped from a `since`/`until` window (before 2.6.0 such lines vanished silently)
- `get_schedule` merges the runtime view (`show/schedule` — next fire, seconds left) with the human-readable name from the config tree; `get_dns_proxy` parses the proxy-config for upstream resolvers and static records. There is no `show/clock` or `show/update` endpoint on NDMS 5.x — the current firmware string lives in `get_system_info`
- Mesh extender clients are fully visible in `get_clients` and `get_wifi_stations` — each device includes a `node` field (`controller` or `extender`) indicating which mesh node it is connected to
- `get_extender_log` authenticates on each extender node independently using the same credentials as the controller; extenders are discovered dynamically from the hotspot table — no hardcoded IPs
- Port forwarding, firewall rules, static DHCP bindings and KeenDNS mappings are not exposed as RCI `show` endpoints in NDMS 5.x, but they are all present in `running-config` — which is what `get_port_forwarding`, `get_firewall_rules`, `get_dhcp_static` and `get_keendns_mappings` parse
- Port forwarding targets are addressed by **MAC**, not by IP (`ip static tcp GigabitEthernet1 8123 aa:bb:cc:dd:ee:ff`)
- `disable` in an `ip static` rule is a **per-rule attribute**, not a global switch for the whole block. In `running-config` it is emitted as a separate `ip static disable` line that continues the *preceding* rule, which reads like a global directive and is not one — confirm with `rci_query path='ip/static' config_tree=true`, where the flag sits inside its own rule object
- Backup scheduler runs in a background thread — no cron or external tools needed
- PID file and server log live in `/tmp` (RAM) — no flash writes on startup

## Security Notes

- The endpoint is protected by a secret token in the URL path
- HTTPS is handled by Keenetic built-in SSL certificate
- `rci_query` is GET-only and cannot modify the router; `crypto`, `ppp`, `user` and `running-config` subtrees are refused outright
- `get_config` masks secrets by default; `include_secrets: true` is opt-in and will put passwords and keys into the chat transcript
- The write tools always protect the server's own port, upstream and proxy name; add anything else via `MCP_PROTECTED_*` in `.env`
- The plain-HTTP tool route serves read-only tools only. State-changing tools are refused with 403 unless named in `MCP_HTTP_TOOL_ALLOWLIST` — treat adding one as handing out a write key, because the URL secret ends up in config files, automation traces and proxy logs
- Anything reachable over MCP is reachable over the tool route with the same secret. If that is not what you want, run `MCP_HTTP_TOOLS=false`
- Never commit `.env` — it is in `.gitignore`
- Change the default SSH password after installation

## Changelog

- **2.6.0** — plain-HTTP access to the tools, for clients that cannot speak MCP. `GET /<MCP_SECRET>/tool/<name>?arg=value` runs any tool and answers JSON (`&raw=1` for the tool's own text); `GET /<MCP_SECRET>/tools` lists what is servable. **Read-only by default:** the 13 state-changing tools return 403 unless named in the new `MCP_HTTP_TOOL_ALLOWLIST`; `MCP_HTTP_TOOLS=false` disables the route entirely. Arguments are coerced from each tool's declared `inputSchema`, so a bad type is an explicit 400 rather than a silently wrong call. The policy lives in a new `http_tools.py` — the registry and tool modules are untouched. Also fixes two `get_log` papercuts: lines with no parsable timestamp are no longer dropped from a `since`/`until` window, and output is flagged when it contains entries stamped before the current boot (pre-NTP clock)
- **2.5.0** — modular refactor + configurable protection, 40 -> 42 tools. `server.py` is split into focused modules (`core`, `backup`, `helpers`, `tools_network`, `tools_system`, `tools_config`, `registry`, and a thin `server`) with identical behaviour. The write-tool protection list moves from hardcoded to `.env` (`MCP_PROTECTED_PORTS` / `MCP_PROTECTED_PROXY_NAMES` / `MCP_PROTECTED_UPSTREAMS`); the server's own port, `127.0.0.1:<port>` upstream and `keenetic-mcp` name are always protected automatically. Two new read tools: `get_schedule` (router schedules, incl. the firmware auto-update window) and `get_dns_proxy` (upstream resolvers with DoT SNI + static records)
- **2.4.0** — write tools, 34 -> 40: `set_port_forwarding` / `remove_port_forwarding`, `set_keendns_mapping` / `remove_keendns_mapping`, `set_dhcp_host` / `remove_dhcp_host`, all with `dry_run` defaulting to true, a coded protected list, `system configuration save` after every write and a before/after verification read. `rci_query` gained `config_tree` for read-only inspection of settings branches
- **2.3.0** — observability release, 25 -> 34 tools: `rci_query`, `get_config`, `get_port_forwarding`, `get_firewall_rules`, `get_dhcp_static`, `get_keendns_mappings`, `get_media`, `get_opkg_status`, `list_backups`; `get_system_info` reports the MCP server version, human-readable uptime and boot time; `get_log` accepts `since`/`until`
- **2.2.2** — client registration fixed: registration lives in `known host`, static IP in `ip dhcp host`, every mutation followed by `system configuration save`
- **2.2.0** — `dump_log` (log snapshot -> rsync to NAS, RAM staging); the `/reboot` endpoint snapshots the log before rebooting
- **2.1.0** — `GET /<MCP_SECRET>/reboot` HTTP endpoint for automated WAN-outage recovery
- **2.0.0** — refactor, per-tool functions, `node` field (controller/extender), `get_extender_log`, built-in backup scheduler

## License

MIT
