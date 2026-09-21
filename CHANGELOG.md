# Changelog

Tool counts are stated as `before -> after`. Dates are MSK.

## 2.8.0 — 2026-09-19

Honest save confirmation and intent verification across the write tools, two new read tools and a repository secret scan — 47 -> 49 tools.

### Added
- `get_config_state` returns a parsed `show/last-change`: when the config was last touched, by which agent, and the checksum. Its `fail-safe.unsaved` field is documented as unreliable.
- `diff_saved_config` diffs `running-config` against `startup-config` as CLI text from the `/ci/` endpoints, answering what is not saved right now.
- `watch_rules.example.json` gains `config_saved_not_mcp`, which fires on a config save made from the router's own CLI or through `ndmc`. It cannot tell a human editing over the web from this server's own writes.
- `tools/scan_repo_secrets.py` and a GitHub Actions workflow fail CI on a real MAC, a real private IP or a forbidden filename (`CLAUDE.md`, `.env`, `watch_rules.json`) in the checked-out tree. Files that are gitignored and untracked are skipped.

### Changed
- All twelve write tools compare the state after a write against what was requested, not against the state before it, and report the result as `matches_intent` — see the [write tool response contract](docs/tools.md#write-tool-response-contract).
- `_save_config()` polls `show/last-change`'s checksum up to `SAVE_VERIFY_TIMEOUT_MS` and answers `confirmed` or `pending`. `config_saved` is true only when the save request returned without an RCI error, and the new `save_verified` says whether the checksum on disk actually changed.
- Unused standard-library imports left over from the 2.5.0 split are removed, including `urllib.request` / `urllib.error` pairs that hide each other from `pyflakes` and `ruff`.

### Breaking
- `register_client`, `update_client`, `block_client` and `unblock_client` return the write-tool JSON object instead of a plain string. There is no compatibility shim for the old text output.

## 2.7.4 — 2026-09-07

Static DNS records become writable, and two read tools that were returning nothing are fixed — 45 -> 47 tools.

### Added
- `set_dns_host` and `remove_dns_host` manage `ip host` records served by the router's own DNS proxy. A name that already resolves elsewhere is refused; removal takes both name and address. Both are mutating tools and are refused over the plain-HTTP route unless allowlisted.

### Fixed
- `get_schedule` returned `actions: []` for a schedule that had actions, plus a phantom `{"id": "show", "name": null}` entry.
- `get_dns_proxy` answered `No DNS proxy status returned` on a router serving static records.

### Changed
- `get_schedule` treats an unreadable tree as an error instead of returning an empty list, and reports the weekday as a raw number under `dow_number` where only the config tree answers.
- `.env.example` adds `80,443` to `MCP_PROTECTED_PORTS`, and the documentation states that protection also forbids creating a rule for a listed port.

## 2.7.3 — 2026-08-20

Backs up the server's own unversioned files — `.env`, `watch_rules.json`, the Entware init script — 44 -> 45 tools.

### Added
- `backup_mcp_config` copies those files to the rsync target, runs synchronously and reports what landed, including an md5 round-trip check. It is a mutating tool, so the plain-HTTP route refuses it unless allowlisted.

### Changed
- The scheduled rsync run now also writes a `mcp-config/` mirror on every run and a dated `mcp-config-YYYY-MM-DD/` snapshot only when the content changed, detected from `manifest.json` on the receiver.
- Nothing on the receiver is rotated or deleted. The rsync private key is excluded from the set.
- Copies use `shutil.copy2`, so the mirror carries each file's original mtime and mode.

## 2.7.2 — 2026-08-03

Fixes the watcher's log polling, broken in 2.7.1.

- The watcher reads the log over the POST form `{"show": {"log": {}}}` again, keeping two clients on its single session: GET for RCI rules, POST for the log.
- A masked proxy URL in a rendered rule keeps host and port readable (`http://***:***@host:port`).

## 2.7.1 — 2026-08-03

⚠️ Broken release — its log source returns 404; use 2.7.2.

The watcher gets its own RCI session, and `test_watch_rule` stops printing secrets.

### Changed
- The watcher authenticates as its own client and guards its own cookie with a private lock. It no longer takes `core.rci_lock`, so a poll cannot delay an MCP call.
- `get_watch_status` gains an `rci_session` block.

### Fixed
- `test_watch_rule` masks credentials in the view it renders — bot tokens, proxy passwords, `Authorization` headers. The call itself still sends the real values.

## 2.7.0 — 2026-08-03

The event watcher: a background thread polls the router locally and makes an outbound HTTP call when a rule matches — 42 -> 44 tools.

### Added
- Two event sources: log lines by regex, and appear/disappear/change diffs on any RCI branch. See [docs/watcher.md](docs/watcher.md).
- Rules live in one JSON file on the router, re-read when its mtime changes, with a `defaults` block every rule inherits from. A rule carries the whole outbound call, so any HTTP receiver works without extra configuration.
- `get_watch_status` and `test_watch_rule`.

### Changed
- Log position is tracked by line content rather than by the log's own numbering, which restarts on reboot. When the position is lost, the current tail becomes the baseline and nothing is reported.
- `core.rci_lock` serialises the shared session cookie between the watcher thread, the backup thread and the HTTP server.

## 2.6.0 — 2026-08-02

Plain-HTTP access to the tools, for clients that cannot speak MCP — Home Assistant, curl, shell scripts.

### Added
- `GET /<MCP_SECRET>/tool/<name>?arg=value` runs any tool and answers JSON; `&raw=1` returns the tool's own text instead. `GET /<MCP_SECRET>/tools` lists what is servable. See [docs/http-api.md](docs/http-api.md).
- `MCP_HTTP_TOOL_ALLOWLIST` names state-changing tools allowed over the route; `MCP_HTTP_TOOLS=false` turns the route off entirely.

### Changed
- The route is read-only by default: state-changing tools answer 403 unless allowlisted.
- Arguments are coerced from each tool's declared `inputSchema`. A value that does not fit its declared type is a 400 naming the parameter.
- The access policy lives in a new `http_tools.py`; the registry and the tool modules are unchanged.

### Fixed
- `get_log` no longer drops lines with no parsable timestamp from a `since`/`until` window.
- `get_log` flags output that contains entries stamped before the current boot, which happens while the clock is still pre-NTP.

## 2.5.0 — 2026-07-27

Modular refactor and a configurable protection list — 40 -> 42 tools.

### Added
- `get_schedule` reads router schedules, including the firmware auto-update window.
- `get_dns_proxy` reads upstream resolvers with their DoT SNI, plus the static records the router serves.

### Changed
- `server.py` is split into `core`, `backup`, `helpers`, `tools_network`, `tools_system`, `tools_config`, `registry` and a thin `server`. See [docs/internals.md](docs/internals.md).
- The write-tool protection list moves from code to `.env`: `MCP_PROTECTED_PORTS`, `MCP_PROTECTED_PROXY_NAMES`, `MCP_PROTECTED_UPSTREAMS`. The server's own port, its `127.0.0.1:<port>` upstream and the `keenetic-mcp` proxy name stay protected automatically.

## 2.4.0 — 2026-07-25

Write tools — 34 -> 40 tools.

### Added
- `set_port_forwarding` / `remove_port_forwarding`, `set_keendns_mapping` / `remove_keendns_mapping` and `set_dhcp_host` / `remove_dhcp_host`, all with `dry_run` defaulting to true.
- `rci_query` gains `config_tree` for read-only inspection of settings branches.

### Changed
- Every real write is followed by `system configuration save` and a re-read of the affected branch, so the answer carries a before/after diff.
- A write touching any of six hardcoded KeenDNS proxy names (`keenetic-mcp`, `ha-mcp`, `vault-mcp`, `adb-mcp`, `homeassistant`, `ntfy`), six hardcoded ports (`9584`, `8123`, `9583`, `3100`, `3200`, `7612`) or the `127.0.0.1:9584` upstream is refused. The set is in code and cannot be configured until 2.5.0.

## 2.3.0 — 2026-07-25

Observability release — 25 -> 34 tools.

### Added
- `rci_query`, `get_config`, `get_port_forwarding`, `get_firewall_rules`, `get_dhcp_static`, `get_keendns_mappings`, `get_media`, `get_opkg_status` and `list_backups`.

### Changed
- `get_system_info` reports the MCP server version, human-readable uptime and boot time.
- `get_log` accepts `since` and `until`.

## 2.2.2 — 2026-07-02

Fixes client registration.

- Registration writes to the `known host` tree, and a static IP to `ip dhcp host`.
- Every mutation is followed by `system configuration save`, so the change survives a reboot.

## 2.2.1 — 2026-07-02

Fixes client registration and blocking, which reported success while the router rejected the write.

### Fixed
- `register_client` and `update_client` sent `"registered": true` in the `ip hotspot host` RCI write. That field is a read-only status flag returned by `show ip hotspot host`, not a writable leaf, so the router rejected the whole write. The field is removed; assigning a `name` is what registers a host in KeeneticOS.
- Both tools now parse the RCI response and return the router's error message instead of reporting success unconditionally.
- The auto-register fallback in `block_client`, which registers an unknown device as `Blocked Device`, sent the same field and failed the same silent way, so an unregistered device could not be blocked at all.

The visible symptom was `register_client` reporting success, the device never appearing in `get_clients`, and a later `block_client` failing with `host is unregistered` (code 19007441). No tool schemas and no part of the MCP API changed.

## 2.2.0 — 2026-06-30

Log snapshots to the NAS.

- `dump_log` snapshots the current router log and rsyncs it to the NAS backup path, staged in `/tmp`.
- The `/reboot` endpoint snapshots the log before rebooting, so the pre-reboot log survives.

## 2.1.0 — 2026-06-29

`GET /<MCP_SECRET>/reboot` reboots the router over HTTP, for automated WAN-outage recovery.

## 2.0.0 — 2026-05-17

Refactor to per-tool functions, with mesh support and a built-in backup scheduler.

- `get_extender_log` reads the system log directly from mesh extenders.
- `get_clients` and `get_wifi_stations` gain a `node` field naming the mesh node a device is connected to (`controller` or `extender`).
- A background thread backs up `running-config` on a cron schedule.

---

Releases exist on GitHub before 2.0.0, back to `v1.7.1`. They are not covered here.
