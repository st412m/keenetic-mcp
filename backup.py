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


def rsync_to_remote(local_file, filename, timeout=60):
    if subprocess.run(["which", "rsync"], capture_output=True).returncode != 0:
        syslog("ERROR: rsync not found, install it: opkg install rsync")
        return False
    remote = f"{core.BACKUP_RSYNC_USER}@{core.BACKUP_RSYNC_HOST}:{core.BACKUP_RSYNC_PATH}/{filename}"
    cmd = ["rsync", "-a"]
    if core.BACKUP_RSYNC_KEY:
        cmd += ["-e", f"ssh -i {core.BACKUP_RSYNC_KEY} -o StrictHostKeyChecking=no"]
    cmd += [local_file, remote]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode == 0:
        syslog(f"INFO: backup synced to {core.BACKUP_RSYNC_HOST}:{core.BACKUP_RSYNC_PATH}/{filename}")
        return True
    else:
        syslog(f"ERROR: rsync failed: {result.stderr.strip()}")
        return False


def do_backup():
    syslog("INFO: starting config backup")
    try:
        config_data = fetch_running_config()
    except Exception as e:
        syslog(f"ERROR: failed to fetch config: {e}")
        return False

    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"keenetic-config-{date_str}.json"
    use_rsync = bool(core.BACKUP_RSYNC_HOST and core.BACKUP_RSYNC_USER and core.BACKUP_RSYNC_PATH)

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
