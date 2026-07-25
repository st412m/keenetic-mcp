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
VERSION = "2.4.0"

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


def load_env():
    global HOST, USER, PASS, SECRET, PORT
    global BACKUP_ENABLED, BACKUP_SCHEDULE, BACKUP_PATH, BACKUP_KEEP
    global BACKUP_RSYNC_HOST, BACKUP_RSYNC_USER, BACKUP_RSYNC_KEY, BACKUP_RSYNC_PATH

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


def rci(commands, timeout=10):
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
