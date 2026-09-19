import json
import urllib.error
import re
from datetime import datetime, timedelta, timezone

import core
from core import _ci_get, _rci_get, rci
from helpers import _config_blocks, _config_lines, _get_hotspot_hosts, _intent_error, _mask_secrets, _rci_errors, _rci_statuses, _running_config_lines, _save_config


RCI_GET_BLACKLIST = ("running-config", "crypto", "ppp", "user")
RCI_MAX_CHARS = 40000


_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*"
    r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$", re.I)

# KeeneticOS caps static `ip host` bindings at 64 (command reference).
DNS_HOST_LIMIT = 64


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
    if port_i in core.PROTECTED_PORTS:
        raise GuardError(
            "port %s is protected (%s): it serves keenetic-mcp or another MCP "
            "endpoint. Change it by hand in the web UI if you really mean to."
            % (port_i, what))
    return str(port_i)


def _guard_proxy_name(name):
    name = str(name or "").strip()
    if not name:
        raise GuardError("proxy name is required")
    if name.lower() in core.PROTECTED_PROXY_NAMES:
        raise GuardError(
            "KeenDNS mapping '%s' is protected - removing or repointing it would "
            "cut the channel this server is reached through. Web UI only." % name)
    return name


def _guard_upstream(host, port):
    if (str(host), int(port)) in core.PROTECTED_UPSTREAMS:
        raise GuardError("upstream %s:%s is protected" % (host, port))


def _entries(tree):
    """Normalize an _rci_tree() result to a list of dicts - a lone surviving
    record often comes back unwrapped (see _dns_hosts)."""
    if isinstance(tree, list):
        return [e for e in tree if isinstance(e, dict)]
    if isinstance(tree, dict):
        return [tree]
    return []


def _commit(payload, verify_path, dry_run, summary,
            intent_field, intent_expected, intent_probe):
    """Shared tail for every mutating tool.

    intent_probe(after) returns the actual value of whatever the write was
    supposed to produce (or None if it is not in the tree at all);
    intent_expected is what it should be once the write has taken - None for
    a removal. RCI answering a write with no error and no effect (B2) is a
    silent no-op, not success: matches_intent catches it where `changed`
    alone cannot, because `changed: false` on a no-op write and `changed:
    false` on a write of the value that was already there are the same
    answer otherwise.

    A transport exception (timeout, dropped connection while reading the
    response) is the case most likely to mean "the router applied it, we
    just didn't find out" (B6) - so it gets the same after/changed/
    matches_intent/save treatment as a normal reply, not a bare string.
    """
    if dry_run:
        return json.dumps({
            "dry_run": True,
            "summary": summary,
            "would_send": payload,
            "note": "nothing was changed. Re-run with dry_run=false to apply.",
        }, ensure_ascii=False, indent=2)

    before = _rci_tree(verify_path)

    def _finish(errors, statuses, transport_error):
        after = _rci_tree(verify_path)
        changed = before != after
        actual = intent_probe(after)
        matches_intent = actual == intent_expected
        intent_err = None
        if not errors and not matches_intent and not transport_error:
            intent_err = _intent_error(intent_field, intent_expected, actual)
        save_attempted = (not errors and matches_intent) or changed
        save_detail = _save_config() if save_attempted else None
        return json.dumps({
            "dry_run": False,
            "summary": summary,
            "sent": payload,
            "statuses": statuses,
            "errors": errors,
            "intent_error": intent_err,
            "transport_error": transport_error,
            "before": before,
            "after": after,
            "changed": changed,
            "matches_intent": matches_intent,
            "save_attempted": save_attempted,
            "config_saved": bool(save_detail and save_detail["ok"]),
            "save_verified": bool(save_detail and save_detail["verified"]),
            "save_detail": save_detail,
        }, ensure_ascii=False, indent=2)

    try:
        result = rci(payload)
    except Exception as e:
        # No RCI response came back, so there is nothing to blame on the
        # router (statuses stays empty) - the exception is ours to report,
        # kept out of `errors` for the same reason as intent_error (B2).
        return _finish([], [], str(e))
    return _finish(_rci_errors(result), _rci_statuses(result), None)


def _dns_hosts():
    """Current `ip host` records. The tree is a list of {"domain","address"};
    a single record can come back unwrapped, hence the isinstance dance."""
    entries = _rci_tree("ip/host")
    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in (entries or []) if isinstance(e, dict)]


def _norm_domain(value):
    d = str(value or "").strip().rstrip(".").lower()
    if not d:
        raise GuardError("domain is required")
    if not _DOMAIN_RE.match(d):
        raise GuardError("'%s' is not a valid domain name" % value)
    return d


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


