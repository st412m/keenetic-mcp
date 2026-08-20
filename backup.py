import json
import hashlib
import urllib.request
import urllib.error
import http.server
import os
import shutil
import subprocess
import re
import threading
import time
from datetime import datetime

import core
from core import auth, rci


def syslog(message):
    try:
        subprocess.run(["logger", "-t", "keenetic-mcp", message],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def cron_matches(schedule, now):
    try:
        parts = schedule.strip().split()
        if len(parts) != 5:
            return False
        minute, hour, dom, month, dow = parts

        def match_field(field, value):
            if field == "*":
                return True
            for part in field.split(","):
                part = part.strip()
                if "-" in part:
                    lo, hi = part.split("-")
                    if int(lo) <= value <= int(hi):
                        return True
                elif int(part) == value:
                    return True
            return False

        return (
            match_field(minute, now.minute) and
            match_field(hour, now.hour) and
            match_field(dom, now.day) and
            match_field(month, now.month) and
            match_field(dow, now.weekday() + 1 if now.weekday() < 6 else 0)
        )
    except Exception:
        return False


def fetch_running_config():
    # Under core.rci_lock: this reads core.session_cookie right after auth(),
    # and since 2.7.0 the watcher thread can be re-authenticating in parallel.
    with core.rci_lock:
        auth()
        req = urllib.request.Request(
            f"{core.HOST}/rci/show/running-config",
            headers={"Cookie": core.session_cookie or ""},
            method="GET"
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = resp.read().decode()
    if not data or len(data) < 100:
        raise ValueError(f"Config response too short: {len(data)} bytes")
    return data


def _ssh_opts():
    """The -e argument shared by every rsync call here. Host key checking is
    off on purpose: the NAS is on the LAN, dropbear has no trusted-hosts file
    worth maintaining on a stick that has died twice, and an interactive
    'Didn't validate host key' prompt would hang a background thread."""
    if core.BACKUP_RSYNC_KEY:
        return ["-e", f"ssh -i {core.BACKUP_RSYNC_KEY} -o StrictHostKeyChecking=no"]
    return []


def _remote(rel=""):
    return f"{core.BACKUP_RSYNC_USER}@{core.BACKUP_RSYNC_HOST}:{core.BACKUP_RSYNC_PATH}/{rel}"


def _rsync_configured():
    return bool(core.BACKUP_RSYNC_HOST and core.BACKUP_RSYNC_USER and core.BACKUP_RSYNC_PATH)


def _have_rsync():
    return subprocess.run(["which", "rsync"], capture_output=True).returncode == 0


def rsync_to_remote(local_file, filename, timeout=60):
    if not _have_rsync():
        syslog("ERROR: rsync not found, install it: opkg install rsync")
        return False
    remote = _remote(filename)
    cmd = ["rsync", "-a"] + _ssh_opts() + [local_file, remote]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode == 0:
        syslog(f"INFO: backup synced to {core.BACKUP_RSYNC_HOST}:{core.BACKUP_RSYNC_PATH}/{filename}")
        return True
    else:
        syslog(f"ERROR: rsync failed: {result.stderr.strip()}")
        return False


# --------------------------------------------------------------------------
# The unversioned set (2.7.3)
#
# Three files that no `git pull` can bring back: .env and watch_rules.json are
# in .gitignore, and S99keenetic-mcp is hand-edited on the flash drive. Until
# now they were copied to the NAS by hand, so the survival of the watcher's
# configuration rested on someone remembering four commands. On 2026-08-19 the
# QNAP volume was rebuilt and both manual sets (06.08 and 09.08) died with it;
# they survived only because a copy happened to be sitting on a desktop.
#
# Storage scheme, decided 2026-08-20: a mirror directory that always holds the
# current state, plus a dated snapshot written ONLY when the content changed.
# The set changes about four times a year, so this leaves single-digit
# directories instead of 52, and history is kept where it matters - a corrupted
# file is itself a change, so it creates its own snapshot and the last good
# state stays in the previous one.
#
# Change detection reads manifest.json out of the mirror on the NAS. No state
# file on the stick (this project does not write to flash on a timer), and no
# parsing of --itemize-changes: after the 2026-08-19 restore all three files
# carry an mtime of 22:47 on the NAS because a `cp` ran without -p, so an
# mtime-based comparison would fire on identical content. The manifest also
# records the true mtime and mode, so the transport can no longer silently
# lose that the way plain `cp` did.
#
# Nothing here deletes anything on the NAS. Rotation would mean handing a
# delete primitive to the process that exists to preserve things; at a handful
# of directories per year it is not worth that.
# --------------------------------------------------------------------------

MCP_CONFIG_DIR = "mcp-config"
_STAGE = "/tmp/" + MCP_CONFIG_DIR                    # tmpfs (RAM), not flash
_PREV_MANIFEST = "/tmp/keenetic-mcp-prev-manifest.json"
_VERIFY_DIR = "/tmp/keenetic-mcp-verify"


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def mcp_config_sources():
    """Absolute paths of the files that go into the set. Missing ones are
    skipped with a warning rather than failing the whole run - the init script
    may legitimately be absent on an installation that starts the server some
    other way."""
    base = os.path.dirname(os.path.abspath(__file__))
    rules = core.WATCH_RULES
    if rules and not os.path.isabs(rules):
        rules = os.path.join(base, rules)
    candidates = [os.path.join(base, ".env"), rules, core.BACKUP_MCP_INIT]
    out = []
    for path in candidates:
        if not path:
            continue
        if os.path.isfile(path):
            out.append(path)
        else:
            syslog(f"WARNING: mcp-config backup: {path} not found, skipped")
    return out


def _stage_mcp_config(stage_dir):
    """Copy the set into a staging directory in RAM and write manifest.json
    next to it. Returns the manifest."""
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(stage_dir)

    files = {}
    for src in mcp_config_sources():
        name = os.path.basename(src)
        # copy2, never copy: on 2026-08-19 a plain `cp` reset the mtime of all
        # three files, and the backup stopped being able to say when a file was
        # last edited. Content survived, provenance did not.
        shutil.copy2(src, os.path.join(stage_dir, name))
        st = os.stat(src)
        files[name] = {
            "source": src,
            "md5": _md5(src),
            "size": st.st_size,
            "mode": oct(st.st_mode & 0o777)[2:].rjust(4, "0"),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtime_epoch": int(st.st_mtime),
        }

    manifest = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_epoch": int(time.time()),
        "mcp_version": core.VERSION,
        "router": core.HOST,
        "files": files,
    }
    with open(os.path.join(stage_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest


def _rsync_dir_to_remote(local_dir, timeout=90):
    """Send a whole directory: rsync -a /tmp/<name> host:PATH/ creates
    PATH/<name>/ on the receiver. Deliberately no --mkpath - that option only
    exists from rsync 3.2.3 and Entware's build cannot be assumed to have it.
    No --delete either: the file set is fixed, and an empty staging directory
    plus --delete would wipe the mirror."""
    if not _have_rsync():
        syslog("ERROR: rsync not found, install it: opkg install rsync")
        return False
    cmd = ["rsync", "-a"] + _ssh_opts() + [local_dir.rstrip("/"), _remote("")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        syslog(f"ERROR: rsync timed out sending {local_dir}")
        return False
    if r.returncode != 0:
        syslog(f"ERROR: rsync failed sending {local_dir}: {r.stderr.strip()}")
        return False
    return True


def _rsync_fetch(rel, local_path, timeout=30):
    """Pull one file back from the NAS. Returns False when it is not there
    (rsync exits 23), which is the normal first-run case."""
    if not _have_rsync():
        return False
    cmd = ["rsync", "-a"] + _ssh_opts() + [_remote(rel), local_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def _rsync_fetch_dir(rel, local_dir, timeout=60):
    if not _have_rsync():
        return False
    if os.path.isdir(local_dir):
        shutil.rmtree(local_dir)
    os.makedirs(local_dir)
    cmd = ["rsync", "-a"] + _ssh_opts() + [_remote(rel.rstrip("/") + "/"), local_dir + "/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def _remote_dir_exists(rel, timeout=30):
    if not _have_rsync():
        return False
    cmd = ["rsync", "--list-only"] + _ssh_opts() + [_remote(rel.rstrip("/") + "/")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def _sums(manifest):
    return {k: v.get("md5") for k, v in ((manifest or {}).get("files") or {}).items()}


def _cleanup(*paths):
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def backup_mcp_config(verify=True, timeout=90):
    """Back up the set that git cannot restore. Returns a result dict; never
    raises, because the weekly scheduler calls it in the same thread as the
    config backup."""
    result = {
        "ok": False,
        "first_run": None,
        "changed": None,
        "mirror": None,
        "snapshot": None,
        "verified": None,
        "files": [],
        "error": None,
    }

    if not core.BACKUP_MCP_CONFIG:
        result["error"] = "disabled (BACKUP_MCP_CONFIG=false in .env)"
        return result
    if not _rsync_configured():
        result["error"] = "rsync target not configured (BACKUP_RSYNC_* in .env)"
        return result
    if not _have_rsync():
        result["error"] = "rsync not found on the router: opkg install rsync"
        return result

    snap_stage = None
    try:
        manifest = _stage_mcp_config(_STAGE)
        if not manifest["files"]:
            result["error"] = "nothing to back up - none of the source files exist"
            return result
        result["files"] = [
            {"name": n, "size": m["size"], "mode": m["mode"], "mtime": m["mtime"]}
            for n, m in sorted(manifest["files"].items())
        ]

        # --- what does the NAS have now ---
        prev = None
        if _rsync_fetch(f"{MCP_CONFIG_DIR}/manifest.json", _PREV_MANIFEST):
            try:
                with open(_PREV_MANIFEST) as f:
                    prev = json.load(f)
            except Exception as e:
                syslog(f"WARNING: mcp-config: previous manifest unreadable ({e}), "
                       f"treating as changed")
        result["first_run"] = prev is None
        changed = _sums(prev) != _sums(manifest)
        result["changed"] = changed

        # --- always refresh the mirror ---
        if not _rsync_dir_to_remote(_STAGE, timeout=timeout):
            result["error"] = "rsync of the mirror failed (see syslog)"
            return result
        result["mirror"] = f"{core.BACKUP_RSYNC_PATH}/{MCP_CONFIG_DIR}/"

        # --- dated snapshot only when the content moved ---
        if changed:
            name = f"{MCP_CONFIG_DIR}-{datetime.now().strftime('%Y-%m-%d')}"
            if _remote_dir_exists(name):
                # Second change on the same day: do not merge into the morning's
                # snapshot, that would quietly overwrite it.
                name = f"{MCP_CONFIG_DIR}-{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
            snap_stage = "/tmp/" + name
            if os.path.isdir(snap_stage):
                shutil.rmtree(snap_stage)
            shutil.copytree(_STAGE, snap_stage)  # copytree uses copy2
            if _rsync_dir_to_remote(snap_stage, timeout=timeout):
                result["snapshot"] = f"{core.BACKUP_RSYNC_PATH}/{name}/"
            else:
                result["error"] = "mirror updated, but the dated snapshot failed to send"
                return result

        # --- prove it comes back, do not trust "it sent" ---
        if verify:
            ok = _rsync_fetch_dir(MCP_CONFIG_DIR, _VERIFY_DIR)
            if not ok:
                result["verified"] = False
                result["error"] = "could not read the mirror back for verification"
                return result
            mismatched = []
            for name, meta in manifest["files"].items():
                back = os.path.join(_VERIFY_DIR, name)
                if not os.path.isfile(back) or _md5(back) != meta["md5"]:
                    mismatched.append(name)
            result["verified"] = not mismatched
            if mismatched:
                result["error"] = "round-trip mismatch: " + ", ".join(sorted(mismatched))
                return result

        result["ok"] = True
        return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        _cleanup(_STAGE, snap_stage, _VERIFY_DIR, _PREV_MANIFEST)


def _backup_mcp_config_logged():
    """Wrapper for the scheduled path: swallows everything into syslog so a
    failure here can never take down the config backup around it."""
    try:
        r = backup_mcp_config()
    except Exception as e:
        syslog(f"ERROR: mcp-config backup crashed: {e}")
        return
    if r.get("ok"):
        syslog("INFO: mcp-config backup ok (changed=%s, verified=%s, snapshot=%s)" % (
            r.get("changed"), r.get("verified"), r.get("snapshot") or "-"))
    elif (r.get("error") or "").startswith("disabled"):
        pass
    else:
        syslog(f"ERROR: mcp-config backup failed: {r.get('error')}")


def do_backup():
    syslog("INFO: starting config backup")
    try:
        config_data = fetch_running_config()
    except Exception as e:
        syslog(f"ERROR: failed to fetch config: {e}")
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"keenetic-config-{date_str}.json"
    use_rsync = _rsync_configured()

    if use_rsync:
        tmp_path = f"/tmp/{filename}"
        try:
            with open(tmp_path, "w") as f:
                f.write(config_data)
        except Exception as e:
            syslog(f"ERROR: failed to write tmp file: {e}")
            return False
        success = rsync_to_remote(tmp_path, filename)
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        # 2.7.3: the same weekly run now also carries the unversioned set. Its
        # outcome deliberately does not change the return value - the Sunday
        # verification automation in Home Assistant checks for the config JSON
        # and must not start reporting a failed backup because of this.
        _backup_mcp_config_logged()
        return success
    else:
        os.makedirs(core.BACKUP_PATH, exist_ok=True)
        local_path = os.path.join(core.BACKUP_PATH, filename)
        try:
            with open(local_path, "w") as f:
                f.write(config_data)
        except Exception as e:
            syslog(f"ERROR: failed to write local backup: {e}")
            return False
        if core.BACKUP_KEEP > 0:
            try:
                files = sorted(
                    [f for f in os.listdir(core.BACKUP_PATH) if f.startswith("keenetic-config-")],
                    reverse=True
                )
                for old in files[core.BACKUP_KEEP:]:
                    os.remove(os.path.join(core.BACKUP_PATH, old))
            except Exception as e:
                syslog(f"WARNING: rotation failed: {e}")
        syslog(f"INFO: backup saved locally {local_path} ({len(config_data)} bytes)")
        return True


def backup_scheduler():
    syslog("INFO: backup scheduler started")
    time.sleep(60)
    last_triggered = None
    while True:
        try:
            now = datetime.now()
            trigger_key = (now.date(), now.hour, now.minute)
            if cron_matches(core.BACKUP_SCHEDULE, now) and last_triggered != trigger_key:
                last_triggered = trigger_key
                syslog(f"INFO: backup triggered by schedule '{core.BACKUP_SCHEDULE}'")
                threading.Thread(target=do_backup, daemon=True).start()
        except Exception as e:
            syslog(f"ERROR: scheduler error: {e}")
        time.sleep(60)
