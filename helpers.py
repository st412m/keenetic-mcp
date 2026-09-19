import json
import re
import time
from datetime import datetime, timedelta

from backup import fetch_running_config, syslog
from core import rci, _rci_get


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


def _known_host_entry(mac):
    """One device's registration from /rci/known/host - a silent no-op there
    (unknown field, no error) is what actually bit register_client and
    update_client on 2026-07-02, so callers re-read this after writing rather
    than trusting the absence of an RCI error.

    The tree is keyed by NAME, not MAC - confirmed live 2026-09-19, against
    salatmaster/keenetic-mcp's docs/rci-api.md claiming this branch never
    returns a name at all. So the match here is by value, not by key.
    """
    try:
        tree = json.loads(_rci_get("known/host"))
    except Exception:
        return None
    if not isinstance(tree, dict):
        return None
    for name, v in tree.items():
        if isinstance(v, dict) and str(v.get("mac", "")).lower() == mac:
            return {"name": name, "mac": v.get("mac")}
    return None


def _dhcp_host_entry(mac):
    """One device's static DHCP reservation from /rci/ip/dhcp/host."""
    try:
        entries = json.loads(_rci_get("ip/dhcp/host"))
    except Exception:
        return None
    if isinstance(entries, dict):
        entries = [entries]
    for e in (entries or []):
        if isinstance(e, dict) and str(e.get("mac", "")).lower() == mac:
            return e
    return None


def _hotspot_access_entry(mac):
    """Access/policy/schedule slice of one device's hotspot host record - not
    the whole record, whose traffic counters and radio stats change on every
    poll regardless of what block/unblock actually wrote."""
    for h in _get_hotspot_hosts():
        if isinstance(h, dict) and str(h.get("mac", "")).lower() == mac:
            return {"access": h.get("access"), "policy": h.get("policy"),
                    "schedule": h.get("schedule")}
    return None


def _intent_error(field, requested, actual):
    """RCI answered a write with no error and the tree still shows the old
    value - the 2026-07-02 failure mode. Kept out of `errors` on purpose
    (B2): that list holds only what the router itself returned, in its own
    shape (status/code/ident/message); a synthetic entry of a different shape
    dropped in there would confuse anything that parses errors as RCI output.
    """
    return {
        "field": field,
        "requested": requested,
        "actual": actual,
        "message": "router accepted the %s write without an RCI error, but "
                    "the value did not change" % field,
    }


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


# Measured 2026-09-19 on live acceptance (KN-1010, KeeneticOS 5.1.5): a save
# takes 4-5s end to end - the router log shows 'saving (<agent>)' then
# 'configuration saved' 4s apart in nine of ten samples, 5s in one. The old
# 2000 left every one of the eight acceptance writes at "pending". 6000 was
# rejected too: on a single-threaded server where 'show log' alone can run
# 6-7s, a one-second margin invites a spurious "pending" from unrelated
# contention rather than from the save itself. The chosen value costs up to
# 7s blocked on a write call, which is acceptable - writes are rare, and
# this timeout never touches the read tools HA polls.
SAVE_VERIFY_TIMEOUT_MS = 7000
SAVE_VERIFY_INTERVAL_MS = 250


