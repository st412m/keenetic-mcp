# Internals

How the server is put together, and the traps specific to this codebase.

## Module layout

The server is a set of flat modules in the repository root. It is not a Python package: `init.d` runs `python server.py` directly, so the script's directory is on `sys.path` and plain imports resolve.

| Module | Contents |
|---|---|
| `core.py` | `.env` loading, router authentication, the RCI client, and all mutable state (session cookie, protected-object sets) |
| `backup.py` | The config-backup scheduler and rsync, plus the backup of the server's own unversioned files |
| `helpers.py` | running-config parsing, secret masking, log formatting, `_save_config` |
| `tools_network.py` | Read tools for clients, WiFi, interfaces, logs, VPN, DNS proxy, mesh |
| `tools_system.py` | System info, client management, ping, media/opkg, reboot, schedules |
| `tools_config.py` | running-config readers and the write tools, with the protection guards |
| `registry.py` | The tool table and dispatcher |
| `http_tools.py` | The plain-HTTP access policy and query-string argument coercion, kept out of the registry and the tool modules |
| `watcher.py` | The event watcher: rule loading, log and RCI polling, deduplication, templating and the outbound call |
| `server.py` | The HTTP / MCP transport and entry point |
| `tools/scan_repo_secrets.py` | Repository secret scan, run by the GitHub Actions workflow in `.github/workflows/` |

## Adding a tool

A tool is a function in the relevant `tools_*` module, registered in `registry.py`.

`watcher.py` is the exception: it registers its own tools instead of appearing in `registry.py`, so a change to the watcher touches neither the registry nor the tool modules.

A tool that changes state must also be added to `MUTATING_TOOLS` in `http_tools.py`. A tool absent from that set is served over the plain-HTTP route as read-only.

## The RCI envelope

`rci()` returns the whole RCI envelope. A POST for `{"show": {"x": {}}}` answers `{"show": {"x": ...}}`, and the payload sits one level down:

```python
result.get("show", {}).get("x", {})
```

`_rci_get()` does not wrap: `GET /rci/show/x` returns the payload bare.

A tool that reads the wrong shape does not raise. It finds nothing and reports an empty result, which is indistinguishable from a router that has none of the objects asked for.

## Lint trap: `urllib` submodule imports

`import urllib.request` and `import urllib.error` in the same file bind the same name, `urllib`. If either submodule is used anywhere in the file, both imports look used to `pyflakes` and to `ruff check --select F`, even when only one is referenced.

The two imports have to be checked separately:

```
grep -n 'urllib\.request\.' <file>
grep -n 'urllib\.error\.' <file>
```

Each import line is then compared against what its own grep found.
