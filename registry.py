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

from tools_config import tool_get_config, tool_get_dhcp_static, tool_get_firewall_rules, tool_get_keendns_mappings, tool_get_port_forwarding, tool_rci_query, tool_remove_dhcp_host, tool_remove_keendns_mapping, tool_remove_port_forwarding, tool_set_dhcp_host, tool_set_keendns_mapping, tool_set_port_forwarding
from tools_network import tool_get_channel_analysis, tool_get_clients, tool_get_dhcp_leases, tool_get_extender_log, tool_get_interfaces, tool_get_internet_status, tool_get_log, tool_get_log_by_device, tool_get_mesh_nodes, tool_get_site_survey, tool_get_traffic, tool_get_unregistered_clients, tool_get_vpn_status, tool_get_web_access, tool_get_wifi, tool_get_wifi_stations, tool_get_dns_proxy
from tools_system import tool_backup_config, tool_backup_mcp_config, tool_block_client, tool_dump_log, tool_get_media, tool_get_opkg_status, tool_get_system_info, tool_list_backups, tool_reboot, tool_register_client, tool_run_ping, tool_unblock_client, tool_update_client, tool_get_schedule


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
    "backup_mcp_config": {
        "description": (
            "Back up the keenetic-mcp files that git cannot restore (.env, "
            "watch_rules.json, the Entware init script) to the NAS. Refreshes "
            "the mcp-config/ mirror every run and writes a dated snapshot only "
            "when the content changed. Runs synchronously and reports what "
            "happened, including a round-trip md5 check of the mirror. Staging "
            "is in /tmp (RAM): nothing is written to the USB stick."
        ),
        "inputSchema": {"type": "object", "properties": {
            "verify": {"type": "boolean", "description": "Read the mirror back and compare md5 (default true)"},
        }},
        "fn": tool_backup_mcp_config,
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
    "get_dns_proxy": {
        "description": "DNS proxy status: upstream resolvers (with DoT SNI) and static A/AAAA records from the router's DNS proxy.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_dns_proxy,
    },
    "get_schedule": {
        "description": "List router schedules (e.g. the firmware auto-update window) with name, weekday/time actions and seconds until the next fire.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_schedule,
    },
}

TOOLS.update(WRITE_TOOLS)


def call_tool(name, args):
    tool = TOOLS.get(name)
    if tool:
        return tool["fn"](args)
    return f"Unknown tool: {name}"
