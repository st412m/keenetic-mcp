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

import core
from core import load_env, auth, VERSION
from backup import syslog, backup_scheduler
from tools_system import dump_log_to_nas, tool_reboot
from registry import TOOLS, call_tool


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == f"/{core.SECRET}":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            caps = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": VERSION},
            }
            self.wfile.write(json.dumps(caps).encode())
        elif self.path == f"/{core.SECRET}/reboot":
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

    print(f"Starting Keenetic MCP v{VERSION} on port {core.PORT}")
    server = http.server.HTTPServer(("0.0.0.0", core.PORT), MCPHandler)
    server.serve_forever()
