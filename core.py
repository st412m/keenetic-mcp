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

HOST = "http://192.168.1.1"
USER = "admin"
PASS = "password"
SECRET = "changeme"
PORT = 9584
VERSION = "2.7.0"

# Backup config
BACKUP_ENABLED = False
BACKUP_SCHEDULE = "0 11 * * 0"
BACKUP_PATH = "/tmp/keenetic-backup"
BACKUP_KEEP = 0
BACKUP_RSYNC_HOST = ""
BACKUP_RSYNC_USER = ""
BACKUP_RSYNC_KEY = ""
BACKUP_RSYNC_PATH = ""

session_cookie = None

# One RCI conversation at a time. Until 2.7.0 there was effectively a single
# caller and the shared session_cookie was safe by accident; the watcher thread
# now polls the router while the HTTP server may be serving a request. Two
# threads re-authenticating at once would overwrite each other's cookie and one
# of them would get a 401 it did not deserve. RLock, because rci() calls auth().
rci_lock = threading.RLock()

# Objects the write tools must never touch. Populated in load_env() from the
# MCP_PROTECTED_* env vars, plus automatic self-protection (own port /
# upstream / proxy name) so a config mistake can never sever this server's
# own channel.
PROTECTED_PORTS = set()
PROTECTED_PROXY_NAMES = set()
PROTECTED_UPSTREAMS = set()

# Plain-HTTP tool route: GET /<SECRET>/tool/<name>?arg=value
# Lets Home Assistant (rest_command / rest sensors / command_line) call tools
# without speaking MCP. Read-only by default: tools that change router state
# are refused unless named explicitly in MCP_HTTP_TOOL_ALLOWLIST.
HTTP_TOOLS_ENABLED = True
HTTP_TOOL_ALLOWLIST = set()

# Watcher: a background thread polls the router locally and makes an outbound
# HTTP call when a rule matches. Rules live in a JSON file on the router; see
# watcher.py. Disabled in effect when the rules file is absent.
WATCH_ENABLED = True
WATCH_RULES = "watch_rules.json"
WATCH_STATE = "/tmp/keenetic-mcp-watch.json"
WATCH_INTERVAL = 10


def _csv(value):
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def load_env():
    global HOST, USER, PASS, SECRET, PORT
    global BACKUP_ENABLED, BACKUP_SCHEDULE, BACKUP_PATH, BACKUP_KEEP
    global BACKUP_RSYNC_HOST, BACKUP_RSYNC_USER, BACKUP_RSYNC_KEY, BACKUP_RSYNC_PATH
    global PROTECTED_PORTS, PROTECTED_PROXY_NAMES, PROTECTED_UPSTREAMS
    global HTTP_TOOLS_ENABLED, HTTP_TOOL_ALLOWLIST
    global WATCH_ENABLED, WATCH_RULES, WATCH_STATE, WATCH_INTERVAL

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

    BACKUP_ENABLED = os.environ.get("BACKUP_ENABLED", "false").lower() == "true"
    BACKUP_SCHEDULE = os.environ.get("BACKUP_SCHEDULE", BACKUP_SCHEDULE)
    BACKUP_PATH = os.environ.get("BACKUP_PATH", BACKUP_PATH)
    BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", str(BACKUP_KEEP)))
    BACKUP_RSYNC_HOST = os.environ.get("BACKUP_RSYNC_HOST", "")
    BACKUP_RSYNC_USER = os.environ.get("BACKUP_RSYNC_USER", "")
    BACKUP_RSYNC_KEY = os.environ.get("BACKUP_RSYNC_KEY", "")
    BACKUP_RSYNC_PATH = os.environ.get("BACKUP_RSYNC_PATH", "")

    # Protected objects for the write tools. The MCP's own port, its
    # 127.0.0.1:<port> upstream and the 'keenetic-mcp' proxy name are always
    # protected; everything else is opt-in via MCP_PROTECTED_*.
    PROTECTED_PORTS = {PORT}
    for p in _csv(os.environ.get("MCP_PROTECTED_PORTS", "")):
        try:
            PROTECTED_PORTS.add(int(p))
        except ValueError:
            pass

    PROTECTED_PROXY_NAMES = {"keenetic-mcp"}
    PROTECTED_PROXY_NAMES.update(
        n.lower() for n in _csv(os.environ.get("MCP_PROTECTED_PROXY_NAMES", "")))

    PROTECTED_UPSTREAMS = {("127.0.0.1", PORT)}
    for u in _csv(os.environ.get("MCP_PROTECTED_UPSTREAMS", "")):
        host, sep, port = u.rpartition(":")
        if sep:
            try:
                PROTECTED_UPSTREAMS.add((host, int(port)))
            except ValueError:
                pass

    HTTP_TOOLS_ENABLED = os.environ.get("MCP_HTTP_TOOLS", "true").lower() == "true"
    # Named here, a tool is served over plain HTTP even if it mutates state.
    # Empty (the default) means: every read-only tool, no mutating ones.
    HTTP_TOOL_ALLOWLIST = set(_csv(os.environ.get("MCP_HTTP_TOOL_ALLOWLIST", "")))

    WATCH_ENABLED = os.environ.get("MCP_WATCH", "true").lower() == "true"
    WATCH_RULES = os.environ.get("MCP_WATCH_RULES", WATCH_RULES)
    WATCH_STATE = os.environ.get("MCP_WATCH_STATE", WATCH_STATE)
    try:
        WATCH_INTERVAL = max(2, int(os.environ.get("MCP_WATCH_INTERVAL", str(WATCH_INTERVAL))))
    except ValueError:
        pass


