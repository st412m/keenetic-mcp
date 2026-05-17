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
VERSION = "1.9.0"

# Backup config
BACKUP_ENABLED = False
BACKUP_SCHEDULE = "0 11 * * 0"   # cron expression
BACKUP_PATH = "/tmp/keenetic-backup"
BACKUP_KEEP = 0                   # 0 = don't store locally
BACKUP_RSYNC_HOST = ""
BACKUP_RSYNC_USER = ""
BACKUP_RSYNC_KEY = ""
BACKUP_RSYNC_PATH = ""

session_cookie = None


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


def syslog(message):
    """Log to router syslog via logger command (no flash writes)."""
    try:
        subprocess.run(["logger", "-t", "keenetic-mcp", message],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def cron_matches(schedule, now):
    """Check if cron expression matches current datetime.
    Format: minute hour day_of_month month day_of_week
    Supports: * and comma-separated values.
    """
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


def do_backup():
    """Fetch running-config from router and optionally rsync to remote host."""
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

        size = len(config_data)
        syslog(f"INFO: backup saved locally {local_path} ({size} bytes)")
        return True


def fetch_running_config():
    """Authenticate and fetch running-config via RCI API."""
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


def rsync_to_remote(local_file, filename):
    """Rsync a file to remote host via SSH key."""
    if subprocess.run(["which", "rsync"], capture_output=True).returncode != 0:
        syslog("ERROR: rsync not found, install it: opkg install rsync")
        return False

    remote = f"{BACKUP_RSYNC_USER}@{BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}/{filename}"
    cmd = ["rsync", "-a"]
    if BACKUP_RSYNC_KEY:
        cmd += ["-e", f"ssh -i {BACKUP_RSYNC_KEY} -o StrictHostKeyChecking=no"]
    cmd += [local_file, remote]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        syslog(f"INFO: backup synced to {BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}/{filename}")
        return True
    else:
        syslog(f"ERROR: rsync failed: {result.stderr.strip()}")
        return False


def backup_scheduler():
    """Background thread: check schedule every minute, run backup when matched."""
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
    source = entry.get("source", "")
    if isinstance(msg, dict):
        label = msg.get("label", "?")
        text = msg.get("message", "")
    else:
        label = "?"
        text = str(msg)
    parts = [f"[{label}]"]
    if time_str:
        parts.append(time_str)
    if source:
        parts.append(source)
    parts.append(text)
    return " ".join(parts)


TOOLS = {
    "get_system_info": {
        "description": "Get router system info: version, uptime, CPU, memory",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_clients": {
        "description": "Get list of connected clients (devices) in the network. Each client includes a 'node' field (controller/extender) indicating which mesh node it is connected to.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_unregistered_clients": {
        "description": "Get list of active but unregistered (unknown) devices in the network",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_dhcp_leases": {
        "description": "Get list of devices with active DHCP leases including expiry time",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_interfaces": {
        "description": "Get network interfaces status and traffic stats",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_log": {
        "description": "Get system log entries with timestamps",
        "inputSchema": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "Number of lines (default 50)"},
            "filter": {"type": "string", "description": "Filter text to search in log lines"}
        }}
    },
    "get_log_by_device": {
        "description": "Get system log entries filtered by device MAC address, IP address or name",
        "inputSchema": {"type": "object", "properties": {
            "device": {"type": "string", "description": "MAC address, IP address or device name"},
            "lines": {"type": "integer", "description": "Number of lines (default 50)"}
        }, "required": ["device"]}
    },
    "get_wifi": {
        "description": "Get WiFi radio status: channel, bandwidth, bitrate, temperature, connected stations count",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_wifi_stations": {
        "description": "Get currently associated WiFi stations with signal strength, traffic, device name and mesh node (controller/extender)",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_traffic": {
        "description": "Get traffic summary for all active network interfaces (rx/tx bytes)",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_internet_status": {
        "description": "Get internet connection status and external IP",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_site_survey": {
        "description": "Scan and list nearby WiFi networks",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_channel_analysis": {
        "description": "Analyze WiFi channel congestion and recommend the least busy channel",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_vpn_status": {
        "description": "Get status of all VPN interfaces (WireGuard, IPsec, L2TP, PPTP)",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_web_access": {
        "description": "Get list of web applications exposed to the internet via Keenetic DDNS",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "run_ping": {
        "description": "Ping a host from the router and return latency and packet loss",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "Host or IP to ping"},
            "count": {"type": "integer", "description": "Number of packets (default 4)"}
        }, "required": ["host"]}
    },
    "register_client": {
        "description": "Register a device by MAC address, assign a name and optionally a static IP",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address, e.g. aa:bb:cc:dd:ee:ff"},
            "name": {"type": "string", "description": "Device name"},
            "ip": {"type": "string", "description": "Optional static IP address, e.g. 192.168.1.100"}
        }, "required": ["mac", "name"]}
    },
    "update_client": {
        "description": "Update name or static IP of a registered device",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address, e.g. aa:bb:cc:dd:ee:ff"},
            "name": {"type": "string", "description": "New device name"},
            "ip": {"type": "string", "description": "New static IP address"}
        }, "required": ["mac"]}
    },
    "block_client": {
        "description": "Block a registered client by MAC address",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address to block, e.g. aa:bb:cc:dd:ee:ff"}
        }, "required": ["mac"]}
    },
    "unblock_client": {
        "description": "Unblock a previously blocked client by MAC address",
        "inputSchema": {"type": "object", "properties": {
            "mac": {"type": "string", "description": "MAC address to unblock, e.g. aa:bb:cc:dd:ee:ff"}
        }, "required": ["mac"]}
    },
    "get_mesh_nodes": {
        "description": "Get Mesh Wi-Fi system nodes: controller and extenders with client count, firmware, uptime and connection speed",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "reboot": {
        "description": "Reboot the router",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "backup_config": {
        "description": "Manually trigger a router config backup right now",
        "inputSchema": {"type": "object", "properties": {}}
    },
}


def call_tool(name, args):
    if name == "get_system_info":
        result = rci({"show": {"version": {}, "system": {}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_clients":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
        output = []
        for h in hosts:
            ap = _get_ap(h)
            entry = {
                "name": h.get("name", h.get("hostname", "")),
                "mac": h.get("mac"),
                "ip": h.get("ip"),
                "hostname": h.get("hostname", ""),
                "active": h.get("active", False),
                "node": _get_node(h),
                "ap": ap,
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

    elif name == "get_unregistered_clients":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
        unreg = [h for h in hosts if h.get("active") and not h.get("registered")]
        output = []
        for h in unreg:
            output.append({
                "mac": h.get("mac"),
                "ip": h.get("ip"),
                "hostname": h.get("hostname", ""),
                "node": _get_node(h),
                "link": h.get("link"),
                "first_seen": h.get("first-seen"),
                "last_seen": h.get("last-seen")
            })
        if not output:
            return "No unregistered active devices found"
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_dhcp_leases":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
        leases = []
        for h in hosts:
            expires = h.get("dhcp", {}).get("expires", 0)
            if expires and expires > 0:
                leases.append({
                    "name": h.get("name", h.get("hostname", "")),
                    "mac": h.get("mac"),
                    "ip": h.get("ip"),
                    "expires_sec": expires,
                    "active": h.get("active", False)
                })
        leases.sort(key=lambda x: x["expires_sec"])
        return json.dumps(leases, ensure_ascii=False, indent=2)

    elif name == "get_interfaces":
        result = rci({"show": {"interface": {}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_log":
        lines = args.get("lines", 50)
        filter_text = args.get("filter", "")
        result = rci({"show": {"log": {}}}, timeout=30)
        log_dict = result.get("show", {}).get("log", {}).get("log", {})
        if not log_dict:
            log_dict = result.get("show", {}).get("log", {})
        entries = []
        for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            entry = log_dict[k]
            line = _format_log_line(entry)
            entries.append(line)
        if filter_text:
            entries = [l for l in entries if filter_text.lower() in l.lower()]
        return "\n".join(entries[-lines:])

    elif name == "get_log_by_device":
        device = args.get("device", "").strip().lower()
        lines = args.get("lines", 50)
        if not device:
            return "Error: device required (MAC, IP or name)"
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
        search_terms = {device}
        for h in hosts:
            name_val = h.get("name", "").lower()
            mac = h.get("mac", "").lower()
            ip = h.get("ip", "").lower()
            hostname = h.get("hostname", "").lower()
            if device in (mac, ip, name_val, hostname):
                search_terms.update([mac, ip, name_val, hostname])
        search_terms = {t for t in search_terms if t}
        log_result = rci({"show": {"log": {}}}, timeout=30)
        log_dict = log_result.get("show", {}).get("log", {}).get("log", {})
        if not log_dict:
            log_dict = log_result.get("show", {}).get("log", {})
        entries = []
        for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            entry = log_dict[k]
            line = _format_log_line(entry)
            if any(t in line.lower() for t in search_terms if t):
                entries.append(line)
        if not entries:
            return f"No log entries found for device: {device}"
        return "\n".join(entries[-lines:])

    elif name == "get_wifi":
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
                "busy_channels": iface.get("busy-channels", [])
            })
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_wifi_stations":
        # Direct associations (controller only)
        assoc_result = rci({"show": {"associations": {}}})
        stations = assoc_result.get("show", {}).get("associations", {}).get("station", [])
        assoc_by_mac = {s.get("mac", "").lower(): s for s in stations}

        # All clients from hotspot (includes extender clients via mws-backhaul)
        hotspot_result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = hotspot_result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])

        output = []
        seen_macs = set()

        # Controller-connected stations (from associations)
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
                "security": s.get("security")
            })

        # Extender clients (mws-backhaul=True, active, wifi)
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
                "rxrate": None,  # not available via hotspot for extender clients
                "txbytes": h.get("txbytes"),
                "rxbytes": h.get("rxbytes"),
                "uptime": mws.get("uptime"),
                "security": mws.get("security")
            })

        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_traffic":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
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
                    "tx_bytes": h.get("txbytes", 0)
                }
                for h in top
            ]
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_internet_status":
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
                    "priority": iface.get("priority")
                })
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_site_survey":
        aps = []
        for master in ["WifiMaster0", "WifiMaster1"]:
            result = rci({"show": {"site-survey": {"name": master}}})
            cells = result.get("show", {}).get("site-survey", {}).get("ap_cell", [])
            for ap in cells:
                if not any(a.get("address") == ap.get("address") for a in aps):
                    aps.append(ap)
        output = []
        for ap in aps:
            output.append({
                "ssid": ap.get("essid"),
                "mac": ap.get("address"),
                "channel": ap.get("channel"),
                "rssi": ap.get("rssi"),
                "quality": ap.get("quality"),
                "encryption": ap.get("encryption"),
                "mode": ap.get("ieee")
            })
        output.sort(key=lambda x: x.get("rssi", -999), reverse=True)
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_channel_analysis":
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
            q = ap.get("quality", 0)
            channel_quality[ch] = channel_quality.get(ch, 0) + q
        channels_24 = [1, 6, 11]
        channels_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 132, 136, 140, 149, 153, 157, 161]

        def analyze(channels):
            result = []
            for ch in channels:
                count = channel_count.get(ch, 0)
                quality = channel_quality.get(ch, 0)
                result.append({"channel": ch, "networks": count, "total_quality": quality})
            result.sort(key=lambda x: (x["networks"], x["total_quality"]))
            return result

        output = {
            "2.4GHz": {
                "recommended": analyze(channels_24)[0]["channel"],
                "channels": analyze(channels_24)
            },
            "5GHz": {
                "recommended": analyze(channels_5)[0]["channel"] if any(ch in channel_count for ch in channels_5) else 36,
                "channels": [c for c in analyze(channels_5) if c["networks"] > 0 or c["channel"] in [36, 44, 149, 157]]
            },
            "all_detected": [{"channel": k, "networks": v} for k, v in sorted(channel_count.items())]
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_vpn_status":
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
                "uptime": iface.get("uptime")
            }
            if iface.get("type") == "Wireguard" and iface.get("wireguard"):
                wg = iface["wireguard"]
                peers = wg.get("peer", [])
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
                            "last_handshake": p.get("last-handshake")
                        }
                        for p in peers
                    ]
                }
            output.append(entry)
        if not output:
            return "No VPN interfaces found"
        return json.dumps(output, ensure_ascii=False, indent=2)

    elif name == "get_web_access":
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

    elif name == "run_ping":
        host = args.get("host", "").strip()
        if not host:
            return "Error: host required"
        count = min(int(args.get("count", 4)), 10)
        r = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout if r.stdout else r.stderr

    elif name == "register_client":
        mac = args.get("mac", "").lower().strip()
        name_val = args.get("name", "").strip()
        ip_val = args.get("ip", "").strip()
        if not mac or not name_val:
            return "Error: mac and name required"
        payload = {"mac": mac, "name": name_val, "registered": True}
        if ip_val:
            payload["ip"] = ip_val
        rci({"ip": {"hotspot": {"host": payload}}})
        return f"Device {mac} registered as '{name_val}'" + (f" with IP {ip_val}" if ip_val else "")

    elif name == "update_client":
        mac = args.get("mac", "").lower().strip()
        if not mac:
            return "Error: mac required"
        payload = {"mac": mac}
        if args.get("name"):
            payload["name"] = args["name"].strip()
        if args.get("ip"):
            payload["ip"] = args["ip"].strip()
        rci({"ip": {"hotspot": {"host": payload}}})
        return f"Device {mac} updated: " + json.dumps({k: v for k, v in payload.items() if k != "mac"})

    elif name == "block_client":
        mac = args.get("mac", "").lower().strip()
        if not mac:
            return "Error: mac address required"
        result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "deny"}}}})
        statuses = result.get("ip", {}).get("hotspot", {}).get("host", {}).get("status", [])
        if any(s.get("code") == "19007441" for s in statuses):
            rci({"ip": {"hotspot": {"host": {"mac": mac, "name": "Blocked Device", "registered": True}}}})
            result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "deny"}}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "unblock_client":
        mac = args.get("mac", "").lower().strip()
        if not mac:
            return "Error: mac address required"
        result = rci({"ip": {"hotspot": {"host": {"mac": mac, "access": "permit"}}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_mesh_nodes":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        hosts = result.get("show", {}).get("ip", {}).get("hotspot", {}).get("host", [])
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

    elif name == "reboot":
        rci({"system": {"reboot": {}}})
        return "Reboot command sent"

    elif name == "backup_config":
        if not BACKUP_ENABLED:
            return "Backup is disabled. Set BACKUP_ENABLED=true in .env to enable."
        threading.Thread(target=do_backup, daemon=True).start()
        dest = f"{BACKUP_RSYNC_USER}@{BACKUP_RSYNC_HOST}:{BACKUP_RSYNC_PATH}" if BACKUP_RSYNC_HOST else BACKUP_PATH
        return f"Backup started. Config will be saved to: {dest}"

    return f"Unknown tool: {name}"


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
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION}
            }
            self.wfile.write(json.dumps(caps).encode())
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
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION}
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


if __name__ == "__main__":
    load_env()
    auth()

    if BACKUP_ENABLED:
        syslog(f"INFO: backup enabled, schedule='{BACKUP_SCHEDULE}'")
        t = threading.Thread(target=backup_scheduler, daemon=True)
        t.start()
    else:
        syslog("INFO: backup disabled (BACKUP_ENABLED=false)")

    print(f"Starting Keenetic MCP v{VERSION} on port {PORT}")
    server = http.server.HTTPServer(("0.0.0.0", PORT), MCPHandler)
    server.serve_forever()