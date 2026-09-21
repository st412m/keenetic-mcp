# Troubleshooting

Symptoms and the command that identifies each one.

## The connector is dead and port 9584 does not answer

Check whether the server is running at all. Most often `/opt` is not mounted, which means `server.py` was never started:

```
/opt/etc/init.d/S99keenetic-mcp status
grep ' /opt ' /proc/mounts
```

If `/opt` is not mounted, rebind the OPKG drive: web interface -> OPKG package manager -> Storage -> select "not selected" -> save -> select the drive again -> save. This remounts `/opt` and re-runs `rc.unslung`. A router reboot is not needed.

The same thing over RCI, from another host:

```
POST /rci/  {"opkg":{"disk":{"no":true}}}
POST /rci/  {"opkg":{"disk":{"disk":"USB:/"}}}
```

⚠️ **Never rebind over SSH on the router itself.** `dropbear` lives on `/opt` and is started from `rc.unslung`. The first command kills the session and the second one never runs, leaving `/opt` unmounted. Do it from the web interface or from another machine.

`get_opkg_status` reports the same thing as a tool: `opt_mounted` is the field to check.

## `Address already in use` in the log, and an old version answers on the port

An orphaned instance is still holding the socket. Rebinding `/opt` spawns a second `server.py` and overwrites the pid-file, so the previous process survives — python keeps its inode across the unmount.

The init script handles this: `stop` and `restart` also kill processes matched in `/proc` (by a python `argv[0]` whose command line contains the server path; a shell that merely mentions it is never matched), and `start` cleans up before spawning.

```
/opt/etc/init.d/S99keenetic-mcp status
```

`status` reports every live instance and warns if more than one is running.

If the installed init script predates this behaviour, copy the current `init.d/S99keenetic-mcp` over by hand once. A `git pull` of the working copy does not replace the one already installed under `/opt/etc/init.d/`.

## Nothing in the log at all

Server output goes to `/tmp/keenetic-mcp.log` (RAM, truncated at 256 KB, marked with `=== <date> start ===`). Python runs with `-u`, so a crashing process does not take its traceback with it. `/tmp` is cleared on reboot; nothing is written to the USB flash.

## A watcher rule never fires

Ask the watcher before suspecting the rule:

```
get_watch_status
```

It reports whether the thread is running, whether the rules file parsed, when each rule last matched, and the last delivery error. A rule that matches but cannot deliver shows up as `failed` with the error attached.

`test_watch_rule` with `dry_run=false` proves the receiver is reachable from the router, which is a different question from whether the event happened.

Check also that the firmware writes the event at all — see [watcher.md](watcher.md#which-events-the-firmware-writes). Authentication logging is off by default and needs `ip http log auth`.

## `backup_mcp_config` reports a file as skipped

It warns to syslog and backs up what it found rather than failing the run. Usually the init script lives somewhere else — point `BACKUP_MCP_INIT` at it.

`"error": "rsync target not configured"` means the `BACKUP_RSYNC_*` block is missing. There is no local fallback for this set.

## The tool list is out of date in the client

MCP clients cache `tools/list` for the lifetime of a session. After adding or removing tools, start a new chat; toggling the connector inside an existing chat is not enough.

To check the tool list without a client:

```
curl -s -X POST http://localhost:9584/YOUR_MCP_SECRET \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## A write tool refuses the operation

The object is in a protected list. The message names the port or the object. See [configuration.md](configuration.md#protected-objects).

## A write returns `save_detail.status: "pending"`

The write applied and the save is still settling. It is not a failure. See [tools.md](tools.md#write-tool-response-contract).
