import json
import hashlib
import urllib.request
import urllib.error
import http.server
import os
import subprocess
import re
import threading
import time
from datetime import datetime

import core
from backup import backup_mcp_config, do_backup, rsync_to_remote, syslog
from core import VERSION, _rci_get, rci
from helpers import _rci_errors, _save_config
from tools_network import tool_get_log


def tool_get_system_info(args):
    result = rci({"show": {"version": {}, "system": {}}})
    extra = {"mcp_server_version": VERSION}
    uptime = result.get("show", {}).get("system", {}).get("uptime")
    try:
        secs = int(uptime)
        extra["uptime_human"] = "%dd %dh %dm" % (
            secs // 86400, secs % 86400 // 3600, secs % 3600 // 60)
        extra["boot_time"] = datetime.fromtimestamp(
            time.time() - secs).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    result["mcp"] = extra
    return json.dumps(result, ensure_ascii=False, indent=2)


def tool_run_ping(args):
    host = args.get("host", "").strip()
    if not host:
        return "Error: host required"
    count = min(int(args.get("count", 4)), 10)
    r = subprocess.run(
        ["ping", "-c", str(count), "-W", "2", host],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout if r.stdout else r.stderr


def tool_register_client(args):
    mac = args.get("mac", "").lower().strip()
    name_val = args.get("name", "").strip()
    ip_val = args.get("ip", "").strip()
    if not mac or not name_val:
        return "Error: mac and name required"
    # Registration in KeeneticOS lives in the KnownHosts tree:
    #   known host <name> <mac>          (Core::KnownHosts)
    # NOT in `ip hotspot host`, which only manages access/policy/schedule
    # for hosts that are already known ("name" there is a read-only echo).
    result = rci({"known": {"host": {"name": name_val, "mac": mac}}})
    errors = _rci_errors(result)
    if errors:
        return f"Error registering {mac}: " + json.dumps(errors, ensure_ascii=False)
    if ip_val:
        # Static DHCP binding: `ip dhcp host <mac> <ip>`
        result_ip = rci({"ip": {"dhcp": {"host": {"mac": mac, "ip": ip_val}}}})
        ip_errors = _rci_errors(result_ip)
        if ip_errors:
            _save_config()
            return (f"Device {mac} registered as '{name_val}', but static IP failed: "
                    + json.dumps(ip_errors, ensure_ascii=False))
    _save_config()
    return f"Device {mac} registered as '{name_val}'" + (f" with IP {ip_val}" if ip_val else "")


def tool_update_client(args):
    mac = args.get("mac", "").lower().strip()
    if not mac:
        return "Error: mac required"
    changed = {}
    if args.get("name"):
        name_val = args["name"].strip()
        result = rci({"known": {"host": {"name": name_val, "mac": mac}}})
        errors = _rci_errors(result)
        if errors:
            return f"Error updating name for {mac}: " + json.dumps(errors, ensure_ascii=False)
        changed["name"] = name_val
    if args.get("ip"):
        ip_val = args["ip"].strip()
        result = rci({"ip": {"dhcp": {"host": {"mac": mac, "ip": ip_val}}}})
        errors = _rci_errors(result)
        if errors:
            if changed:
                _save_config()
            return f"Error updating IP for {mac}: " + json.dumps(errors, ensure_ascii=False)
        changed["ip"] = ip_val
    if not changed:
        return "Error: nothing to update (provide name and/or ip)"
    _save_config()
    return f"Device {mac} updated: " + json.dumps(changed, ensure_ascii=False)


def tool_block_client(args):
    mac = args.get("mac", "").lower().strip()
    if not mac:
        return "Error: mac address required"
    result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "deny"}}}})
    statuses = result.get("ip", {}).get("hotspot", {}).get("host", {}).get("status", [])
    if any(s.get("code") == "19007441" for s in statuses):
        # Unregistered host: register it first via KnownHosts, then deny.
        reg = rci({"known": {"host": {"name": "Blocked Device", "mac": mac}}})
        reg_errors = _rci_errors(reg)
        if reg_errors:
            return "Error auto-registering before block: " + json.dumps(reg_errors, ensure_ascii=False)
        result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "deny"}}}})
    if not _rci_errors(result):
        _save_config()
    return json.dumps(result, ensure_ascii=False, indent=2)


def tool_unblock_client(args):
    mac = args.get("mac", "").lower().strip()
    if not mac:
        return "Error: mac address required"
    result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "permit"}}}})
    if not _rci_errors(result):
        _save_config()
    return json.dumps(result, ensure_ascii=False, indent=2)


