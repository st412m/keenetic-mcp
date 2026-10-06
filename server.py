import json
import http.server
import threading
from urllib.parse import urlsplit, parse_qs, unquote

import core
from core import load_env, auth, VERSION
from backup import syslog, backup_scheduler
from tools_system import dump_log_to_nas, tool_reboot
from registry import TOOLS, call_tool
import http_tools
import watcher


# MCP protocol revisions this server speaks, newest first. Sessions, SSE,
# structuredContent and outputSchema are not implemented.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


def negotiate_protocol_version(requested):
    """initialize: answer with the client's version if it is supported,
    otherwise with the latest one this server supports."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def check_protocol_header(value):
    """MCP-Protocol-Version on a POST other than initialize. Returns None when
    the request may proceed, or the error message for an unsupported value.
    A missing header is accepted: the transport says to assume 2025-03-26."""
    if value is None:
        return None
    value = value.strip()
    if value in SUPPORTED_PROTOCOL_VERSIONS:
        return None
    return "Unsupported MCP-Protocol-Version: %s (supported: %s)" % (
        value, ", ".join(SUPPORTED_PROTOCOL_VERSIONS))


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_post(raw_body, protocol_header=None):
    """One POST to /<SECRET>, without the socket. raw_body is the request body
    as bytes, or None when Content-Length was unusable. Returns
    (http_status, payload): payload is the JSON-RPC response as a dict, or
    None for a notification, which gets 202 with no body.

    The checks run in a fixed order: body parse, protocol header, notification,
    params shape, then the method itself."""
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError, RecursionError):
        return 400, _rpc_error(None, -32700, "Parse error: body is not valid JSON")
    if not isinstance(body, dict):
        return 400, _rpc_error(None, -32600, "Invalid Request: expected a single "
                                             "JSON-RPC object (batches are not supported)")

    method = body.get("method", "")
    req_id = body.get("id")

    if method != "initialize":
        header_error = check_protocol_header(protocol_header)
        if header_error:
            return 400, _rpc_error(req_id, -32600, header_error)

    if "id" not in body:
        return 202, None

    params = body.get("params", {})
    if not isinstance(params, dict):
        return 200, _rpc_error(req_id, -32602, "Invalid params: params must be an object")

    response = {"jsonrpc": "2.0", "id": req_id}

    if method == "initialize":
        response["result"] = {
            "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
        }
    elif method == "tools/list":
        response["result"] = {"tools": [
            {"name": k, "title": v["title"], "description": v["description"],
             "inputSchema": v["inputSchema"], "annotations": v["annotations"]}
            for k, v in TOOLS.items()
        ]}
    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        try:
            result = call_tool(tool_name, tool_args)
            response["result"] = {"content": [{"type": "text", "text": result}]}
        except Exception as e:
            response["error"] = {"code": -32000, "message": str(e)}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}

    return 200, response


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code, text):
        body = str(text).encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query, keep_blank_values=True)
        prefix = f"/{core.SECRET}"

        if path == prefix:
            self._send_json(200, {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
            })
        elif path == f"{prefix}/tools":
            self._send_json(200, {
                "ok": True,
                "count": len(http_tools.allowed_tools()),
                "tools": http_tools.describe_tools(),
            })
        elif path.startswith(f"{prefix}/tool/"):
            name = unquote(path[len(prefix) + len("/tool/"):]).strip("/")
            code, payload = http_tools.run(name, query)
            if code != 200:
                syslog(f"WARNING: HTTP tool '{name}' -> {code}: {payload.get('error')}")
            # ?raw=1 returns the tool's own text, for command_line sensors and
            # anything that would rather not unwrap JSON.
            if query.get("raw") and code == 200:
                self._send_text(200, payload["result"])
                return
            if code == 200:
                # Most tools return a JSON document as text. Parse it so HA
                # templates can index into it instead of parsing twice.
                try:
                    payload["result"] = json.loads(payload["result"])
                except (TypeError, ValueError):
                    pass
            self._send_json(code, payload)
        elif path == f"{prefix}/reboot":
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
        if not self.path.startswith(f"/{core.SECRET}"):
            self.send_response(403)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        # A negative length would make read() wait for the client to hang up.
        raw_body = self.rfile.read(length) if length >= 0 else None
        code, response = handle_post(raw_body, self.headers.get("MCP-Protocol-Version"))

        if response is None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


if __name__ == "__main__":
    load_env()
    auth()

    if core.BACKUP_ENABLED:
        syslog(f"INFO: backup enabled, schedule='{core.BACKUP_SCHEDULE}'")
        threading.Thread(target=backup_scheduler, daemon=True).start()
    else:
        syslog("INFO: backup disabled (BACKUP_ENABLED=false)")

    if core.HTTP_TOOLS_ENABLED:
        extra = sorted(core.HTTP_TOOL_ALLOWLIST & http_tools.MUTATING_TOOLS)
        syslog("INFO: HTTP tool route enabled, %d tools servable%s"
               % (len(http_tools.allowed_tools()),
                  ", mutating allowed: " + ", ".join(extra) if extra else ""))
    else:
        syslog("INFO: HTTP tool route disabled (MCP_HTTP_TOOLS=false)")

    try:
        syslog("INFO: " + watcher.start())
    except Exception as e:
        syslog(f"ERROR: watcher failed to start: {e}")

    print(f"Starting Keenetic MCP v{VERSION} on port {core.PORT}")
    server = http.server.HTTPServer(("0.0.0.0", core.PORT), MCPHandler)
    server.serve_forever()
