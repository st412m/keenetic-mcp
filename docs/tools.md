# Tools

Full reference for all 49 tools: what each one returns, and the rules the write tools enforce.

## System monitoring

- `get_system_info` — firmware version, uptime, CPU load, memory usage. Also returns an `mcp` block with `mcp_server_version`, `uptime_human` and `boot_time`, the exact reboot timestamp
- `get_internet_status` — internet connection status and external IP address
- `get_interfaces` — all network interfaces status and configuration
- `get_traffic` — top clients by traffic with total rx/tx summary. Aggregates rx/tx from active clients and shows the top 10 by usage
- `get_vpn_status` — status of all VPN interfaces (WireGuard, IPsec, L2TP, PPTP) with peer details

## WiFi

- `get_wifi` — WiFi radio status: channel, bandwidth, bitrate, temperature, connected stations count. Uses `show interface`; the `show wireless` endpoint was removed in NDMS 5.x
- `get_wifi_stations` — currently connected WiFi stations with signal strength (RSSI), speed, traffic and mesh node (controller/extender)
- `get_site_survey` — scan nearby WiFi networks
- `get_channel_analysis` — analyze WiFi channel congestion and recommend the least busy channel for 2.4GHz and 5GHz. Uses site survey data

## Clients

- `get_clients` — all devices in the network with IP, MAC, signal, traffic and mesh node (controller/extender)
- `get_unregistered_clients` — active devices not yet registered in the router (unknown devices)
- `get_dhcp_leases` — devices with an active DHCP lease from the pool, including expiry time
- `get_dhcp_static` — static DHCP reservations (`ip dhcp host`). Complements `get_dhcp_leases`: a device with a fixed binding does not appear as a pool lease
- `register_client` — register a device by MAC, assign a name and optionally a static IP
- `update_client` — update name or static IP of a registered device
- `block_client` — block a device by MAC address (works for both registered and unregistered devices)
- `unblock_client` — unblock a previously blocked device by MAC address

### How client management works

- `get_unregistered_clients` shows devices that connected to the network but were never named or registered
- `get_dhcp_leases` shows devices that received an IP from the DHCP pool with time until the lease expires; `get_dhcp_static` shows fixed bindings, which never appear as leases
- `register_client` assigns a name and optional static IP to a device
- `block_client` denies network access to a device. A device that is not yet registered is registered automatically as "Blocked Device" before blocking
- `unblock_client` restores access with a permit rule
- Blocking does not disconnect the device from WiFi and does not stop it from getting a DHCP lease. It cuts off internet and LAN access at the firewall level
- Registration lives in the `known host` tree (`Core::KnownHosts`), not in `ip hotspot host`, and every mutation is followed by `system configuration save`, without which the change does not survive a reboot
- These four tools answer with the [write tool response contract](#write-tool-response-contract), not with a plain string

## Configuration and network rules (read-only)

- `get_config` — the router's `running-config` with an optional case-insensitive regex filter. Secrets (`md5`, `nthash`, `psk`, `password`, `private-key`, long base64 keys) are masked unless `include_secrets: true`
- `get_config_state` — parsed `show/last-change`: when the config was last touched (given both in MSK and UTC), which agent and user touched it, and the `checksum` — the only reliable signal that a save has actually landed on disk. The raw `fail-safe` block is included as-is, but its `unsaved` field must **not** be branched on: it can read `false` while a save is still in flight. Compare `checksum` across two calls instead
- `diff_saved_config` — diffs `running-config` against `startup-config`, both fetched as CLI text from the `/ci/` endpoints (outside `/rci/` entirely). Answers "what is not saved right now": it reports a difference for as long as `checksum` still shows the old value, which is the whole save window. It is not a way to catch a specific just-made write, since a single MCP round trip rarely beats that window. `only_in_running` / `only_in_startup` are line lists; both empty means fully saved. The config's own service header (`! $$$ Agent / Last change / Md5 / Username`) shows up in the diff whenever the two sides differ at all; that is normal, not an artifact. Secrets are masked on both sides before comparing. Not wired into the write tools — call it on demand
- `get_port_forwarding` — port forwarding / static NAT rules (`ip static`)
- `get_firewall_rules` — access-lists with their entries, `ip firewall` settings and interface access-groups
- `get_keendns_mappings` — KeenDNS / web application access mappings (`ip http proxy`) with their upstreams
- `get_dns_proxy` — DNS proxy status: upstream resolvers with their DoT SNI, the static A/AAAA records the router serves (parsed into domain/address/type rather than handed back as config lines), and the `ip host` config tree that `set_dns_host` / `remove_dns_host` write to, so a write can be verified without a second call. A missing `proxy-status` block is an error, not an empty list
- `get_schedule` — router schedules (the firmware auto-update window, and any others) with their name, weekday/time actions and seconds until the next fire. An unreadable tree produces an explicit **error**, never an empty list, so a failed read is never mistaken for "no window is configured". The weekday comes from the router's own resolved name (`Sat`); when only the config tree is available the day is reported as its raw number under `dow_number`, because the numbering is undocumented and both common conventions fit the samples. The tool merges the runtime view (`show/schedule` — next fire, seconds left, the resolved day name) with the human-readable name from the config tree
- `rci_query` — raw **read-only** query against the RCI tree: `GET /rci/show/<path>`, or `GET /rci/<path>` with `config_tree: true`. It cannot write: writing to RCI requires a POST body and this tool never sends one. Use the default tree for state (`ntp`, `components`, `ndns`, `interface/GigabitEthernet1`) and `config_tree` to inspect the exact write-shape of a settings branch (`ip/static`, `ip/http/proxy`, `ip/dhcp/host`). Schedules and the DNS proxy have their own named tools. Blacklisted subtrees: `running-config` (use `get_config`), `crypto`, `ppp`, `user`. Output is capped at 40 000 characters

Port forwarding, firewall rules, static DHCP bindings and KeenDNS mappings are not exposed as RCI `show` endpoints in NDMS 5.x. They are present in `running-config`, which is what `get_port_forwarding`, `get_firewall_rules`, `get_dhcp_static` and `get_keendns_mappings` parse.

## Configuration changes (write)

Every tool below takes `dry_run`, and **it defaults to `true`**: the tool returns the exact payload it would send and changes nothing. A real write is followed by `system configuration save`, without which a raw RCI write does not survive a reboot, and by a re-read of the affected branch, so the answer contains a before/after diff.

- `set_port_forwarding` — create or update an `ip static` rule. The target is addressed by **MAC**, not IP: pass `to_host` as a MAC, or as the IP of a registered host, which is resolved. An IP the router does not know is refused. Supports `to_port`, `end_port` (ranges), `comment` and `enable`, the last being the per-rule `disable` flag
- `remove_port_forwarding` — delete a rule by `index` (from `get_port_forwarding`) or by `port`. Ambiguous matches are refused rather than guessed
- `set_keendns_mapping` / `remove_keendns_mapping` — manage `ip http proxy` entries: name → upstream host:port, published on the ndns domain with ssl redirect
- `set_dhcp_host` / `remove_dhcp_host` — manage `ip dhcp host` reservations. An IP already reserved for a different MAC is refused. The device *name* lives in the known-host tree — use `register_client` / `update_client` for that
- `set_dns_host` / `remove_dns_host` — manage static DNS records (`ip host <domain> <address>`) served by the router's own DNS proxy. This is the LAN half of a split-horizon setup: public DNS sends a name to the WAN address, this record sends clients inside the network straight to the reverse proxy. `ip host` accepts several addresses for one name and round-robins them, so a name that already resolves elsewhere is refused rather than extended. Remove the old record first. Removal needs both name and address (the router's own `no ip host domain address` form), and the address is read from the config tree unless a name holds several. The documented ceiling of 64 records is checked before writing

