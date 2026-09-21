# Event watcher

The push half of the server: a background thread polls the router locally and makes an outbound HTTP call when a rule matches.

A rule carries a complete HTTP call — method, URL, headers, body — so any HTTP receiver works: a home-automation webhook, ntfy, the Telegram Bot API, a log collector. There is no per-receiver integration to configure.

## Rules

Rules live in one JSON file on the router: `watch_rules.json` next to `server.py` (override with `MCP_WATCH_RULES`). Copy `watch_rules.example.json` and edit. The file is re-read when its mtime changes, with no restart. There is no web interface.

With no rules file the watcher does not start, and says so in syslog.

```json
{
  "defaults": {
    "method": "POST",
    "url": "http://192.168.1.54:8123/api/webhook/keenetic_watch",
    "body": {"text": "${message}"},
    "cooldown": 60
  },
  "rules": [
    {
      "id": "vpn_login",
      "source": "log",
      "match": "Vpn::EventSender: \"([^\"]+)\": user \"([^\"]+)\" connected from \"([^\"]+)\"",
      "message": "VPN login: ${m2} from ${m3}"
    },
    {
      "id": "unknown_device",
      "source": "rci",
      "path": "show/ip/hotspot",
      "key": "mac",
      "where": {"active": true, "registered": false},
      "message": "Unknown device ${mac} (${ip}, '${name}')"
    }
  ]
}
```

A rule inherits everything it does not set from `defaults`.

## Common fields

`id`, `source` (`log` or `rci`), `enabled` (default true), `interval` seconds between polls (log: `MCP_WATCH_INTERVAL`, default 10; rci: 30), `cooldown` seconds (default 60), `max_events` per poll (default 5), `method`, `url`, `headers`, `body`, `timeout` (default 10), `proxy`, `verify_ssl`, `message`.

## `source: "log"`

`match` is a regex against the formatted log line; `exclude` is an optional counter-regex. Capture groups arrive as `${m1}`…`${m9}`, named groups under their own names, plus `${line}`, `${text}`, `${label}`, `${ident}`, `${log_time}`.

Cooldown for a log rule is **per rule**, not per distinct matched text. One login writes several lines and produces at most one call per cooldown window.

## `source: "rci"`

`path` is an RCI path (`show/ip/hotspot`), `key` is the field identifying an item (`mac`), `where` / `where_not` select which items count, `on` is any of `appear` (default), `disappear`, `change`, and `change` compares the fields in `track`.

Every field of the matched item is a placeholder, nested ones flattened with underscores (`${interface_name}`) and also exposed under their short name when nothing else claims it (`${ap}` for `mws_ap`).

Cooldown here is **per item**.

## Substitution

Substitution is `string.Template.safe_substitute`: an unknown `${name}` is left in the text rather than raising or silently emptying, so a typo shows up in the message instead of disappearing.

When `body` is an object it is sent as JSON and the escaping is done by the JSON encoder. A hand-built string body is not escaped.

## Secrets

`$NAME` in a `url`, a header, the `proxy` or the `body` is expanded from the environment when the file is loaded, so a bot token, a proxy password and a chat id can live in `.env` and stay out of the rules file.

Only **UPPERCASE** names are taken from the environment. Event fields are lowercase, so the two namespaces cannot collide.

`watch_rules.json` is gitignored, and it is included in the backup of the server's own configuration — see [backup.md](backup.md#backing-up-the-servers-own-configuration).

Rule matches are logged to syslog by rule id, never with the matched line, so a message body does not end up in the router log.

## Proxy

`proxy` is optional and per rule. Without it the call goes out directly: no proxy handler is constructed, and there is no global proxy setting.

It takes a full HTTP-proxy URL, credentials included (`http://user:pass@host:port`). An authenticated proxy works: `urllib` sends `Proxy-Authorization` on the `CONNECT`.

It has to be an HTTP proxy; SOCKS is not supported. Many SOCKS endpoints also speak HTTP on the same port.

Use it when the router itself cannot reach the receiver, for example a bot API blocked upstream.

Put it in `defaults` only when *every* receiver needs it. A rule posting to a machine on the LAN must not inherit a proxy that sends the request abroad and back.

The proxy does not decrypt anything: `CONNECT` forwards bytes and TLS stays end to end, so the certificate is still verified by the router's own python. If HTTPS fails on certificate verification, the router is missing a CA store (`opkg install ca-certificates`). `"verify_ssl": false` is a last resort.

## Which events the firmware writes