def dump_log_to_nas(timeout=12):
    """Snapshot the router log to /tmp (tmpfs/RAM) and rsync it to NAS.
    Used before a reboot so the pre-reboot log survives. No flash writes."""
    if not (core.BACKUP_RSYNC_HOST and core.BACKUP_RSYNC_USER and core.BACKUP_RSYNC_PATH):
        syslog("WARNING: log dump skipped, NAS rsync not configured")
        return False
    try:
        log_text = tool_get_log({"lines": 2000})
    except Exception as e:
        syslog(f"ERROR: log dump failed to fetch log: {e}")
        return False
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"keenetic-log-{ts}.txt"
    tmp_path = f"/tmp/{filename}"  # /tmp = tmpfs (RAM), not flash
    try:
        with open(tmp_path, "w") as f:
            f.write(log_text)
    except Exception as e:
        syslog(f"ERROR: log dump failed to write tmp: {e}")
        return False
    try:
        ok = rsync_to_remote(tmp_path, filename, timeout=timeout)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    if ok:
        syslog(f"INFO: log snapshot synced to NAS: {filename}")
    return ok


def tool_dump_log(args):
    ok = dump_log_to_nas()
    if ok:
        return "Log snapshot synced to NAS"
    return "Log dump failed or NAS not configured (see syslog)"


def tool_reboot(args):
    rci({"system": {"reboot": {}}})
    return "Reboot command sent"


def tool_backup_config(args):
    if not core.BACKUP_ENABLED:
        return "Backup is disabled. Set BACKUP_ENABLED=true in .env to enable."
    threading.Thread(target=do_backup, daemon=True).start()
    dest = f"{core.BACKUP_RSYNC_USER}@{core.BACKUP_RSYNC_HOST}:{core.BACKUP_RSYNC_PATH}" if core.BACKUP_RSYNC_HOST else core.BACKUP_PATH
    return f"Backup started. Config will be saved to: {dest}"


def tool_backup_mcp_config(args):
    """Unlike backup_config this runs synchronously and returns the outcome:
    the whole set is ~7 KB over the LAN, and a backup tool that answers
    'started' is exactly how you end up believing in a backup that is not
    happening. verify=true also reads the mirror back off the NAS and compares
    md5 against what was sent."""
    verify = args.get("verify", True)
    if isinstance(verify, str):
        verify = verify.strip().lower() not in ("0", "false", "no", "off")
    result = backup_mcp_config(verify=bool(verify))
    return json.dumps(result, ensure_ascii=False, indent=2)


def tool_get_media(args):
    try:
        data = json.loads(_rci_get("show/media"))
    except Exception as e:
        return "Error reading show/media: %s" % e

    def _mb(v):
        try:
            return round(int(v) / 1048576.0, 1)
        except (TypeError, ValueError):
            return None

    out = []
    for name, m in (data or {}).items():
        if not isinstance(m, dict):
            continue
        for pid, p in (m.get("partition") or {}).items():
            out.append({
                "media": name,
                "bus": m.get("bus"),
                "product": str(m.get("product", "")).strip(),
                "media_state": m.get("state"),
                "removable": m.get("removable"),
                "partition": pid,
                "uuid": p.get("uuid"),
                "label": p.get("label"),
                "fstype": p.get("fstype"),
                "state": p.get("state"),
                "total_mb": _mb(p.get("total")),
                "free_mb": _mb(p.get("free")),
                "used_by": p.get("used-by") or [],
            })
    return json.dumps(out, ensure_ascii=False, indent=2)


def tool_get_opkg_status(args):
    try:
        info = json.loads(_rci_get("opkg"))
    except Exception as e:
        return "Error reading /rci/opkg: %s" % e
    mounted = False
    try:
        with open("/proc/mounts") as f:
            mounted = any(" /opt " in line for line in f)
    except Exception:
        pass
    result = {"opkg": info, "opt_mounted": mounted}
    if not mounted:
        result["hint"] = ("/opt is NOT mounted - Entware is down. Rebind the drive "
                          "from Home Assistant (shell_command.keenetic_opkg_rebind) "
                          "or via the web UI. Do NOT do it over SSH: dropbear lives "
                          "on /opt and would kill its own session.")
    return json.dumps(result, ensure_ascii=False, indent=2)