def tool_get_config_state(args):
    """Parsed show/last-change: when the config was last touched, by what
    agent and user, and the checksum - the only reliable signal that a save
    has actually landed on disk (see _save_config in helpers.py, and
    diff_saved_config for a second, independent way to answer the same
    question).

    The raw 'fail-safe' block is included as-is, but its 'unsaved' field is
    NOT a save indicator: it can read false while a save is still in flight.
    Do not branch on it - compare 'checksum' against a previous reading
    instead.
    """
    try:
        envelope = rci({"show": {"last-change": {}}})
        if not isinstance(envelope, dict):
            raise ValueError("unexpected response type %s" % type(envelope).__name__)
        data = envelope.get("show", {}).get("last-change", {}) or {}
    except Exception as e:
        return "Error reading show/last-change: %s" % e

    date_str = data.get("date")
    date_out = {"utc": None, "msk": None}
    if date_str:
        try:
            dt_utc = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT")
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        except ValueError:
            date_out["utc"] = date_str
        else:
            date_out["utc"] = dt_utc.strftime("%Y-%m-%d %H:%M:%SZ")
            date_out["msk"] = (dt_utc + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S MSK")

    return json.dumps({
        "date": date_out,
        "agent": data.get("agent"),
        "user": data.get("user"),
        "checksum": data.get("checksum"),
        "fail-safe": data.get("fail-safe"),
    }, ensure_ascii=False, indent=2)


def tool_diff_saved_config(args):
    """Diff running-config against startup-config, both fetched as CLI text
    from the /ci/ endpoints (outside /rci/ entirely) - a direct, checksum-
    independent answer to "what is not saved yet": a non-empty
    only_in_running means the running config has lines startup-config does
    not have (unsaved change); only_in_startup is the mirror case (rare -
    usually means something reverted since the last save).

    Not wired into _commit: two ~12 KB text fetches per write is too much
    for a single-threaded server on every call, so this is opt-in. Secrets
    are masked on BOTH sides before comparing - comparing a masked line
    against an unmasked one would report every secret-bearing line as
    changed, saved or not.
    """
    try:
        running = _ci_get("running-config.txt")
        startup = _ci_get("startup-config.txt")
    except Exception as e:
        return "Error reading /ci/running-config.txt or /ci/startup-config.txt: %s" % e
    running_lines = _mask_secrets(running.splitlines())
    startup_lines = _mask_secrets(startup.splitlines())
    startup_set = set(startup_lines)
    running_set = set(running_lines)
    return json.dumps({
        "only_in_running": [l for l in running_lines if l not in startup_set],
        "only_in_startup": [l for l in startup_lines if l not in running_set],
    }, ensure_ascii=False, indent=2)


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

    def _probe(after):
        for r in _entries(after):
            if r.get("protocol") == proto and r.get("port") == port:
                return r.get("to-host")
        return None
    return _commit({"ip": {"static": rule}}, "ip/static", args.get("dry_run", True),
                   "forward %s/%s on %s -> %s" % (proto, port, rule["interface"], to_host),
                   "to_host", to_host, _probe)


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

    def _probe(after):
        for r in _entries(after):
            if r.get("index") == rule.get("index"):
                return r.get("index")
        return None
    return _commit({"ip": {"static": payload_rule}}, "ip/static",
                   args.get("dry_run", True),
                   "remove rule %s (%s/%s -> %s)" % (
                       rule.get("index"), rule.get("protocol"),
                       rule.get("port"), rule.get("to-host")),
                   "index", None, _probe)


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

    def _probe(after):
        mapping = after.get(name) if isinstance(after, dict) else None
        if not isinstance(mapping, dict):
            return None
        up = mapping.get("upstream")
        if not isinstance(up, dict):
            return None
        p, h, pt = up.get("proto"), up.get("upstream"), up.get("port")
        if p is None or h is None or pt is None:
            return None
        return "%s://%s:%s" % (p, h, pt)
    return _commit({"ip": {"http": {"proxy": {name: entry}}}}, "ip/http/proxy",
                   args.get("dry_run", True),
                   "KeenDNS '%s' -> %s://%s:%s (%s)" % (name, proto, host, port, level),
                   "upstream", "%s://%s:%s" % (proto, host, port), _probe)


def tool_remove_keendns_mapping(args):
    try:
        name = _guard_proxy_name(args.get("name"))
    except GuardError as e:
        return "Refused: %s" % e
    existing = _rci_tree("ip/http/proxy")
    if isinstance(existing, dict) and name not in existing:
        return "No KeenDNS mapping named '%s'. Existing: %s" % (
            name, ", ".join(sorted(existing.keys())))

    def _probe(after):
        return name if isinstance(after, dict) and name in after else None
    return _commit({"ip": {"http": {"proxy": {name: {"no": True}}}}},
                   "ip/http/proxy", args.get("dry_run", True),
                   "remove KeenDNS mapping '%s'" % name,
                   "name", None, _probe)


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

    def _probe(after):
        for e in _entries(after):
            if e.get("mac") == mac:
                return e.get("ip")
        return None
    return _commit({"ip": {"dhcp": {"host": {"mac": mac, "ip": ip}}}},
                   "ip/dhcp/host", args.get("dry_run", True),
                   "reserve %s for %s" % (ip, mac),
                   "ip", ip, _probe)


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

    def _probe(after):
        for e in _entries(after):
            if e.get("mac") == mac:
                return e.get("mac")
        return None
    return _commit({"ip": {"dhcp": {"host": {"mac": mac, "no": True}}}},
                   "ip/dhcp/host", args.get("dry_run", True),
                   "remove reservation %s (%s)" % (match.get("ip"), mac),
                   "mac", None, _probe)



def tool_set_dns_host(args):
    """`ip host <domain> <address>` - a static record served by the router's own
    DNS proxy. This is the LAN half of split-horizon: public DNS points the name
    at the WAN address, this record points clients inside the network straight at
    the reverse proxy, so a new site behind Caddy needs one of these every time.

    `ip host` is a multiple-input command: writing a second address for a name
    that already has one ADDS it and the proxy then round-robins the name. That
    is almost never what someone repointing a site wants, so a conflicting name
    is refused and has to be removed first - the same shape as set_dhcp_host
    refusing an IP already reserved for another MAC.
    """
    try:
        domain = _norm_domain(args.get("domain"))
        address = str(args.get("address", "")).strip()
        if not _IP_RE.match(address):
            raise GuardError("'%s' is not an IPv4 address" % args.get("address"))
        hosts = _dns_hosts()
        same_name = [h for h in hosts
                     if str(h.get("domain", "")).strip().rstrip(".").lower() == domain]
        if same_name and not any(h.get("address") == address for h in same_name):
            raise GuardError(
                "%s already resolves to %s. `ip host` allows several addresses per "
                "name, so writing another one would load-balance the name instead "
                "of moving it - remove the old record first: "
                "remove_dns_host domain='%s'"
                % (domain, ", ".join(str(h.get("address")) for h in same_name), domain))
        if not same_name and len(hosts) >= DNS_HOST_LIMIT:
            raise GuardError(
                "the router already holds %d static `ip host` records and the "
                "documented limit is %d" % (len(hosts), DNS_HOST_LIMIT))
    except GuardError as e:
        return "Refused: %s" % e

    def _probe(after):
        # A name can hold several addresses (round-robin) - search all of
        # them, not just the first entry for this domain.
        for e in _entries(after):
            if (str(e.get("domain", "")).strip().rstrip(".").lower() == domain
                    and e.get("address") == address):
                return e.get("address")
        return None
    return _commit({"ip": {"host": {"domain": domain, "address": address}}},
                   "ip/host", args.get("dry_run", True),
                   "static DNS record %s -> %s" % (domain, address),
                   "address", address, _probe)


def tool_remove_dns_host(args):
    """`no ip host <domain> <address>`. The address is not optional in the
    router's command - the Keenetic command reference spells the removal form as
    `no ip host domain address` - so it is filled in from the tree rather than
    left out of the payload. A name holding several addresses is refused until
    the caller says which one."""
    try:
        domain = _norm_domain(args.get("domain"))
    except GuardError as e:
        return "Refused: %s" % e
    address = str(args.get("address", "") or "").strip()
    matches = [h for h in _dns_hosts()
               if str(h.get("domain", "")).strip().rstrip(".").lower() == domain]
    if address:
        matches = [h for h in matches if h.get("address") == address]
    if not matches:
        return "No static DNS record for %s%s" % (
            domain, (" -> %s" % address) if address else "")
    if len(matches) > 1:
        return ("%d records for %s (%s) - name the address to pick one"
                % (len(matches), domain,
                   ", ".join(str(h.get("address")) for h in matches)))
    rec = matches[0]

    def _probe(after):
        for e in _entries(after):
            if (str(e.get("domain", "")).strip().rstrip(".").lower() == domain
                    and e.get("address") == rec.get("address")):
                return e.get("address")
        return None
    return _commit({"ip": {"host": {"domain": rec.get("domain"),
                                    "address": rec.get("address"),
                                    "no": True}}},
                   "ip/host", args.get("dry_run", True),
                   "remove static DNS record %s -> %s"
                   % (rec.get("domain"), rec.get("address")),
                   "address", None, _probe)
