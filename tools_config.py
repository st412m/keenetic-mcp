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

from core import _rci_get, rci
from helpers import _config_blocks, _config_lines, _get_hotspot_hosts, _mask_secrets, _rci_errors, _rci_statuses, _running_config_lines, _save_config


RCI_GET_BLACKLIST = ("running-config", "crypto", "ppp", "user")
RCI_MAX_CHARS = 40000


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