def tool_list_backups(args):
    if not (core.BACKUP_RSYNC_HOST and core.BACKUP_RSYNC_USER and core.BACKUP_RSYNC_PATH):
        return "rsync backup target is not configured (BACKUP_RSYNC_* in .env)"
    if subprocess.run(["which", "rsync"], capture_output=True).returncode != 0:
        return "rsync not found on the router: opkg install rsync"
    cmd = ["rsync", "--list-only"]
    if core.BACKUP_RSYNC_KEY:
        cmd += ["-e", "ssh -i %s -o StrictHostKeyChecking=no" % core.BACKUP_RSYNC_KEY]
    cmd += ["%s@%s:%s/" % (core.BACKUP_RSYNC_USER, core.BACKUP_RSYNC_HOST, core.BACKUP_RSYNC_PATH)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "Timeout listing %s:%s" % (core.BACKUP_RSYNC_HOST, core.BACKUP_RSYNC_PATH)
    return r.stdout.strip() or r.stderr.strip() or "(empty listing)"


def _schedule_actions(runtime, config):
    """Actions of one schedule, runtime view preferred.

    The runtime view is the readable one: the router itself resolves the day
    number to a name ("Sat") and min/hour to "02:00". The config tree carries
    the same actions as raw numbers and is only a fallback - and there the day
    is left AS A NUMBER on purpose. Keenetic does not document whether `dow`
    counts from Sunday=0 or Monday=1, and the one live sample available here
    ("6" for Saturday) fits both conventions. Naming the day from the number
    would be a guess, and this tool exists to tell an automation when it may
    reboot the router: a wrong weekday is worse than a bare number.
    """
    acts = [a for a in (runtime.get("action") or []) if isinstance(a, dict)]
    if acts:
        return [{
            "type": a.get("type"),
            "dow": a.get("dow"),
            "time": a.get("time"),
            "next": a.get("next", False),
            "seconds_left": a.get("left"),
            "source": "runtime",
        } for a in acts]
    out = []
    for a in (config.get("action") or []):
        if not isinstance(a, dict):
            continue
        try:
            hhmm = "%02d:%02d" % (int(a.get("hour", 0)), int(a.get("min", 0)))
        except (TypeError, ValueError):
            hhmm = None
        out.append({
            "type": a.get("action"),
            "dow": None,
            "dow_number": a.get("dow"),
            "time": hhmm,
            "next": None,
            "seconds_left": None,
            "source": "config_tree",
            "note": ("runtime view unavailable for this schedule; the weekday is "
                     "left as the raw number because the numbering is undocumented"),
        })
    return out


def tool_get_schedule(args):
    """Router schedules, merged from the runtime view and the config tree.

    2.7.4 - until now this answered `actions: []` for a schedule that plainly
    had actions, next to a phantom entry `{"id": "show", "name": null}`. Both
    were one bug: `rci()` returns the whole RCI envelope, so the schedules sit
    under result["show"]["schedule"], and the old code treated the envelope
    itself as the schedule map. That is where the literal key "show" came from,
    and why every action list was empty. The phantom was a symptom, not a
    service node inside the schedule tree - with the envelope unwrapped there
    is nothing left to filter. `get_system_info` has always had this shape
    right: result.get("show", {}).get("system", {}).

    A tree that cannot be read is now an ERROR, never an empty list. This tool
    answers "when may I reboot the router"; an automation that cannot tell
    "no window is configured" from "I failed to read it" will call every hour
    safe.
    """
    problems = []
    try:
        envelope = rci({"show": {"schedule": {}}})
        if not isinstance(envelope, dict):
            raise ValueError("unexpected response type %s" % type(envelope).__name__)
        show = envelope.get("show", {}).get("schedule", {}) or {}
        if not isinstance(show, dict):
            raise ValueError("show/schedule is not an object")
    except Exception as e:
        show = {}
        problems.append("runtime view (show/schedule): %s" % e)
    try:
        tree = json.loads(_rci_get("schedule")) or {}
        if not isinstance(tree, dict):
            raise ValueError("config tree is not an object")
    except Exception as e:
        tree = {}
        problems.append("config tree (/rci/schedule): %s" % e)

    if problems:
        return ("Error: the router's schedules could not be read, and an empty "
                "answer here would be indistinguishable from 'no schedules are "
                "configured'. Do not treat this as an empty schedule list. "
                "Details: " + "; ".join(problems))

    out = []
    for sid in sorted(set(show) | set(tree)):
        s = show.get(sid) if isinstance(show.get(sid), dict) else {}
        t = tree.get(sid) if isinstance(tree.get(sid), dict) else {}
        out.append({
            "id": sid,
            "name": t.get("description"),
            "actions": _schedule_actions(s, t),
        })

    if not out:
        return ("No schedules are configured. Both the runtime view and the config "
                "tree were read successfully and both are empty.")

    empty = [e["id"] for e in out if not e["actions"]]
    if len(empty) == len(out):
        return ("Error: %d schedule(s) found (%s) but not a single action could be "
                "read from either tree. A KeeneticOS schedule always carries "
                "actions, so this is a read failure rather than an empty schedule. "
                "Cross-check with rci_query path='schedule' config_tree=true."
                % (len(empty), ", ".join(empty)))

    result = {"count": len(out), "schedules": out}
    if empty:
        result["warning"] = ("no actions could be read for: %s - treat those as "
                             "unknown, not as empty" % ", ".join(empty))
    return json.dumps(result, ensure_ascii=False, indent=2)