Port forwarding targets are addressed by MAC, not by IP (`ip static tcp GigabitEthernet1 8123 aa:bb:cc:dd:ee:ff`).

`disable` in an `ip static` rule is a **per-rule attribute**, not a global switch for the whole block. In `running-config` it is emitted as a separate `ip static disable` line that continues the *preceding* rule, which reads like a global directive and is not one. Confirm with `rci_query path='ip/static' config_tree=true`, where the flag sits inside its own rule object.

Static DNS records live in two places that do not look alike: `ip host` in the config tree (what `set_dns_host` writes, `{domain, address}` objects) and `static_a` / `static_aaaa` lines inside the DNS proxy's generated config (what the proxy actually serves, including records the router creates for itself). `get_dns_proxy` returns both. The trailing number on a `static_a` line is the router's own flag and is not a reliable marker of "user-created"; it is passed through unread.

### Protected objects

The guards live in code, not in the tool descriptions. The server always protects itself, regardless of configuration and with no way to switch it off: its own port (`MCP_PORT`), the `127.0.0.1:<MCP_PORT>` upstream and the `keenetic-mcp` proxy name.

Everything else goes in `.env`: `MCP_PROTECTED_PORTS`, `MCP_PROTECTED_PROXY_NAMES`, `MCP_PROTECTED_UPSTREAMS`. See [configuration.md](configuration.md#protected-objects) for the full rules.

The refusal runs in both directions: a protected port cannot be removed **and** cannot be forwarded in the first place. Listing a port that is not forwarded today therefore also blocks publishing it later.

A protected object is refused outright, and the message names the port or the object. Changing it means doing so in the web interface, or taking it out of `.env` first.

## Write tool response contract

Twelve tools write to the router: the four pairs above (`set_port_forwarding` / `remove_port_forwarding`, `set_keendns_mapping` / `remove_keendns_mapping`, `set_dhcp_host` / `remove_dhcp_host`, `set_dns_host` / `remove_dns_host`) plus `register_client`, `update_client`, `block_client` and `unblock_client`. All twelve answer with the same fields:

| Field | Meaning |
|---|---|
| `errors` | Only what the router itself returned (`status` / `code` / `ident` / `message`). Never a synthetic entry |
| `intent_error` | `null`, or `{field, requested, actual, message}` when the router accepted the write with no error but the tree still shows the old value: a silent no-op that `errors` alone cannot see |
| `transport_error` | `null`, or the exception text when the connection dropped or timed out while reading the response. The write may still have reached the router; this field is separate from `errors` for that reason |
| `matches_intent` | Bool. Whether the value now on the router equals what was **requested**, not whether it differs from what was there **before**. This is what the tool's success is judged on |
| `changed` | Informational only, not a success signal. A record that changed but still does not match what was requested is `changed: true` and a failed write at the same time |
| `save_attempted` | Bool. Whether a save was even tried. It is not tried when nothing actually landed |
| `config_saved` | Bool. `true` only once the save request itself came back with no RCI error |
| `save_verified` | Bool. Whether the checksum on disk was actually seen to change |
| `save_detail` | The full object `_save_config` returns |

A write of a value that was already correct and a silently ignored write both read `changed: false`. `matches_intent` is what tells them apart.

`config_saved: true` means the save request came back with no RCI error. It does not mean the save has landed on disk. `save_verified` is that signal.

### `save_detail.status: "pending"` is a normal outcome, not an error

- The write tool polls the checksum every 250 ms for at most 7 s, then returns `pending` if the save has not landed. Both figures are constants in `helpers.py`, not settings
- `pending` means the write applied and the save is still settling. It is not a failure. Failures are `errors` (the router said no) and `intent_error` (the router said nothing and did nothing)
- The cap is finite: polling blocks this single-threaded server for its duration
- `fail-safe.unsaved` inside `show/last-change` is not a save indicator: it can read `false` while a save is still in flight. Compare `checksum` across two calls instead
- `show/last-change`'s `date` is stamped when a save starts; `checksum` changes when it finishes. `show/last-change` tracks `running-config` specifically

## Diagnostics

- `get_log` — system log with timestamps, optional line count, text filter and time window (`since` / `until`, accepting `HH:MM`, `HH:MM:SS` or `Jul 24 08:00`)
- `get_log_by_device` — system log filtered by device MAC address, IP address or name. Resolves a device name or IP to a MAC for more accurate log matching
- `run_ping` — ping a host directly from the router, returns latency and packet loss
- `get_watch_status` — watcher state: rules file, per-rule poll interval, cooldown, seconds to the next poll, match/sent/failed counters and the last delivery error
- `test_watch_rule` — render a watcher rule's outbound call with sample values (`dry_run` true by default) or actually send it, to prove the receiver is reachable from the router. Credentials in the rendered view are masked; the call itself uses the real values

### Log timestamps after a reboot

Until NTP answers, log entries carry a time restored from flash, which can be days off. `get_log` compares timestamps against the router's uptime and prefixes its output with `[!] N of these entries are stamped BEFORE this boot` when it finds impossible dates. `Ntp::Client: time synchronized` in the log marks where real time starts. A `since`/`until` window still matches those wrong dates.

A log line with no parsable timestamp inherits the timestamp of the line above it rather than being dropped from a `since`/`until` window.

## Mesh

- `get_mesh_nodes` — Mesh Wi-Fi nodes: controller and extenders with firmware, uptime and connection speed
- `get_extender_log` — system log directly from mesh extender(s); extenders are discovered automatically, optional filter by IP, line count and text. Each extender is authenticated independently with the same credentials as the controller; extenders come from the hotspot table, with no hardcoded IPs

Mesh extender clients are fully visible in `get_clients` and `get_wifi_stations`. Each device carries a `node` field (`controller` or `extender`) naming the mesh node it is connected to.

## Storage and Entware

- `get_media` — internal flash and USB drives: partition UUID, label, filesystem, state, free space and which subsystem uses the partition (e.g. `opkg`). Use it to check whether the Entware drive is healthy
- `get_opkg_status` — which drive OPKG is bound to, the initrc path, and whether `/opt` is **actually mounted**. `opt_mounted: false` means the server is running on an unmounted `/opt`; see [troubleshooting.md](troubleshooting.md)

## Backups and management

- `backup_config` — trigger a router config backup right now
- `backup_mcp_config` — back up **this server's own** unversioned files (`.env`, `watch_rules.json`, the Entware init script) to the NAS. Runs synchronously and reports what happened, including a round-trip md5 check. See [backup.md](backup.md#backing-up-the-servers-own-configuration)
- `list_backups` — list backup files already present on the NAS (`rsync --list-only`), which confirms the scheduled backups arrive
- `dump_log` — snapshot the current router log and rsync it to the NAS backup path (RAM staging, no flash writes)
- `reboot` — reboot the router

## Security

- `get_web_access` — web applications exposed to the internet via Keenetic DDNS, read from the generated nginx config. `get_keendns_mappings` shows the same thing from the other side (running-config); comparing the two catches stale entries

## Links

- [configuration.md](configuration.md) — every `.env` variable
- [http-api.md](http-api.md) — calling these tools over plain HTTP
- [watcher.md](watcher.md) — `get_watch_status` and `test_watch_rule` in context
- [backup.md](backup.md) — what `backup_config` and `backup_mcp_config` do
