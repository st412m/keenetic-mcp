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

from backup import fetch_running_config, syslog
from core import rci


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