Check that the event is actually written before building a rule on it, and check whether the firmware has to be told to write it.

**Authentication logging is off by default on KeeneticOS 5.1.1** (observed then; not re-checked on 5.1.5). Turn it on once and it survives reboots:

```
ip http log auth
system configuration save
```

From an Entware shell the same two commands go through `ndmc -c '...'`, run under a normal ssh login — not under `exec sh`, which fails with `ndmc: system failed [0xcffd0060]`. Typing `ip http log auth` directly in an Entware shell hits busybox `ip`, not the router CLI.

With it on, the router logs both halves of a web-configurator login, with the source address:

```
Core::Scgi::Auth::Handler: opened session for user "admin" from "192.168.1.41".
Core::Scgi::Auth::Handler: authentication failed for user "admin" from "192.168.1.41".
```

Two authentication channels write under different prefixes:

- `Core::Scgi::Auth::Handler` covers the web configurator **and RCI**, so this server's own logins land there too, from the router's own LAN address
- `Core::Authenticator: user "admin" authenticated ... tag "cli"` is the telnet/SSH channel, written whether or not `log auth` is on

Brute-force bans need no switch: `Netfilter::Util::BfdManager: "Http": ban remote host <IP> for 15 minutes`, with a matching unban.

Logged without any switch:

- `Vpn::EventSender: ... connected from` for remote access, with user and source IP
- `Core::System::StartupConfig: saving (http/rci)` when settings are saved. This fires for this server's own write tools too, since the log does not say who asked; a change made through `ndmc` writes `saving (cli)` instead

`Core::Authenticator: user "admin" tagged with "http"` is the config being replayed at boot, so a rule matching `tag "http"` alone fires on every reboot.

Match the string the log actually contains, not the one Keenetic's documentation shows.

### `config_saved_not_mcp`

This rule is the mirror of `router_config_saved`: it matches the same `StartupConfig: saving (...)` line but excludes `saving (http/rci)`, so it fires on a save made from the router's own CLI or through `ndmc`.

It cannot catch a human editing over the web. That writes `http/rci` too, exactly like this server's write tools, and nothing distinguishes the two — not the log line, and not the `agent` field in `show/last-change`.

It is enabled by default, unlike most rules in the example file.

## How position in the log is tracked

- **Position is found by content, not by number.** The log is a RAM ring buffer and its numbering restarts after a reboot. The watcher remembers the hashes of the last few lines and looks for them in the next poll. When it cannot find them — reboot, or a burst larger than the buffer — it adopts the current tail as the new baseline and reports nothing
- **The first sight of an RCI rule is a baseline, not an event.** Items already present when a rule starts are not reported
- **State lives in `/tmp`** (`MCP_WATCH_STATE`), in RAM. A router reboot therefore resets the baseline: a device that was already connected before the reboot becomes part of the new normal. A restart of the server alone keeps its state
- **The watcher has its own RCI session.** It authenticates as its own client and guards its own cookie with a private lock, so it never touches `core.rci_lock` and a poll cannot delay an MCP call

One visible side effect of that separate session: the watcher authenticates over HTTP like any other RCI client, so with `ip http log auth` enabled its login appears as `Core::Scgi::Auth::Handler: opened session for user "admin" from "<the router's own LAN address>"` at startup and on session renewal — not under `Core::Authenticator`, which is the CLI/SSH channel. A rule matching web logins sees it; exclude the router's own address to keep it out.

`get_log` still takes around ten seconds and belongs nowhere near a polling loop.

## Checking it works

```bash
# is it running, and what has it seen
curl -s "http://192.168.1.1:9584/<MCP_SECRET>/tool/get_watch_status" | head -40

# render a rule's outbound call without sending anything
curl -s "http://192.168.1.1:9584/<MCP_SECRET>/tool/test_watch_rule?rule_id=vpn_login"
```

`get_watch_status` reports whether the thread is running, whether the rules file parsed, when each rule last matched, and the last delivery error.

`test_watch_rule` with `dry_run=false` actually sends. It is in the mutating set, so over HTTP it needs `MCP_HTTP_TOOL_ALLOWLIST`; over MCP it is available directly. Credentials in the rendered view are masked — bot tokens, proxy passwords, `Authorization` headers — by value against the environment and by pattern. Masking applies to the report, not to the call.

## Security

A watcher rule is an outbound HTTP call the router makes on its own. Treat `watch_rules.json` as credential material: it can carry bot tokens and internal URLs. Prefer `$NAME` placeholders resolved from `.env`.
