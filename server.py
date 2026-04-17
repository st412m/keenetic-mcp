#!/usr/bin/env python3

import json
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import http.server
import os

HOST = "http://192.168.1.1"
USER = "admin"
PASS = "password"
SECRET = "changeme"
PORT = 9584

session_cookie = None

def load_env():
    global HOST, USER, PASS, SECRET, PORT
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
    HOST = os.environ.get("KEENETIC_HOST", HOST)
    USER = os.environ.get("KEENETIC_USER", USER)
    PASS = os.environ.get("KEENETIC_PASS", PASS)
    SECRET = os.environ.get("MCP_SECRET", SECRET)
    PORT = int(os.environ.get("MCP_PORT", str(PORT)))

def auth():
    global session_cookie
    req = urllib.request.Request(f"{HOST}/auth")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            realm = e.headers.get("X-NDM-Realm", "")
            challenge = e.headers.get("X-NDM-Challenge", "")
            cookie_header = e.headers.get("Set-Cookie", "")
            session_cookie = cookie_header.split(";")[0] if cookie_header else ""
            md5_pass = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
            sha256_hash = hashlib.sha256(f"{challenge}{md5_pass}".encode()).hexdigest()
            payload = json.dumps({"login": USER, "password": sha256_hash}).encode()
            req2 = urllib.request.Request(
                f"{HOST}/auth",
                data=payload,
                headers={"Content-Type": "application/json", "Cookie": session_cookie},
                method="POST"
            )
            resp = urllib.request.urlopen(req2)
            cookie2 = resp.headers.get("Set-Cookie", "")
            if cookie2:
                session_cookie = cookie2.split(";")[0]
            return True
    return False

def rci(commands):
    global session_cookie
    if not session_cookie:
        auth()
    payload = json.dumps(commands).encode()

    def do_request():
        req = urllib.request.Request(
            f"{HOST}/rci/",
            data=payload,
            headers={"Content-Type": "application/json", "Cookie": session_cookie or ""},
            method="POST"
        )
        return urllib.request.urlopen(req)

    try:
        resp = do_request()
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            session_cookie = None
            auth()
            resp = do_request()
            return json.loads(resp.read())
        raise

TOOLS = {
    "get_system_info": {
        "description": "Get router system info: version, uptime, CPU, memory",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_clients": {
        "description": "Get list of connected clients (devices) in the network",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_interfaces": {
        "description": "Get network interfaces status and traffic stats",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "get_log": {
        "description": "Get system log entries",
        "inputSchema": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "Number of lines (default 50)"},
            "filter": {"type": "string", "description": "Filter text to search in log lines"}
        }}
    },
    "get_wifi": {
        "description": "Get WiFi networks and connected stations",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "reboot": {
        "description": "Reboot the router",
        "inputSchema": {"type": "object", "properties": {}}
    },
}

def call_tool(name, args):
    if name == "get_system_info":
        result = rci({"show": {"version": {}, "system": {}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_clients":
        result = rci({"show": {"ip": {"hotspot": {}}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_interfaces":
        result = rci({"show": {"interface": {}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "get_log":
        lines = args.get("lines", 50)
        filter_text = args.get("filter", "")
        result = rci({"show": {"log": {}}})
        log_dict = result.get("show", {}).get("log", {}).get("log", {})
        if not log_dict:
            log_dict = result.get("show", {}).get("log", {})
        entries = []
        for k in sorted(log_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            entry = log_dict[k]
            if isinstance(entry, dict):
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    line = f"[{msg.get('label','?')}] {entry.get('source','')} {msg.get('message','')}"
                else:
                    line = str(entry)
                entries.append(line)
        if filter_text:
            entries = [l for l in entries if filter_text.lower() in l.lower()]
        return "\n".join(entries[-lines:])

    elif name == "get_wifi":
        result = rci({"show": {"wireless": {}}})
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif name == "reboot":
        rci({"system": {"reboot": {}}})
        return "Reboot command sent"

    return f"Unknown tool: {name}"


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == f"/{SECRET}":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            caps = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "keenetic-mcp", "version": "1.0.0"}
            }
            self.wfile.write(json.dumps(caps).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.path.startswith(f"/{SECRET}"):
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
                "serverInfo": {"name": "keenetic-mcp", "version": "1.0.0"}
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
    print(f"Starting Keenetic MCP on port {PORT}")
    print(f"Endpoint: http://0.0.0.0:{PORT}/{SECRET}")
    auth()
    server = http.server.HTTPServer(("0.0.0.0", PORT), MCPHandler)
    server.serve_forever()
