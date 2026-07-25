#!/usr/bin/env python3

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

HOST = "http://192.168.1.1"
USER = "admin"
PASS = "password"
SECRET = "changeme"
PORT = 9584
VERSION = "2.4.0"

# Backup config
BACKUP_ENABLED = False
BACKUP_SCHEDULE = "0 11 * * 0"
BACKUP_PATH = "/tmp/keenetic-backup"
BACKUP_KEEP = 0
BACKUP_RSYNC_HOST = ""
BACKUP_RSYNC_USER = ""
BACKUP_RSYNC_KEY = ""
BACKUP_RSYNC_PATH = ""

session_cookie = None


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env():
    global HOST, USER, PASS, SECRET, PORT
    global BACKUP_ENABLED, BACKUP_SCHEDULE, BACKUP_PATH, BACKUP_KEEP
    global BACKUP_RSYNC_HOST, BACKUP_RSYNC_USER, BACKUP_RSYNC_KEY, BACKUP_RSYNC_PATH

    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

    HOST = os.environ.get("KEENETIC_HOST", HOST)
    USER = os.environ.get("KEENETIC_USER", USER)
    PASS = os.environ.get("KEENETIC_PASS", PASS)
    SECRET = os.environ.get("MCP_SECRET", SECRET)
    PORT = int(os.environ.get("MCP_PORT", str(PORT)))

    BACKUP_ENABLED = os.environ.get("BACKUP_ENABLED", "false").lower() == "true"
    BACKUP_SCHEDULE = os.environ.get("BACKUP_SCHEDULE", BACKUP_SCHEDULE)
    BACKUP_PATH = os.environ.get("BACKUP_PATH", BACKUP_PATH)
    BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", str(BACKUP_KEEP)))
    BACKUP_RSYNC_HOST = os.environ.get("BACKUP_RSYNC_HOST", "")
    BACKUP_RSYNC_USER = os.environ.get("BACKUP_RSYNC_USER", "")
    BACKUP_RSYNC_KEY = os.environ.get("BACKUP_RSYNC_KEY", "")
    BACKUP_RSYNC_PATH = os.environ.get("BACKUP_RSYNC_PATH", "")


# ---------------------------------------------------------------------------
# Auth & RCI — controller
# ---------------------------------------------------------------------------

def auth():
    global session_cookie
    req = urllib.request.Request(f"{HOST}/auth")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            realm = e.headers.get("X-NDM-Realm", "")
            challenge = e.headers.get("X-NDM-Challenge", "")
            cookie_header = e.headers.get("Set-Cookie", "")
            session_cookie = cookie_header.split(";")[0] if cookie_header else ""
            md5_pass = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
            sha256_hash = hashlib.sha256(f"{challenge}{md5_pass}".encode()).hexdigest()
            payload = json.dumps({"login": USER, "password": sha256_hash}).encode()
            req2 = urllib.request.Request(
                f"{HOST}/auth",
                data=payload,
                headers={"Content-Type": "application/json", "Cookie": session_cookie},
                method="POST"
            )
            resp = urllib.request.urlopen(req2)
            cookie2 = resp.headers.get("Set-Cookie", "")
            if cookie2:
                session_cookie = cookie2.split(";")[0]
            return True
    return False


def rci(commands, timeout=10):
    global session_cookie
    if not session_cookie:
        auth()
    payload = json.dumps(commands).encode()

    def do_request():
        req = urllib.request.Request(
            f"{HOST}/rci/",
            data=payload,
            headers={"Content-Type": "application/json", "Cookie": session_cookie or ""},
            method="POST"
        )
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        resp = do_request()
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            session_cookie = None
            auth()
            resp = do_request()
            return json.loads(resp.read())
        raise


# ---------------------------------------------------------------------------
# Auth & RCI — mesh extender nodes
# ---------------------------------------------------------------------------

def _auth_node(ip):
    """Authenticate on a mesh extender node and return session cookie."""
    req = urllib.request.Request(f"http://{ip}/auth")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            realm = e.headers.get("X-NDM-Realm", "")
            challenge = e.headers.get("X-NDM-Challenge", "")
            cookie = e.headers.get("Set-Cookie", "").split(";")[0]
            md5_pass = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
            sha256_hash = hashlib.sha256(f"{challenge}{md5_pass}".encode()).hexdigest()
            payload = json.dumps({"login": USER, "password": sha256_hash}).encode()
            req2 = urllib.request.Request(
                f"http://{ip}/auth",
                data=payload,
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST"
            )
            resp = urllib.request.urlopen(req2)
            cookie2 = resp.headers.get("Set-Cookie", "").split(";")[0]
            return cookie2 or cookie
    return None


