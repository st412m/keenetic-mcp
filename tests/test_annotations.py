"""Tool titles and MCP annotations: present, well-formed, and matching the
expected table below.

Run from the repository root: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_tools  # noqa: E402
import registry  # noqa: E402
import server  # noqa: E402
import watcher  # noqa: E402,F401  (import publishes the watcher's tools)


# name: (title, readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
R = (True, False, True, False)  # a tool that only reads
EXPECTED = {
    "get_system_info": ("Get system info",) + R,
    "get_clients": ("List clients",) + R,
    "get_unregistered_clients": ("List unregistered clients",) + R,
    "get_dhcp_leases": ("List DHCP leases",) + R,
    "get_interfaces": ("Get interfaces",) + R,
    "get_log": ("Read router log",) + R,
    "get_log_by_device": ("Read log by device",) + R,
    "get_wifi": ("Get Wi-Fi status",) + R,
    "get_wifi_stations": ("List Wi-Fi stations",) + R,
    "get_traffic": ("Get traffic summary",) + R,
    "get_internet_status": ("Get internet status",) + R,
    "get_site_survey": ("Scan nearby Wi-Fi networks",) + R,
    "get_channel_analysis": ("Analyze Wi-Fi channels",) + R,
    "get_vpn_status": ("Get VPN status",) + R,
    "get_web_access": ("List published web apps",) + R,
    "get_mesh_nodes": ("List mesh nodes",) + R,
    "get_extender_log": ("Read extender log",) + R,
    "rci_query": ("Query RCI tree",) + R,
    "get_config": ("Read running config",) + R,
    "get_config_state": ("Get config save state",) + R,
    "diff_saved_config": ("Diff unsaved config",) + R,
    "get_port_forwarding": ("List port forwarding",) + R,
    "get_firewall_rules": ("List firewall rules",) + R,
    "get_dhcp_static": ("List static DHCP reservations",) + R,
    "get_keendns_mappings": ("List KeenDNS mappings",) + R,
    "get_media": ("Get storage overview",) + R,
    "get_opkg_status": ("Get Entware status",) + R,
    "list_backups": ("List backups on NAS",) + R,
    "get_dns_proxy": ("Get DNS proxy status",) + R,
    "get_schedule": ("List schedules",) + R,
    "get_watch_status": ("Get watcher status",) + R,
    "run_ping": ("Ping host", True, False, True, True),

    "set_port_forwarding": ("Set port forwarding rule", False, True, False, False),
    "remove_port_forwarding": ("Remove port forwarding rule", False, True, True, False),
    "set_keendns_mapping": ("Set KeenDNS mapping", False, True, True, False),
    "remove_keendns_mapping": ("Remove KeenDNS mapping", False, True, True, False),
    "set_dhcp_host": ("Set static DHCP reservation", False, True, True, False),
    "remove_dhcp_host": ("Remove static DHCP reservation", False, True, True, False),
    "set_dns_host": ("Add static DNS record", False, False, True, False),
    "remove_dns_host": ("Remove static DNS record", False, True, True, False),
    "register_client": ("Register client", False, True, True, False),
    "update_client": ("Update client", False, True, True, False),
    "block_client": ("Block client", False, True, True, False),
    "unblock_client": ("Unblock client", False, True, True, False),
    "reboot": ("Reboot router", False, True, False, False),
    "backup_config": ("Back up router config", False, True, False, False),
    "backup_mcp_config": ("Back up MCP config files", False, True, True, False),
    "dump_log": ("Dump router log to NAS", False, False, False, False),
    "test_watch_rule": ("Test watcher rule", False, True, False, True),
}

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


class AnnotationTests(unittest.TestCase):

    def test_tool_count(self):
        self.assertEqual(len(registry.TOOLS), 49)
        self.assertEqual(len(EXPECTED), 49)

    def test_every_tool_is_well_formed(self):
        for name, spec in registry.TOOLS.items():
            with self.subTest(tool=name):
                title = spec.get("title")
                self.assertIsInstance(title, str)
                self.assertTrue(title.strip())
                ann = spec.get("annotations")
                self.assertIsInstance(ann, dict)
                self.assertEqual(ann.get("title"), title)
                self.assertEqual(set(ann), {"title"} | set(HINTS))
                for hint in HINTS:
                    self.assertIs(type(ann[hint]), bool, hint)

    def test_matches_expected_table(self):
        self.assertEqual(set(registry.TOOLS), set(EXPECTED))
        for name, spec in registry.TOOLS.items():
            with self.subTest(tool=name):
                ann = spec["annotations"]
                actual = (spec["title"],) + tuple(ann[h] for h in HINTS)
                self.assertEqual(actual, EXPECTED[name])

    def test_read_only_matches_mutating_set(self):
        not_read_only = {n for n, s in registry.TOOLS.items()
                         if not s["annotations"]["readOnlyHint"]}
        self.assertEqual(not_read_only, http_tools.MUTATING_TOOLS)

    def test_tools_list_carries_title_and_annotations(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        code, response = server.handle_post(body)
        self.assertEqual(code, 200)
        tools = response["result"]["tools"]
        self.assertEqual(len(tools), 49)
        for tool in tools:
            with self.subTest(tool=tool.get("name")):
                self.assertEqual(set(tool),
                                 {"name", "title", "description", "inputSchema", "annotations"})
                self.assertEqual(tool["title"], registry.TOOLS[tool["name"]]["title"])
                self.assertEqual(tool["annotations"], registry.TOOLS[tool["name"]]["annotations"])


if __name__ == "__main__":
    unittest.main()
