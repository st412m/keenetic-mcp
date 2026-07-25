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

from core import _rci_node, rci
from helpers import _format_log_line, _get_ap, _get_extender_hosts, _get_hotspot_hosts, _get_node, _log_time_window, _parse_log_dict


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
