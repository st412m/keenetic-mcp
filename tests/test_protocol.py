"""MCP transport: protocol version negotiation, the MCP-Protocol-Version
header, notifications and malformed bodies. Nothing here opens a socket or
talks to a router.

Run from the repository root: python -m unittest discover -s tests -v
"""

import http.client
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import server  # noqa: E402


def rpc(method, req_id=1, **extra):
    """A JSON-RPC message as bytes. req_id=None leaves the id out entirely."""
    msg = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        msg["id"] = req_id
    msg.update(extra)
    return json.dumps(msg).encode()


def initialize(version=None):
    params = {"capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
    if version is not None:
        params["protocolVersion"] = version
    return rpc("initialize", params=params)


def run_handler(body, headers):
    """Drive MCPHandler.do_POST over in-memory streams instead of a socket."""
    h = server.MCPHandler.__new__(server.MCPHandler)
    head = "".join("%s: %s\r\n" % kv for kv in headers.items()) + "\r\n"
    h.headers = http.client.parse_headers(io.BytesIO(head.encode()))
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.path = "/" + core.SECRET
    h.command = "POST"
    h.request_version = "HTTP/1.0"
    h.requestline = "POST %s HTTP/1.0" % h.path
    h.client_address = ("test", 0)
    h.do_POST()
    raw_head, _, payload = h.wfile.getvalue().partition(b"\r\n\r\n")
    return int(raw_head.split(b" ")[1]), payload


class NegotiationTests(unittest.TestCase):

    def test_initialize_versions(self):
        cases = [
            ("2025-06-18", "2025-06-18"),
            ("2025-03-26", "2025-03-26"),
            ("2024-11-05", "2024-11-05"),
            ("2099-01-01", "2025-06-18"),
            (None, "2025-06-18"),
        ]
        for requested, expected in cases:
            with self.subTest(requested=requested):
                code, response = server.handle_post(initialize(requested))
                self.assertEqual(code, 200)
                self.assertEqual(response["result"]["protocolVersion"], expected)

    def test_negotiate_ignores_non_string(self):
        self.assertEqual(server.negotiate_protocol_version(20250618), "2025-06-18")


class HeaderTests(unittest.TestCase):

    def test_missing_header_accepted(self):
        self.assertIsNone(server.check_protocol_header(None))
        code, response = server.handle_post(rpc("tools/list"))
        self.assertEqual(code, 200)
        self.assertIn("result", response)

    def test_supported_versions_accepted(self):
        for version in ("2025-06-18", "2025-03-26", "2024-11-05"):
            with self.subTest(version=version):
                self.assertIsNone(server.check_protocol_header(version))
                code, response = server.handle_post(rpc("tools/list"), version)
                self.assertEqual(code, 200)
                self.assertIn("result", response)

    def test_unsupported_version_rejected(self):
        code, response = server.handle_post(rpc("tools/list", req_id=7), "1999-01-01")
        self.assertEqual(code, 400)
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], -32600)
        self.assertIn("1999-01-01", response["error"]["message"])

    def test_initialize_not_checked(self):
        code, response = server.handle_post(initialize("2025-06-18"), "1999-01-01")
        self.assertEqual(code, 200)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")


class BodyTests(unittest.TestCase):

    def assertRpcError(self, result, http_code, rpc_code, req_id=None):
        code, response = result
        self.assertEqual(code, http_code)
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], req_id)
        self.assertEqual(response["error"]["code"], rpc_code)

    def test_notification_gets_202(self):
        self.assertEqual(server.handle_post(rpc("notifications/initialized", req_id=None)),
                         (202, None))

    def test_not_json(self):
        for raw in (b"", b"{", None):
            with self.subTest(raw=raw):
                self.assertRpcError(server.handle_post(raw), 400, -32700)

    def test_not_an_object(self):
        batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]).encode()
        for raw in (b"null", b"[]", batch, b'"x"', b"42"):
            with self.subTest(raw=raw):
                self.assertRpcError(server.handle_post(raw), 400, -32600)

    def test_params_not_an_object(self):
        for method in ("tools/call", "initialize"):
            for params in (None, []):
                with self.subTest(method=method, params=params):
                    raw = rpc(method, req_id=3, params=params)
                    self.assertRpcError(server.handle_post(raw), 200, -32602, req_id=3)

    def test_notification_with_bad_params_gets_202(self):
        raw = rpc("notifications/initialized", req_id=None, params=None)
        self.assertEqual(server.handle_post(raw), (202, None))

    def test_order_parse_before_header(self):
        self.assertRpcError(server.handle_post(b"{", "1999-01-01"), 400, -32700)

    def test_order_header_before_notification(self):
        code, _ = server.handle_post(rpc("tools/list", req_id=None), "1999-01-01")
        self.assertEqual(code, 400)

    def test_tools_list_with_id(self):
        code, response = server.handle_post(rpc("tools/list", req_id=5))
        self.assertEqual(code, 200)
        self.assertEqual(response["id"], 5)
        self.assertEqual(len(response["result"]["tools"]), 49)

    def test_unknown_method(self):
        self.assertRpcError(server.handle_post(rpc("no/such/method", req_id=9)),
                            200, -32601, req_id=9)

    def test_null_id_is_a_request(self):
        code, response = server.handle_post(rpc("tools/list", req_id=None, id=None))
        self.assertEqual(code, 200)
        self.assertIsNone(response["id"])
        self.assertIn("result", response)


class HandlerTests(unittest.TestCase):

    def test_bad_content_length(self):
        for length in ("abc", "-1"):
            with self.subTest(length=length):
                code, payload = run_handler(b"{}", {"Content-Length": length})
                self.assertEqual(code, 400)
                self.assertEqual(json.loads(payload)["error"]["code"], -32700)

    def test_missing_content_length(self):
        code, payload = run_handler(b"", {})
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], -32700)

    def test_notification_has_empty_body(self):
        body = rpc("notifications/initialized", req_id=None)
        code, payload = run_handler(body, {"Content-Length": str(len(body))})
        self.assertEqual(code, 202)
        self.assertEqual(payload, b"")

    def test_request_round_trip(self):
        body = initialize("2024-11-05")
        code, payload = run_handler(body, {"Content-Length": str(len(body)),
                                           "MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(payload)["result"]["protocolVersion"], "2024-11-05")


if __name__ == "__main__":
    unittest.main()
