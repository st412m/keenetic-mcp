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
from urllib.parse import urlsplit, parse_qs, unquote

import core
from core import load_env, auth, VERSION
from backup import syslog, backup_scheduler
from tools_system import dump_log_to_nas, tool_reboot
from registry import TOOLS, call_tool
import http_tools
import watcher


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
                "protocolVersion": "2024-11-05",
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

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        method = body.get("method", "")
        req_id = body.get("id")
        response = {"jsonrpc": "2.0", "id": req_id}

        if method == "initialize":
            response["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
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