def _rci_node(ip, commands, timeout=15):
    """Execute RCI command on a specific mesh extender node."""
    cookie = _auth_node(ip)
    if not cookie:
        raise RuntimeError(f"Auth failed on {ip}")
    payload = json.dumps(commands).encode()
    req = urllib.request.Request(
        f"http://{ip}/rci/",
        data=payload,
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

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
    global session_cookie
    auth()
    req = urllib.request.Request(
        f"{HOST}/rci/show/running-config",
        headers={"Cookie": session_cookie or ""},
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
    remote = f"{BACKUP_RSYNC_USER}@{BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}/{filename}"
    cmd = ["rsync", "-a"]
    if BACKUP_RSYNC_KEY:
        cmd += ["-e", f"ssh -i {BACKUP_RSYNC_KEY} -o StrictHostKeyChecking=no"]
    cmd += [local_file, remote]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode == 0:
        syslog(f"INFO: backup synced to {BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}/{filename}")
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
    use_rsync = bool(BACKUP_RSYNC_HOST and BACKUP_RSYNC_USER and BACKUP_RSYNC_PATH)

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
        os.makedirs(BACKUP_PATH, exist_ok=True)
        local_path = os.path.join(BACKUP_PATH, filename)
        try:
            with open(local_path, "w") as f:
                f.write(config_data)
        except Exception as e:
            syslog(f"ERROR: failed to write local backup: {e}")
            return False
        if BACKUP_KEEP > 0:
            try:
                files = sorted(
                    [f for f in os.listdir(BACKUP_PATH) if f.startswith("keenetic-config-")],
                    reverse=True
                )
                for old in files[BACKUP_KEEP:]:
                    os.remove(os.path.join(BACKUP_PATH, old))
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
            if cron_matches(BACKUP_SCHEDULE, now) and last_triggered != trigger_key:
                last_triggered = trigger_key
                syslog(f"INFO: backup triggered by schedule '{BACKUP_SCHEDULE}'")
                threading.Thread(target=do_backup, daemon=True).start()
        except Exception as e:
            syslog(f"ERROR: scheduler error: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ap(host):
    """Extract AP interface from host entry (direct or via MWS backhaul)."""
    if host.get("mws-backhaul"):
        return host.get("mws", {}).get("ap", "")
    return host.get("ap", "")


def _get_node(host):
    """Return 'extender' if client is on extender (mws-backhaul), else 'controller'."""
    return "extender" if host.get("mws-backhaul") else "controller"


def _format_log_line(entry):
    """Format a log entry dict into a readable string with timestamp."""
    if not isinstance(entry, dict):
        return str(entry)
    msg = entry.get("message", {})
    time_str = entry.get("timestamp", "")
    ident = entry.get("ident", "")
    if isinstance(msg, dict):
        label = msg.get("label", "?")
        text = msg.get("message", "")
    else:
        label = "?"
        text = str(msg)
    parts = [f"[{label}]"]
    if time_str:
        parts.append(time_str)
    if ident:
        parts.append(ident + ":")
    parts.append(text)
    return " ".join(parts)


def _parse_log_dict(result):
    """Extract log dict from RCI response."""
    log_dict = result.get("show", {}).get("log", {}).get("log", {})
    if not log_dict:
        log_dict = result.get("show", {}).get("log", {})
    return log_dict


def _get_hotspot_hosts():
    """Fetch all hotspot hosts from RCI."""
    result = rci({"show": {"ip": {"hotspot": {}}}})
    return result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])


def _get_extender_hosts():
    """Return list of active extender nodes from hotspot."""
    hosts = _get_hotspot_hosts()
    return [
        {
            "ip": h.get("ip"),
            "mac": h.get("mac"),
            "name": h.get("name", h.get("hostname", h.get("mac"))),
        }
        for h in hosts
        if h.get("system-mode") == "extender" and h.get("active") and h.get("ip")
    ]


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

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


def tool_get_clients(args):
    hosts = _get_hotspot_hosts()
    output = []
    for h in hosts:
        entry = {
            "name": h.get("name", h.get("hostname", "")),
            "mac": h.get("mac"),
            "ip": h.get("ip"),
            "hostname": h.get("hostname", ""),
            "active": h.get("active", False),
            "node": _get_node(h),
            "ap": _get_ap(h),
            "link": h.get("link"),
            "uptime": h.get("uptime"),
            "rxbytes": h.get("rxbytes", 0),
            "txbytes": h.get("txbytes", 0),
            "rssi": h.get("rssi") or h.get("mws", {}).get("rssi"),
            "registered": h.get("registered", False),
            "access": h.get("access"),
        }
        if h.get("port"):
            entry["port"] = h.get("port")
            entry["speed"] = h.get("speed")
        output.append(entry)
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_unregistered_clients(args):
    hosts = _get_hotspot_hosts()
    unreg = [h for h in hosts if h.get("active") and not h.get("registered")]
    if not unreg:
        return "No unregistered active devices found"
    output = []
    for h in unreg:
        output.append({
            "mac": h.get("mac"),
            "ip": h.get("ip"),
            "hostname": h.get("hostname", ""),
            "node": _get_node(h),
            "link": h.get("link"),
            "first_seen": h.get("first-seen"),
            "last_seen": h.get("last-seen"),
        })
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_dhcp_leases(args):
    hosts = _get_hotspot_hosts()
    leases = []
    for h in hosts:
        expires = h.get("dhcp", {}).get("expires", 0)
        if expires and expires > 0:
            leases.append({
                "name": h.get("name", h.get("hostname", "")),
                "mac": h.get("mac"),
                "ip": h.get("ip"),
                "expires_sec": expires,
                "active": h.get("active", False),
            })
    leases.sort(key=lambda x: x["expires_sec"])
    return json.dumps(leases, ensure_ascii=False, indent=2)


def tool_get_interfaces(args):
    result = rci({"show": {"interface": {}}})
    return json.dumps(result, ensure_ascii=False, indent=2)


def tool_get_log(args):
    lines = args.get("lines", 50)
    filter_text = args.get("filter", "")
    result = rci({"show": {"log": {}}}, timeout=30)
    log_dict = _parse_log_dict(result)
    entries = []
    for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        entries.append(_format_log_line(log_dict[k]))
    if filter_text:
        entries = [l for l in entries if filter_text.lower() in l.lower()]
    entries = _log_time_window(entries, args.get("since"), args.get("until"))
    return "\n".join(entries[-lines:])


def tool_get_log_by_device(args):
    device = args.get("device", "").strip().lower()
    lines = args.get("lines", 50)
    if not device:
        return "Error: device required (MAC, IP or name)"

    hosts = _get_hotspot_hosts()
    search_terms = {device}
    for h in hosts:
        name_val = h.get("name", "").lower()
        mac = h.get("mac", "").lower()
        ip = h.get("ip", "").lower()
        hostname = h.get("hostname", "").lower()
        if device in (mac, ip, name_val, hostname):
            search_terms.update([mac, ip, name_val, hostname])
    search_terms = {t for t in search_terms if t}

    result = rci({"show": {"log": {}}}, timeout=30)
    log_dict = _parse_log_dict(result)
    entries = []
    for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
        line = _format_log_line(log_dict[k])
        if any(t in line.lower() for t in search_terms if t):
            entries.append(line)
    if not entries:
        return f"No log entries found for device: {device}"
    return "\n".join(entries[-lines:])


def tool_get_wifi(args):
    result = rci({"show": {"interface": {}}})
    ifaces = result.get("show", {}).get("interface", {})
    stations_result = rci({"show": {"associations": {}}})
    stations = stations_result.get("show", {}).get("associations", {}).get("station", [])
    output = []
    for iface_name, iface in ifaces.items():
        if not isinstance(iface, dict):
            continue
        if iface.get("type") != "WifiMaster":
            continue
        ap_count = sum(1 for s in stations if s.get("ap", "").startswith(iface_name))
        output.append({
            "name": iface_name,
            "state": iface.get("state"),
            "channel": iface.get("channel"),
            "bandwidth": iface.get("bandwidth"),
            "bitrate_mbps": round(iface.get("bitrate", 0) / 1000000, 1) if iface.get("bitrate") else None,
            "temperature_c": iface.get("temperature"),
            "connected_stations": ap_count,
            "busy_channels": iface.get("busy-channels", []),
        })
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_wifi_stations(args):
    assoc_result = rci({"show": {"associations": {}}})
    stations = assoc_result.get("show", {}).get("associations", {}).get("station", [])
    hosts = _get_hotspot_hosts()

    output = []
    seen_macs = set()

    # Controller-connected stations
    for s in stations:
        mac = s.get("mac", "").lower()
        seen_macs.add(mac)
        host = next((h for h in hosts if h.get("mac", "").lower() == mac), {})
        output.append({
            "name": host.get("name", host.get("hostname", "")),
            "mac": mac,
            "node": "controller",
            "ap": s.get("ap"),
            "rssi": s.get("rssi"),
            "mode": s.get("mode"),
            "txrate": s.get("txrate"),
            "rxrate": s.get("rxrate"),
            "txbytes": s.get("txbytes"),
            "rxbytes": s.get("rxbytes"),
            "uptime": s.get("uptime"),
            "security": s.get("security"),
        })

    # Extender clients (mws-backhaul)
    for h in hosts:
        if not h.get("active"):
            continue
        if not h.get("mws-backhaul"):
            continue
        mac = h.get("mac", "").lower()
        if mac in seen_macs:
            continue
        if h.get("system-mode") == "extender":
            continue
        if h.get("port"):  # wired
            continue
        mws = h.get("mws", {})
        output.append({
            "name": h.get("name", h.get("hostname", "")),
            "mac": mac,
            "node": "extender",
            "ap": mws.get("ap", ""),
            "rssi": mws.get("rssi"),
            "mode": mws.get("mode"),
            "txrate": mws.get("txrate"),
            "rxrate": None,
            "txbytes": h.get("txbytes"),
            "rxbytes": h.get("rxbytes"),
            "uptime": mws.get("uptime"),
            "security": mws.get("security"),
        })

    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_traffic(args):
    hosts = _get_hotspot_hosts()
    active = [h for h in hosts if h.get("active")]
    total_rx = sum(h.get("rxbytes", 0) or 0 for h in active)
    total_tx = sum(h.get("txbytes", 0) or 0 for h in active)
    top = sorted(active, key=lambda h: (h.get("rxbytes", 0) or 0) + (h.get("txbytes", 0) or 0), reverse=True)[:10]
    output = {
        "total_active_clients": len(active),
        "total_rx_bytes": total_rx,
        "total_tx_bytes": total_tx,
        "top_clients": [
            {
                "name": h.get("name", h.get("hostname", h.get("mac"))),
                "ip": h.get("ip"),
                "rx_bytes": h.get("rxbytes", 0),
                "tx_bytes": h.get("txbytes", 0),
            }
            for h in top
        ],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_internet_status(args):
    result = rci({"show": {"interface": {}}})
    interfaces = result.get("show", {}).get("interface", {})
    output = []
    for iface_name, iface in interfaces.items():
        if not isinstance(iface, dict):
            continue
        if iface.get("global") and iface.get("state") == "up":
            output.append({
                "name": iface_name,
                "description": iface.get("description", ""),
                "address": iface.get("address"),
                "uptime": iface.get("uptime"),
                "defaultgw": iface.get("defaultgw", False),
                "priority": iface.get("priority"),
            })
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_site_survey(args):
    aps = []
    for master in ["WifiMaster0", "WifiMaster1"]:
        result = rci({"show": {"site-survey": {"name": master}}})
        cells = result.get("show", {}).get("site-survey", {}).get("ap_cell", [])
        for ap in cells:
            if not any(a.get("address") == ap.get("address") for a in aps):
                aps.append(ap)
    output = [
        {
            "ssid": ap.get("essid"),
            "mac": ap.get("address"),
            "channel": ap.get("channel"),
            "rssi": ap.get("rssi"),
            "quality": ap.get("quality"),
            "encryption": ap.get("encryption"),
            "mode": ap.get("ieee"),
        }
        for ap in aps
    ]
    output.sort(key=lambda x: x.get("rssi", -999), reverse=True)
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_channel_analysis(args):
    aps = []
    for master in ["WifiMaster0", "WifiMaster1"]:
        result = rci({"show": {"site-survey": {"name": master}}})
        cells = result.get("show", {}).get("site-survey", {}).get("ap_cell", [])
        for ap in cells:
            if not any(a.get("address") == ap.get("address") for a in aps):
                aps.append(ap)

    channel_count = {}
    channel_quality = {}
    for ap in aps:
        ch = ap.get("channel")
        if not ch:
            continue
        channel_count[ch] = channel_count.get(ch, 0) + 1
        channel_quality[ch] = channel_quality.get(ch, 0) + ap.get("quality", 0)

    channels_24 = [1, 6, 11]
    channels_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 132, 136, 140, 149, 153, 157, 161]

    def analyze(channels):
        result = []
        for ch in channels:
            result.append({
                "channel": ch,
                "networks": channel_count.get(ch, 0),
                "total_quality": channel_quality.get(ch, 0),
            })
        result.sort(key=lambda x: (x["networks"], x["total_quality"]))
        return result

    output = {
        "2.4GHz": {
            "recommended": analyze(channels_24)[0]["channel"],
            "channels": analyze(channels_24),
        },
        "5GHz": {
            "recommended": analyze(channels_5)[0]["channel"] if any(ch in channel_count for ch in channels_5) else 36,
            "channels": [c for c in analyze(channels_5) if c["networks"] > 0 or c["channel"] in [36, 44, 149, 157]],
        },
        "all_detected": [{"channel": k, "networks": v} for k, v in sorted(channel_count.items())],
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_vpn_status(args):
    result = rci({"show": {"interface": {}}})
    interfaces = result.get("show", {}).get("interface", {})
    vpn_types = ["Wireguard", "IPsec", "OpenVPN", "L2tp", "Pptp", "Sstp", "OpenConnect"]
    output = []
    for iface_name, iface in interfaces.items():
        if not isinstance(iface, dict):
            continue
        if iface.get("type") not in vpn_types:
            continue
        entry = {
            "name": iface_name,
            "type": iface.get("type"),
            "description": iface.get("description", ""),
            "state": iface.get("state"),
            "link": iface.get("link"),
            "address": iface.get("address"),
            "uptime": iface.get("uptime"),
        }
        if iface.get("type") == "Wireguard" and iface.get("wireguard"):
            wg = iface["wireguard"]
            entry["wireguard"] = {
                "public_key": wg.get("public-key"),
                "listen_port": wg.get("listen-port"),
                "peers": [
                    {
                        "public_key": p.get("public-key"),
                        "description": p.get("description", ""),
                        "remote_endpoint": f"{p.get('remote-endpoint-address')}:{p.get('remote-port')}",
                        "online": p.get("online"),
                        "rxbytes": p.get("rxbytes"),
                        "txbytes": p.get("txbytes"),
                        "last_handshake": p.get("last-handshake"),
                    }
                    for p in wg.get("peer", [])
                ],
            }
        output.append(entry)
    if not output:
        return "No VPN interfaces found"
    return json.dumps(output, ensure_ascii=False, indent=2)


def tool_get_web_access(args):
    try:
        with open('/tmp/nginx/nginx.conf') as f:
            lines = f.readlines()
        servers = []
        current = {}
        for line in lines:
            line = line.strip()
            if line.startswith('server_name ') and 'keenetic.link' in line:
                current['domain'] = line.replace('server_name ', '').rstrip(';')
            if 'ndm_proxy_upstream' in line and 'set $' in line:
                m = re.search(r'"([\d.:]+)"', line)
                if m:
                    current['upstream'] = m.group(1)
            if current.get('domain') and current.get('upstream'):
                servers.append(dict(current))
                current = {}
        return json.dumps(servers, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error reading nginx config: {e}"


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



def _rci_statuses(result):
    """Extract flat list of status dicts from an RCI response (any depth)."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            st = node.get("status")
            if isinstance(st, list):
                found.extend(s for s in st if isinstance(s, dict))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(result)
    return found


def _rci_errors(result):
    return [s for s in _rci_statuses(result) if s.get("status") == "error"]


def _save_config():
    """Persist running-config so changes survive a reboot (web UI does this
    automatically; raw RCI writes do not)."""
    try:
        rci({"system": {"configuration": {"save": {}}}})
        return True
    except Exception as e:
        syslog(f"WARNING: config save failed: {e}")
        return False


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


def tool_get_mesh_nodes(args):
    hosts = _get_hotspot_hosts()
    extenders = [h for h in hosts if h.get("system-mode") == "extender"]
    sys_result = rci({"show": {"version": {}, "system": {}}})
    version = sys_result.get("show", {}).get("version", {})
    total_clients = sum(1 for h in hosts if h.get("active") and not h.get("system-mode"))
    controller_clients = sum(
        1 for h in hosts
        if h.get("active") and not h.get("system-mode") and not h.get("mws-backhaul")
    )
    extender_clients = sum(
        1 for h in hosts
        if h.get("active") and not h.get("system-mode") and h.get("mws-backhaul")
    )
    nodes = [{
        "role": "controller",
        "name": version.get("description", ""),
        "model": version.get("hw_id", ""),
        "firmware": version.get("release", ""),
        "active_clients": controller_clients,
        "total_active_clients": total_clients,
        "connection": "direct",
    }]
    for e in extenders:
        nodes.append({
            "role": "extender",
            "name": e.get("name", ""),
            "model": e.get("description", ""),
            "firmware": e.get("firmware", ""),
            "ip": e.get("ip"),
            "mac": e.get("mac"),
            "connection_speed_mbps": e.get("speed"),
            "uptime_sec": e.get("uptime"),
            "port": e.get("port"),
            "active": e.get("active"),
            "active_clients": extender_clients,
        })
    return json.dumps(nodes, ensure_ascii=False, indent=2)


def tool_get_extender_log(args):
    lines = args.get("lines", 50)
    filter_text = args.get("filter", "")
    target_ip = args.get("extender_ip", "").strip()

    extenders = _get_extender_hosts()
    if not extenders:
        return "No active extenders found"

    if target_ip:
        extenders = [e for e in extenders if e["ip"] == target_ip]
        if not extenders:
            return f"Extender {target_ip} not found or not active"

    output = []
    for ext in extenders:
        ip = ext["ip"]
        name = ext["name"]
        try:
            result = _rci_node(ip, {"show": {"log": {}}}, timeout=20)
            log_dict = _parse_log_dict(result)
            entries = []
            for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                line = _format_log_line(log_dict[k])
                entries.append(line)
            if filter_text:
                entries = [l for l in entries if filter_text.lower() in l.lower()]
            entries = entries[-lines:]
            output.append(f"[extender: {ip} — {name}]")
            output.extend(entries)
            output.append("")
        except Exception as e:
            output.append(f"[extender: {ip} — {name}] ERROR: {e}")
            output.append("")

    return "\n".join(output).strip()


def dump_log_to_nas(timeout=12):
    """Snapshot the router log to /tmp (tmpfs/RAM) and rsync it to NAS.
    Used before a reboot so the pre-reboot log survives. No flash writes."""
    if not (BACKUP_RSYNC_HOST and BACKUP_RSYNC_USER and BACKUP_RSYNC_PATH):
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
    if not BACKUP_ENABLED:
        return "Backup is disabled. Set BACKUP_ENABLED=true in .env to enable."
    threading.Thread(target=do_backup, daemon=True).start()
    dest = f"{BACKUP_RSYNC_USER}@{BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}" if BACKUP_RSYNC_HOST else BACKUP_PATH
    return f"Backup started. Config will be saved to: {dest}"


# ---------------------------------------------------------------------------
# v2.3.0 - observability helpers
# ---------------------------------------------------------------------------

# rci_query is GET-only under /rci/show/. These subtrees are refused outright:
# running-config has its own masked tool, the rest carry keys and hashes.
RCI_GET_BLACKLIST = ("running-config", "crypto", "ppp", "user")
RCI_MAX_CHARS = 40000

SECRET_PATTERNS = [
    (re.compile(r"(\bmd5\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bnthash\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bpassword\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bpsk\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bwpa-psk\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bsecret\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(private-key\s+)\S+", re.I), r"\1***"),
    (re.compile(r"(\bkey\s+)[A-Za-z0-9+/=]{16,}", re.I), r"\1***"),
]

_LOG_TS_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _rci_get(path, timeout=15):
    """GET /rci/<path>. Read-only by construction: there is no request body
    here, and writing to RCI requires a POST with one."""
    global session_cookie
    if not session_cookie:
        auth()

    def do_request():
        req = urllib.request.Request(
            "%s/rci/%s" % (HOST, path),
            headers={"Cookie": session_cookie or ""},
            method="GET",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        resp = do_request()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            session_cookie = None
            auth()
            resp = do_request()
        else:
            raise
    return resp.read().decode("utf-8", "replace")


def _running_config_lines():
    data = json.loads(fetch_running_config())
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        for key in ("running-config", "config", "message"):
            val = data.get(key)
            if isinstance(val, list):
                return [str(x) for x in val]
    return str(data).splitlines()


def _mask_secrets(lines):
    out = []
    for line in lines:
        for pat, repl in SECRET_PATTERNS:
            line = pat.sub(repl, line)
        out.append(line)
    return out


def _config_lines(prefix, lines=None):
    """Top-level running-config lines starting with prefix."""
    src = lines if lines is not None else _running_config_lines()
    return [l.strip() for l in src
            if not l.startswith(" ") and l.strip().startswith(prefix)]


def _config_blocks(prefix, lines=None):
    """Top-level entries starting with prefix, together with their indented
    children. Returns a list of lists of stripped strings."""
    src = lines if lines is not None else _running_config_lines()
    blocks, cur = [], None
    for l in src:
        stripped = l.strip()
        if not l.startswith(" "):
            if stripped.startswith(prefix):
                cur = [stripped]
                blocks.append(cur)
            else:
                cur = None
        elif cur is not None and stripped and stripped != "!":
            cur.append(stripped)
    return blocks


def _parse_bound(text):
    """Accepts 'HH:MM', 'HH:MM:SS' or 'Jul 24 08:00[:SS]'."""
    if not text:
        return None
    text = str(text).strip()
    now = datetime.now()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if m:
        hh, mm, ss = m.group(1), m.group(2), m.group(3) or "0"
        return datetime(now.year, now.month, now.day, int(hh), int(mm), int(ss))
    m = re.match(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if m:
        mon = _MONTHS.get(m.group(1).title())
        if mon:
            return datetime(now.year, mon, int(m.group(2)),
                            int(m.group(3)), int(m.group(4)),
                            int(m.group(5) or 0))
    return None


def _log_time_window(entries, since=None, until=None):
    lo, hi = _parse_bound(since), _parse_bound(until)
    if not lo and not hi:
        return entries
    out = []
    for line in entries:
        m = _LOG_TS_RE.search(line)
        if not m:
            continue
        mon = _MONTHS.get(m.group(1))
        if not mon:
            continue
        try:
            ts = datetime(datetime.now().year, mon, int(m.group(2)),
                          int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            continue
        if lo and ts < lo:
            continue
        if hi and ts > hi:
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# v2.3.0 - tools
# ---------------------------------------------------------------------------

def tool_rci_query(args):
    # v2.4.0: config_tree=true reads /rci/<path> instead of /rci/show/<path>.
    # Still read-only by construction - a GET carries no body, and RCI only
    # writes when it receives one. The blacklist applies to both trees.
    config_tree = bool(args.get("config_tree"))
    path = args.get("path", "").strip().strip("/")
    if not path:
        return ("Error: path required. Examples: 'system', 'media', 'clock', "
                "'schedule', 'dns-proxy', 'interface/GigabitEthernet1'")
    path = path.replace("..", "").strip("/")
    low = path.lower()
    for bad in RCI_GET_BLACKLIST:
        if low.startswith(bad):
            hint = " Use get_config instead (masked)." if bad == "running-config" else ""
            return "Error: '%s' is blacklisted for rci_query.%s" % (bad, hint)
    prefix = "" if config_tree else "show/"
    try:
        raw = _rci_get("%s%s" % (prefix, path))
    except urllib.error.HTTPError as e:
        return "HTTP %s for %s%s - the path probably does not exist" % (e.code, prefix, path)
    except Exception as e:
        return "Error querying %s%s: %s" % (prefix, path, e)
    if len(raw) > RCI_MAX_CHARS:
        return raw[:RCI_MAX_CHARS] + "\n... [truncated, %d chars total]" % len(raw)
    return raw


def tool_get_config(args):
    flt = str(args.get("filter", "") or "").strip()
    try:
        limit = min(int(args.get("limit", 400)), 2000)
    except (TypeError, ValueError):
        limit = 400
    try:
        lines = _running_config_lines()
    except Exception as e:
        return "Error fetching running-config: %s" % e
    if flt:
        try:
            rx = re.compile(flt, re.I)
        except re.error as e:
            return "Error: bad regex %r: %s" % (flt, e)
        lines = [l for l in lines if rx.search(l)]
    if not args.get("include_secrets"):
        lines = _mask_secrets(lines)
    total = len(lines)
    shown = lines[:limit]
    head = "# %d line(s) matched" % total
    if total > len(shown):
        head += ", showing first %d" % len(shown)
    return head + "\n" + "\n".join(shown)


def tool_get_port_forwarding(args):
    try:
        rules = _config_lines("ip static")
    except Exception as e:
        return "Error: %s" % e
    if not rules:
        return "No 'ip static' rules (port forwarding / NAT) found in running-config"
    return json.dumps({"count": len(rules), "rules": rules},
                      ensure_ascii=False, indent=2)


def tool_get_firewall_rules(args):
    try:
        lines = _running_config_lines()
    except Exception as e:
        return "Error: %s" % e
    acl = _config_blocks("access-list", lines)
    ip_fw = _config_lines("ip firewall", lines)
    iface_acl = [l for l in _config_lines("ip access-group", lines)]
    if not acl and not ip_fw and not iface_acl:
        return "No firewall rules (access-list / ip firewall) found in running-config"
    return json.dumps({
        "access_lists": [{"name": b[0], "rules": b[1:]} for b in acl],
        "ip_firewall": ip_fw,
        "access_groups": iface_acl,
    }, ensure_ascii=False, indent=2)


def tool_get_dhcp_static(args):
    try:
        hosts = _config_lines("ip dhcp host")
    except Exception as e:
        return "Error: %s" % e
    if not hosts:
        return "No static DHCP reservations ('ip dhcp host') found"
    return json.dumps({"count": len(hosts), "hosts": hosts},
                      ensure_ascii=False, indent=2)


def tool_get_keendns_mappings(args):
    """KeenDNS / web-access mappings straight from running-config.
    Complements get_web_access, which reads the generated nginx config."""
    try:
        blocks = _config_blocks("ip http proxy", _running_config_lines())
    except Exception as e:
        return "Error: %s" % e
    if not blocks:
        return "No 'ip http proxy' entries found in running-config"
    return json.dumps([{"proxy": b[0], "settings": b[1:]} for b in blocks],
                      ensure_ascii=False, indent=2)


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
    if not (BACKUP_RSYNC_HOST and BACKUP_RSYNC_USER and BACKUP_RSYNC_PATH):
        return "rsync backup target is not configured (BACKUP_RSYNC_* in .env)"
    if subprocess.run(["which", "rsync"], capture_output=True).returncode != 0:
        return "rsync not found on the router: opkg install rsync"
    cmd = ["rsync", "--list-only"]
    if BACKUP_RSYNC_KEY:
        cmd += ["-e", "ssh -i %s -o StrictHostKeyChecking=no" % BACKUP_RSYNC_KEY]
    cmd += ["%s@%s:%s/" % (BACKUP_RSYNC_USER, BACKUP_RSYNC_HOST, BACKUP_RSYNC_PATH)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "Timeout listing %s:%s" % (BACKUP_RSYNC_HOST, BACKUP_RSYNC_PATH)
    return r.stdout.strip() or r.stderr.strip() or "(empty listing)"


# ---------------------------------------------------------------------------
# v2.4.0 - write tools (port forwarding, KeenDNS, static DHCP)
#
# Payload shapes below are NOT guessed. They were taken from the live config
# tree of a KN-1010 on NDMS 5.1.1 (GET /rci/ip/static, /rci/ip/http/proxy,
# /rci/ip/dhcp/host) and cross-checked against the vendor CLI reference for
# 'ip static'. Deletion uses the documented {"no": true} form.
#
# Every mutating tool defaults to dry_run=True: it returns the exact payload
# it would send and changes nothing. A real write is always followed by
# 'system configuration save' (raw RCI writes do not survive a reboot without
# it) and by a re-read of the affected subtree, so the caller gets a
# before/after diff instead of a "command sent" claim.
# ---------------------------------------------------------------------------

# Refusals are in code, not in the description: it is trivially easy to shoot
# away the very channel this server is reached through.
PROTECTED_PROXY_NAMES = {
    "keenetic-mcp", "ha-mcp", "vault-mcp", "adb-mcp", "homeassistant", "ntfy",
}
PROTECTED_PORTS = {9584, 8123, 9583, 3100, 3200, 7612}
PROTECTED_UPSTREAMS = {("127.0.0.1", 9584)}

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class GuardError(Exception):
    """Raised when a request would touch a protected object."""


def _rci_tree(path):
    """GET a config-tree branch (not /rci/show/) and parse it."""
    raw = _rci_get(path)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _norm_mac(value):
    mac = str(value or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(mac):
        raise GuardError("'%s' is not a MAC address (expected aa:bb:cc:dd:ee:ff)" % value)
    return mac


def _resolve_to_host(value):
    """Port forwarding targets a MAC, never an IP. Accept either and resolve,
    refusing hosts the router does not know - a typo would otherwise create a
    rule that silently forwards nowhere."""
    value = str(value or "").strip().lower()
    if _MAC_RE.match(value.replace("-", ":")):
        return _norm_mac(value)
    if not _IP_RE.match(value):
        raise GuardError("'%s' is neither a MAC nor an IPv4 address" % value)
    for entry in _rci_tree("ip/dhcp/host") or []:
        if isinstance(entry, dict) and entry.get("ip") == value:
            return _norm_mac(entry.get("mac"))
    for host in _get_hotspot_hosts():
        if host.get("ip") == value and host.get("mac"):
            return _norm_mac(host.get("mac"))
    raise GuardError(
        "no registered host has IP %s - register it first (register_client) or "
        "pass the MAC explicitly" % value)


def _guard_port(port, what):
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        raise GuardError("port '%s' is not a number" % port)
    if not 1 <= port_i <= 65535:
        raise GuardError("port %s is out of range" % port)
    if port_i in PROTECTED_PORTS:
        raise GuardError(
            "port %s is protected (%s): it serves keenetic-mcp or another MCP "
            "endpoint. Change it by hand in the web UI if you really mean to."
            % (port_i, what))
    return str(port_i)


def _guard_proxy_name(name):
    name = str(name or "").strip()
    if not name:
        raise GuardError("proxy name is required")
    if name.lower() in PROTECTED_PROXY_NAMES:
        raise GuardError(
            "KeenDNS mapping '%s' is protected - removing or repointing it would "
            "cut the channel this server is reached through. Web UI only." % name)
    return name


def _guard_upstream(host, port):
    if (str(host), int(port)) in PROTECTED_UPSTREAMS:
        raise GuardError("upstream %s:%s is protected" % (host, port))


def _commit(payload, verify_path, dry_run, summary):
    """Shared tail for every mutating tool."""
    if dry_run:
        return json.dumps({
            "dry_run": True,
            "summary": summary,
            "would_send": payload,
            "note": "nothing was changed. Re-run with dry_run=false to apply.",
        }, ensure_ascii=False, indent=2)

    before = _rci_tree(verify_path)
    try:
        result = rci(payload)
    except Exception as e:
        return "Error sending payload: %s" % e
    errors = _rci_errors(result)
    saved = _save_config() if not errors else False
    after = _rci_tree(verify_path)
    return json.dumps({
        "dry_run": False,
        "summary": summary,
        "sent": payload,
        "statuses": _rci_statuses(result),
        "errors": errors,
        "config_saved": saved,
        "before": before,
        "after": after,
        "changed": before != after,
    }, ensure_ascii=False, indent=2)


def _static_rules():
    rules = _rci_tree("ip/static")
    return rules if isinstance(rules, list) else []


def tool_set_port_forwarding(args):
    try:
        to_host = _resolve_to_host(args.get("to_host"))
        port = _guard_port(args.get("port"), "external port")
        proto = str(args.get("protocol", "tcp")).lower()
        if proto not in ("tcp", "udp"):
            raise GuardError("protocol must be tcp or udp")
        rule = {
            "interface": str(args.get("interface", "GigabitEthernet1")),
            "protocol": proto,
            "port": port,
            "to-host": to_host,
        }
        if args.get("to_port"):
            rule["to-port"] = _guard_port(args["to_port"], "internal port")
        if args.get("end_port"):
            rule["end-port"] = _guard_port(args["end_port"], "range end")
        if args.get("comment"):
            rule["comment"] = str(args["comment"])
        # 'disable' is a per-rule attribute; clearing it uses the {"no": true}
        # attribute-removal form.
        rule["disable"] = True if args.get("enable") is False else {"no": True}
    except GuardError as e:
        return "Refused: %s" % e
    return _commit({"ip": {"static": rule}}, "ip/static", args.get("dry_run", True),
                   "forward %s/%s on %s -> %s" % (proto, port, rule["interface"], to_host))


def tool_remove_port_forwarding(args):
    rules = _static_rules()
    index = str(args.get("index", "") or "").strip()
    port = str(args.get("port", "") or "").strip()
    proto = str(args.get("protocol", "") or "").strip().lower()

    if index:
        matches = [r for r in rules if r.get("index") == index]
    elif port:
        matches = [r for r in rules if r.get("port") == port
                   and (not proto or r.get("protocol") == proto)]
    else:
        return "Error: pass either index or port (see get_port_forwarding)"

    if not matches:
        return "No matching rule found. Current rules:\n" + json.dumps(
            rules, ensure_ascii=False, indent=2)
    if len(matches) > 1:
        return ("%d rules match - narrow it down with index:\n%s"
                % (len(matches), json.dumps(matches, ensure_ascii=False, indent=2)))

    rule = matches[0]
    try:
        _guard_port(rule.get("port"), "rule being removed")
    except GuardError as e:
        return "Refused: %s" % e

    payload_rule = {k: v for k, v in rule.items() if k != "disable"}
    payload_rule["no"] = True
    return _commit({"ip": {"static": payload_rule}}, "ip/static",
                   args.get("dry_run", True),
                   "remove rule %s (%s/%s -> %s)" % (
                       rule.get("index"), rule.get("protocol"),
                       rule.get("port"), rule.get("to-host")))


def tool_set_keendns_mapping(args):
    try:
        name = _guard_proxy_name(args.get("name"))
        host = str(args.get("host", "")).strip()
        if not host:
            raise GuardError("host is required (IP, MAC or 127.0.0.1)")
        port = _guard_port(args.get("port"), "upstream port")
        _guard_upstream(host, port)
        proto = str(args.get("proto", "http")).lower()
        if proto not in ("http", "https"):
            raise GuardError("proto must be http or https")
        level = str(args.get("security_level", "public")).lower()
        if level not in ("public", "private"):
            raise GuardError("security_level must be public or private")
    except GuardError as e:
        return "Refused: %s" % e

    entry = {
        "upstream": {"proto": proto, "upstream": host, "port": port},
        "domain": {"ndns": True},
        "ssl": {"redirect": True},
        "security-level": {level: True},
    }
    return _commit({"ip": {"http": {"proxy": {name: entry}}}}, "ip/http/proxy",
                   args.get("dry_run", True),
                   "KeenDNS '%s' -> %s://%s:%s (%s)" % (name, proto, host, port, level))


def tool_remove_keendns_mapping(args):
    try:
        name = _guard_proxy_name(args.get("name"))
    except GuardError as e:
        return "Refused: %s" % e
    existing = _rci_tree("ip/http/proxy")
    if isinstance(existing, dict) and name not in existing:
        return "No KeenDNS mapping named '%s'. Existing: %s" % (
            name, ", ".join(sorted(existing.keys())))
    return _commit({"ip": {"http": {"proxy": {name: {"no": True}}}}},
                   "ip/http/proxy", args.get("dry_run", True),
                   "remove KeenDNS mapping '%s'" % name)


def tool_set_dhcp_host(args):
    try:
        mac = _norm_mac(args.get("mac"))
        ip = str(args.get("ip", "")).strip()
        if not _IP_RE.match(ip):
            raise GuardError("'%s' is not an IPv4 address" % ip)
    except GuardError as e:
        return "Refused: %s" % e
    for entry in _rci_tree("ip/dhcp/host") or []:
        if isinstance(entry, dict) and entry.get("ip") == ip and entry.get("mac") != mac:
            return ("Refused: %s is already reserved for %s. Free it first."
                    % (ip, entry.get("mac")))
    return _commit({"ip": {"dhcp": {"host": {"mac": mac, "ip": ip}}}},
                   "ip/dhcp/host", args.get("dry_run", True),
                   "reserve %s for %s" % (ip, mac))


def tool_remove_dhcp_host(args):
    try:
        mac = _norm_mac(args.get("mac"))
    except GuardError as e:
        return "Refused: %s" % e
    entries = _rci_tree("ip/dhcp/host") or []
    match = next((e for e in entries
                  if isinstance(e, dict) and e.get("mac") == mac), None)
    if not match:
        return "No static DHCP reservation for %s" % mac
    return _commit({"ip": {"dhcp": {"host": {"mac": mac, "no": True}}}},
                   "ip/dhcp/host", args.get("dry_run", True),
                   "remove reservation %s (%s)" % (match.get("ip"), mac))


WRITE_TOOLS = {
    "set_port_forwarding": {
        "description": (
            "Create or update a port forwarding rule ('ip static'). The target is "
            "addressed by MAC: pass to_host as a MAC, or as an IP that belongs to a "
            "registered host (it is resolved, and refused if unknown). dry_run is "
            "TRUE by default - it returns the payload without sending it. A real "
            "write is saved to startup-config and verified by re-reading the tree. "
            "Ports serving MCP endpoints are refused in code."
        ),
        "inputSchema": {"type": "object", "properties": {
            "port": {"type": "integer", "description": "External port"},
            "to_host": {"type": "string", "description": "Target MAC, or IP of a registered host"},
            "protocol": {"type": "string", "description": "tcp (default) or udp"},
            "interface": {"type": "string", "description": "WAN interface, default GigabitEthernet1"},
            "to_port": {"type": "integer", "description": "Internal port, if different"},
            "end_port": {"type": "integer", "description": "End of external port range"},
            "comment": {"type": "string", "description": "Rule comment"},
            "enable": {"type": "boolean", "description": "false disables the rule (per-rule flag)"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }, "required": ["port", "to_host"]},
        "fn": tool_set_port_forwarding,
    },
    "remove_port_forwarding": {
        "description": (
            "Delete a port forwarding rule, selected by index (from "
            "get_port_forwarding) or by port. Refuses ambiguous matches and "
            "protected ports. dry_run is TRUE by default."
        ),
        "inputSchema": {"type": "object", "properties": {
            "index": {"type": "string", "description": "Rule index hash"},
            "port": {"type": "integer", "description": "External port, if index is unknown"},
            "protocol": {"type": "string", "description": "Narrow a port match to tcp/udp"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }},
        "fn": tool_remove_port_forwarding,
    },
    "set_keendns_mapping": {
        "description": (
            "Create or update a KeenDNS web-access mapping ('ip http proxy'): "
            "name -> upstream host:port, published on the ndns domain with ssl "
            "redirect. Protected names (the MCP servers and Home Assistant) are "
            "refused in code. dry_run is TRUE by default."
        ),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Subdomain label, e.g. 'grafana'"},
            "host": {"type": "string", "description": "Upstream IP or MAC (127.0.0.1 for the router itself)"},
            "port": {"type": "integer", "description": "Upstream port"},
            "proto": {"type": "string", "description": "http (default) or https"},
            "security_level": {"type": "string", "description": "public (default) or private"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }, "required": ["name", "host", "port"]},
        "fn": tool_set_keendns_mapping,
    },
    "remove_keendns_mapping": {
        "description": "Delete a KeenDNS mapping by name. Protected names are refused. dry_run is TRUE by default.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Mapping name"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }, "required": ["name"]},
        "fn": tool_remove_keendns_mapping,
    },
    "set_dhcp_host": {
        "description": (
            "Create or update a static DHCP reservation ('ip dhcp host'). Refuses "
            "an IP already reserved for a different MAC. The device NAME lives in "
            "the known-host tree - use register_client/update_client for that. "
            "dry_run is TRUE by default."
        ),
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address"},
            "ip": {"type": "string", "description": "IPv4 address to pin"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }, "required": ["mac", "ip"]},
        "fn": tool_set_dhcp_host,
    },
    "remove_dhcp_host": {
        "description": "Delete a static DHCP reservation by MAC. dry_run is TRUE by default.",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address"},
            "dry_run": {"type": "boolean", "description": "Default true"},
        }, "required": ["mac"]},
        "fn": tool_remove_dhcp_host,
    },
}


# ---------------------------------------------------------------------------
# Tool registry & dispatcher
# ---------------------------------------------------------------------------

TOOLS = {
    "get_system_info": {
        "description": "Get router system info: version, uptime, CPU, memory",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_system_info,
    },
    "get_clients": {
        "description": "Get list of connected clients (devices) in the network. Each client includes a 'node' field (controller/extender) indicating which mesh node it is connected to.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_clients,
    },
    "get_unregistered_clients": {
        "description": "Get list of active but unregistered (unknown) devices in the network",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_unregistered_clients,
    },
    "get_dhcp_leases": {
        "description": "Get list of devices with active DHCP leases including expiry time",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_dhcp_leases,
    },
    "get_interfaces": {
        "description": "Get network interfaces status and traffic stats",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_interfaces,
    },
    "get_log": {
        "description": "Get system log entries with timestamps. Supports an optional time window via since/until ('HH:MM', 'HH:MM:SS' or 'Jul 24 08:00').",
        "inputSchema": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "Number of lines (default 50)"},
            "filter": {"type": "string", "description": "Filter text to search in log lines"}, "since": {"type": "string", "description": "Only entries at or after this time"}, "until": {"type": "string", "description": "Only entries at or before this time"}}},
        "fn": tool_get_log,
    },
    "get_log_by_device": {
        "description": "Get system log entries filtered by device MAC address, IP address or name",
        "inputSchema": {"type": "object", "properties": {
            "device": {"type": "string", "description": "MAC address, IP address or device name"},
            "lines": {"type": "integer", "description": "Number of lines (default 50)"},
        }, "required": ["device"]},
        "fn": tool_get_log_by_device,
    },
    "get_wifi": {
        "description": "Get WiFi radio status: channel, bandwidth, bitrate, temperature, connected stations count",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_wifi,
    },
    "get_wifi_stations": {
        "description": "Get currently associated WiFi stations with signal strength, traffic, device name and mesh node (controller/extender)",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_wifi_stations,
    },
    "get_traffic": {
        "description": "Get traffic summary for all active network interfaces (rx/tx bytes)",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_traffic,
    },
    "get_internet_status": {
        "description": "Get internet connection status and external IP",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_internet_status,
    },
    "get_site_survey": {
        "description": "Scan and list nearby WiFi networks",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_site_survey,
    },
    "get_channel_analysis": {
        "description": "Analyze WiFi channel congestion and recommend the least busy channel",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_channel_analysis,
    },
    "get_vpn_status": {
        "description": "Get status of all VPN interfaces (WireGuard, IPsec, L2TP, PPTP)",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_vpn_status,
    },
    "get_web_access": {
        "description": "Get list of web applications exposed to the internet via Keenetic DDNS",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_web_access,
    },
    "run_ping": {
        "description": "Ping a host from the router and return latency and packet loss",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "Host or IP to ping"},
            "count": {"type": "integer", "description": "Number of packets (default 4)"},
        }, "required": ["host"]},
        "fn": tool_run_ping,
    },
    "register_client": {
        "description": "Register a device by MAC address, assign a name and optionally a static IP",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address, e.g. aa:bb:cc:dd:ee:ff"},
            "name": {"type": "string", "description": "Device name"},
            "ip": {"type": "string", "description": "Optional static IP address"},
        }, "required": ["mac", "name"]},
        "fn": tool_register_client,
    },
    "update_client": {
        "description": "Update name or static IP of a registered device",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address, e.g. aa:bb:cc:dd:ee:ff"},
            "name": {"type": "string", "description": "New device name"},
            "ip": {"type": "string", "description": "New static IP address"},
        }, "required": ["mac"]},
        "fn": tool_update_client,
    },
    "block_client": {
        "description": "Block a registered client by MAC address",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address to block, e.g. aa:bb:cc:dd:ee:ff"},
        }, "required": ["mac"]},
        "fn": tool_block_client,
    },
    "unblock_client": {
        "description": "Unblock a previously blocked client by MAC address",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address to unblock, e.g. aa:bb:cc:dd:ee:ff"},
        }, "required": ["mac"]},
        "fn": tool_unblock_client,
    },
    "get_mesh_nodes": {
        "description": "Get Mesh Wi-Fi system nodes: controller and extenders with client count, firmware, uptime and connection speed",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_mesh_nodes,
    },
    "get_extender_log": {
        "description": "Get system log from mesh extender(s). Extenders are discovered automatically. If extender_ip is not specified, fetches logs from all active extenders.",
        "inputSchema": {"type": "object", "properties": {
            "extender_ip": {"type": "string", "description": "Extender IP address (optional, default: all extenders)"},
            "lines": {"type": "integer", "description": "Number of log lines per extender (default 50)"},
            "filter": {"type": "string", "description": "Filter text to search in log lines"},
        }},
        "fn": tool_get_extender_log,
    },
    "reboot": {
        "description": "Reboot the router",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_reboot,
    },
    "backup_config": {
        "description": "Manually trigger a router config backup right now",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_backup_config,
    },
    "dump_log": {
        "description": "Snapshot the current router log and rsync it to the NAS backup path (RAM-only staging, no flash writes). Useful to preserve the log before a reboot.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_dump_log,
    },
    "rci_query": {
        "description": (
            "Raw READ-ONLY query against the router's RCI tree. Performs GET "
            "/rci/show/<path> - it cannot write, because writing requires a POST "
            "body and this tool never sends one. Use it to explore state that has "
            "no dedicated tool yet. Useful paths: 'clock', 'schedule', 'dns-proxy', "
            "'media', 'ntp', 'ip/hotspot', 'interface/GigabitEthernet1', "
            "'components', 'ndns', 'update'. Blacklisted: running-config (use "
            "get_config), crypto, ppp, user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path under /rci/show/, e.g. 'media'"},
                "config_tree": {"type": "boolean", "description": "Read /rci/<path> (config tree) instead of /rci/show/<path>. Use it to see write-shapes, e.g. 'ip/static'"}
            },
            "required": ["path"],
        },
        "fn": tool_rci_query,
    },
    "get_config": {
        "description": (
            "Read the router's running-config with an optional regex filter. "
            "Secrets (md5/nthash/psk/password/private-key) are masked unless "
            "include_secrets is true. Examples: filter='ip static' for port "
            "forwarding, 'access-list' for firewall, 'ip dhcp host' for static "
            "reservations, 'ip http proxy' for KeenDNS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Case-insensitive regex"},
                "limit": {"type": "integer", "description": "Max lines returned (default 400, max 2000)"},
                "include_secrets": {"type": "boolean", "description": "Return unmasked secrets (default false)"},
            },
        },
        "fn": tool_get_config,
    },
    "get_port_forwarding": {
        "description": "List port forwarding / static NAT rules ('ip static') from running-config",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_port_forwarding,
    },
    "get_firewall_rules": {
        "description": "List firewall rules: access-lists with their entries, ip firewall settings and interface access-groups",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_firewall_rules,
    },
    "get_dhcp_static": {
        "description": "List static DHCP reservations ('ip dhcp host'). Unlike get_dhcp_leases, which only shows dynamic pool leases, this shows fixed bindings.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_dhcp_static,
    },
    "get_keendns_mappings": {
        "description": "KeenDNS / web-access mappings from running-config ('ip http proxy'). Complements get_web_access, which reads the generated nginx config instead.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_keendns_mappings,
    },
    "get_media": {
        "description": "Storage overview: internal flash and USB drives with partition UUID, label, filesystem, state, free space and which subsystem uses them (e.g. opkg). Use it to check whether the Entware drive is healthy.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_media,
    },
    "get_opkg_status": {
        "description": "Entware/OPKG state: which drive is bound, the initrc path, and whether /opt is actually mounted. If opt_mounted is false, keenetic-mcp itself is running on borrowed time.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_opkg_status,
    },
    "list_backups": {
        "description": "List config backup files already present on the NAS (rsync --list-only). Confirms that scheduled backups actually arrived.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_list_backups,
    },
}

TOOLS.update(WRITE_TOOLS)


def call_tool(name, args):
    tool = TOOLS.get(name)
    if tool:
        return tool["fn"](args)
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class MCPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == f"/{SECRET}":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            caps = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
            }
            self.wfile.write(json.dumps(caps).encode())
        elif self.path == f"/{SECRET}/reboot":
            log_synced = False
            try:
                log_synced = dump_log_to_nas()
            except Exception as e:
                syslog(f"ERROR: pre-reboot log dump crashed: {e}")
            try:
                tool_reboot({})
                ok, msg, code = True, "Reboot command sent", 200
            except Exception as e:
                ok, msg, code = False, str(e), 500
            syslog(f"WARNING: HTTP reboot trigger -> {msg} (log_synced={log_synced})")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "log_synced": log_synced, "result": msg}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.path.startswith(f"/{SECRET}"):
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        method = body.get("method", "")
        req_id = body.get("id")
        response = {"jsonrpc": "2.0", "id": req_id}

        if method == "initialize":
            response["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
            }
        elif method == "tools/list":
            response["result"] = {"tools": [
                {"name": k, "description": v["description"], "inputSchema": v["inputSchema"]}
                for k, v in TOOLS.items()
            ]}
        elif method == "tools/call":
            tool_name = body.get("params", {}).get("name")
            tool_args = body.get("params", {}).get("arguments", {})
            try:
                result = call_tool(tool_name, tool_args)
                response["result"] = {"content": [{"type": "text", "text": result}]}
            except Exception as e:
                response["error"] = {"code": -32000, "message": str(e)}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_env()
    auth()

    if BACKUP_ENABLED:
        syslog(f"INFO: backup enabled, schedule='{BACKUP_SCHEDULE}'")
        threading.Thread(target=backup_scheduler, daemon=True).start()
    else:
        syslog("INFO: backup disabled (BACKUP_ENABLED=false)")

    print(f"Starting Keenetic MCP v{VERSION} on port {PORT}")
    server = http.server.HTTPServer(("0.0.0.0", PORT), MCPHandler)
    server.serve_forever()
