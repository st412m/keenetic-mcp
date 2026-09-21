# Plain HTTP endpoints

The `GET` routes for clients that cannot speak MCP: Home Assistant's `rest` sensor, `rest_command` and `command_line`, cron jobs, plain `curl`.

Besides the MCP protocol (POST `/<MCP_SECRET>`), the server answers a few plain `GET` requests, authenticated by the same secret token in the URL path.

## Calling tools over HTTP

```
GET /<MCP_SECRET>/tool/<name>?arg=value
GET /<MCP_SECRET>/tools
```

`/tool/<name>` runs the same function the MCP client would and answers with

```json
{"ok": true, "tool": "get_system_info", "args": {}, "result": { ... }}
```

Most tools return a JSON document. It is parsed into `result` rather than nested as a string, so a template can index into it directly. Add `&raw=1` to get the tool's own text as `text/plain` instead, which suits `command_line` sensors and reading logs by eye.

`/tools` lists what is servable right now, with parameter names, declared types and a `mutating` flag.

## Read-only by default

The URL secret is a weak credential: it lands in `configuration.yaml`, in automation traces, in shell history and in any proxy log along the way. Tools that change state answer **403** on this route no matter what.

The mutating set is the eight write tools (`set_port_forwarding`, `remove_port_forwarding`, `set_keendns_mapping`, `remove_keendns_mapping`, `set_dhcp_host`, `remove_dhcp_host`, `set_dns_host`, `remove_dns_host`) plus `reboot`, `register_client`, `update_client`, `block_client`, `unblock_client`, `backup_config`, `backup_mcp_config`, `dump_log` and `test_watch_rule`. Everything else is served.

To lift the gate for a specific tool, name it explicitly:

```
MCP_HTTP_TOOL_ALLOWLIST=register_client
```

An allowlisted tool is callable by anyone holding the URL secret.

`MCP_HTTP_TOOLS=false` turns the whole route off. `/reboot` and the MCP protocol are unaffected by both variables.

Anything reachable over MCP is reachable over this route with the same secret.

## Argument coercion

Arguments are coerced using each tool's declared `inputSchema`, not by guessing from the text. A value that does not fit its declared type is a **400** with an explanation, never a silently wrong call:

```
$ curl 'http://192.168.1.1:9584/SECRET/tool/get_log?lines=zzz'
{"ok": false, "tool": "get_log", "error": "parameter 'lines' must be an integer, got 'zzz'"}
```

Unknown and missing parameters are 400 as well, and the error lists what the tool accepts. Booleans take `true/false`, `1/0`, `yes/no` or `on/off`. A repeated parameter (`?x=1&x=2`) takes the last value.

## Single-threaded server

⚠️ The server handles one request at a time, so a busy polling loop blocks MCP calls. Keep `scan_interval` at 60 s or more, and keep the slow tools out of anything that polls: `get_log` (allows the router 30 s to answer), `get_site_survey`, `get_channel_analysis`.

## Reboot endpoint

```
GET /<MCP_SECRET>/reboot
```

It runs the same `reboot` tool (`system reboot` over RCI) and returns `{"ok": true, "log_synced": <bool>, "result": "Reboot command sent"}`. Protected by the same secret token in the URL path. This endpoint is separate from the tool route.

Before rebooting, the endpoint snapshots the current router log and rsyncs it to the NAS backup path (see [backup.md](backup.md)) so the pre-reboot log survives the reboot. The snapshot is staged in `/tmp` (tmpfs/RAM); the USB flash is not written to. If the NAS backup is not configured the dump is skipped and the reboot still proceeds; `log_synced` reports the result. The same snapshot can be taken on demand, without rebooting, via the `dump_log` MCP tool.

Intended for automated recovery, for example a Home Assistant `rest_command` that reboots the router on a WAN outage. Use the **LAN IP**, not the DDNS host, so it works while the uplink is down:

```
curl http://192.168.1.1:9584/YOUR_MCP_SECRET/reboot
```

⚠️ Reboots the router immediately — no confirmation step.

## Home Assistant examples

A REST sensor that tracks the WAN address, and a `rest_command` that reboots:

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

Use the LAN IP rather than the DDNS host in automations meant to survive an outage: the DDNS name goes through the uplink being recovered.