def auth():
    with rci_lock:
        return _auth_locked()


def _auth_locked():
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


def rci(commands, timeout=10):
    with rci_lock:
        return _rci_locked(commands, timeout)


def _rci_locked(commands, timeout=10):
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
        return urllib.request.urlopen(req, timeout=timeout)

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


def _auth_node(ip):
    """Authenticate on a mesh extender node and return session cookie."""
    req = urllib.request.Request(f"http://{ip}/auth")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            realm = e.headers.get("X-NDM-Realm", "")
            challenge = e.headers.get("X-NDM-Challenge", "")
            cookie = e.headers.get("Set-Cookie", "").split(";")[0]
            md5_pass = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
            sha256_hash = hashlib.sha256(f"{challenge}{md5_pass}".encode()).hexdigest()
            payload = json.dumps({"login": USER, "password": sha256_hash}).encode()
            req2 = urllib.request.Request(
                f"http://{ip}/auth",
                data=payload,
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST"
            )
            resp = urllib.request.urlopen(req2)
            cookie2 = resp.headers.get("Set-Cookie", "").split(";")[0]
            return cookie2 or cookie
    return None


def _rci_node(ip, commands, timeout=15):
    """Execute RCI command on a specific mesh extender node."""
    cookie = _auth_node(ip)
    if not cookie:
        raise RuntimeError(f"Auth failed on {ip}")
    payload = json.dumps(commands).encode()
    req = urllib.request.Request(
        f"http://{ip}/rci/",
        data=payload,
        headers={"Content-Type": "application/json", "Cookie": cookie},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _rci_get(path, timeout=15):
    """GET /rci/<path>. Read-only by construction: there is no request body
    here, and writing to RCI requires a POST with one."""
    with rci_lock:
        return _rci_get_locked(path, timeout)


def _rci_get_locked(path, timeout=15):
    global session_cookie
    if not session_cookie:
        auth()

    def do_request():
        req = urllib.request.Request(
            "%s/rci/%s" % (HOST, path),
            headers={"Cookie": session_cookie or ""},
            method="GET",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        resp = do_request()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            session_cookie = None
            auth()
            resp = do_request()
        else:
            raise
    return resp.read().decode("utf-8", "replace")