def _save_config():
    """Persist running-config so changes survive a reboot (web UI does this
    automatically; raw RCI writes do not).

    Returns a dict, not a bool. Measured 2026-09-19: the router answers the
    save call before the on-disk checksum catches up (lag 4-5s, narrowed from
    an earlier 1-8s estimate once the log gave exact start/end timestamps),
    so treating "no exception" as "saved" was wrong - config_saved:true meant
    only that the request went out. The fail-safe block's own flag cannot
    substitute for this: it read false in every sample taken right after a
    write, including the ones still mid-save. The only reliable signal is
    show/last-change.checksum changing. Polling blocks this single-threaded
    server, so the wait is capped at SAVE_VERIFY_TIMEOUT_MS; a save that has
    not confirmed by then comes back as status "pending" - applied, not yet
    verified on disk - not as a failure.
    """
    try:
        before = rci({"show": {"last-change": {}}}).get("show", {}).get("last-change", {}) or {}
    except Exception as e:
        syslog(f"WARNING: config save failed reading checksum before save: {e}")
        return {"ok": False, "verified": False, "status": "error",
                "checksum_before": None, "checksum_after": None,
                "agent": None, "waited_ms": 0, "errors": [str(e)]}
    checksum_before = before.get("checksum")

    try:
        result = rci({"system": {"configuration": {"save": {}}}})
    except Exception as e:
        syslog(f"WARNING: config save failed: {e}")
        return {"ok": False, "verified": False, "status": "error",
                "checksum_before": checksum_before, "checksum_after": None,
                "agent": None, "waited_ms": 0, "errors": [str(e)]}
    errors = _rci_errors(result)
    if errors:
        syslog(f"WARNING: config save returned errors: {errors}")
        return {"ok": False, "verified": False, "status": "error",
                "checksum_before": checksum_before, "checksum_after": None,
                "agent": None, "waited_ms": 0, "errors": errors}

    checksum_after = checksum_before
    agent = before.get("agent")
    verified = False
    waited_ms = 0
    while waited_ms < SAVE_VERIFY_TIMEOUT_MS:
        time.sleep(SAVE_VERIFY_INTERVAL_MS / 1000.0)
        waited_ms += SAVE_VERIFY_INTERVAL_MS
        try:
            after = rci({"show": {"last-change": {}}}).get("show", {}).get("last-change", {}) or {}
        except Exception:
            continue
        checksum_after = after.get("checksum")
        agent = after.get("agent")
        if checksum_after != checksum_before:
            verified = True
            break

    return {
        "ok": True,
        "verified": verified,
        "status": "confirmed" if verified else "pending",
        "checksum_before": checksum_before,
        "checksum_after": checksum_after,
        "agent": agent,
        "waited_ms": waited_ms,
        "errors": [],
    }


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


def _line_ts(line):
    """Timestamp of a formatted log line, or None if it carries none."""
    m = _LOG_TS_RE.search(line)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1))
    if not mon:
        return None
    try:
        return datetime(datetime.now().year, mon, int(m.group(2)),
                        int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None


def _log_time_window(entries, since=None, until=None):
    lo, hi = _parse_bound(since), _parse_bound(until)
    if not lo and not hi:
        return entries
    out = []
    last_ts = None
    for line in entries:
        ts = _line_ts(line)
        if ts is None:
            # A line without its own timestamp is a continuation of the one
            # before it. Inheriting that timestamp keeps it with its parent;
            # dropping it (the old behaviour) removed data from the window
            # without saying so.
            ts = last_ts
        else:
            last_ts = ts
        if ts is not None:
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
        out.append(line)
    return out


def router_boot_dt():
    """Best-effort boot time from 'show system uptime'. None if unavailable."""
    try:
        result = rci({"show": {"system": {}}})
        secs = int(result.get("show", {}).get("system", {}).get("uptime"))
    except Exception:
        return None
    if secs < 0:
        return None
    return datetime.fromtimestamp(time.time() - secs)


def pre_ntp_notice(entries):
    """Warn about entries stamped before this boot.

    The router logs from the moment it powers on, but its clock only becomes
    correct once NTP answers. Everything written in between carries whatever
    time was restored from flash — often days off. Those lines are real, their
    dates are not, and nothing in the raw output says so: a time-windowed
    query will happily place them in the wrong day. Returns a banner line, or
    None when the log is clean.
    """
    boot = router_boot_dt()
    if not boot:
        return None
    # A minute of slack: uptime and log timestamps are seconds-granular and
    # the very first lines are written as the clock is still being set.
    cutoff = boot - timedelta(minutes=1)
    stale = [ts for ts in (_line_ts(l) for l in entries) if ts and ts < cutoff]
    if not stale:
        return None
    return (
        "[!] %d of these entries are stamped BEFORE this boot (router came up "
        "%s), earliest %s. The clock was not NTP-synced yet, so their dates are "
        "wrong — they belong to this boot. Search the log for "
        "'Ntp::Client: time synchronized' to find where real time starts."
        % (len(stale), boot.strftime("%Y-%m-%d %H:%M:%S"),
           min(stale).strftime("%b %d %H:%M:%S"))
    )
