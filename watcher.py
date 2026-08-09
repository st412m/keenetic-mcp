"""Universal watcher: poll the router locally, call out over HTTP on a match.

Why it exists: without it, anything that wants to know about a router event has
to poll the router from the outside. That is slow (a 10-second event needs a
60-second poll to be noticed), and every poll costs a request against a
single-threaded server. Polling from inside the router reverses the direction:
the watcher notices, and pushes.

The watcher knows nothing about who it talks to. A rule carries a complete HTTP
call - method, URL, headers, body - and every receiver that speaks HTTP is
equal: a home automation webhook, ntfy, the Telegram Bot API, a log collector.

Rules file (JSON, path in MCP_WATCH_RULES, default watch_rules.json next to
this module):

    {
      "defaults": {
        "method": "POST",
        "url": "http://192.168.1.54:8123/api/webhook/router_event",
        "body": {"text": "${message}"},
        "cooldown": 60
      },
      "rules": [
        {
          "id": "web_login",
          "source": "log",
          "match": "Scgi::Auth::Handler: opened session for user \\"([^\\"]+)\\" from \\"([^\\"]+)\\"",
          "message": "Web login: ${m1} from ${m2}"
        },
        {
          "id": "unknown_device",
          "source": "rci",
          "path": "show/ip/hotspot",
          "key": "mac",
          "where": {"active": true, "registered": false},
          "message": "Unknown device ${mac} (${ip})"
        }
      ]
    }

Everything except id, source and the source's own selector is optional: a rule
inherits the rest from "defaults". Rules are re-read when the file's mtime
changes - no restart needed.

Substitution is string.Template.safe_substitute: ${name} is replaced when the
event carries that field and left alone when it does not. There is no jinja2 on
the router and there never will be, so nothing here needs one.

State (log position, previous RCI snapshots, cooldowns) lives in /tmp, i.e. in
RAM. Writing it to the USB stick is not an option - that is the one thing this
project does not do. The cost is that a router reboot resets the baseline:
after a reboot the current picture becomes "normal" and only changes from that
moment are reported. A restart of the server alone keeps its state.

Why the watcher has its own RCI session (2.7.1 - read before touching it)
------------------------------------------------------------------------
2.7.0 had the watcher call core.rci()/core._rci_get(), which serialise on
core.rci_lock. That lock exists for one reason: core keeps a single shared
session_cookie, and two threads re-authenticating at once would overwrite each
other's cookie. The lock was correct and the contention was brutal. 'show log'
takes six to seven seconds on a KN-1010 and the watcher polls it every ten, so
a lock-sharing watcher held core.rci_lock roughly two thirds of the time, and
every MCP call and plain-HTTP tool call that landed inside a poll waited it
out. Measured 2026-08-03: get_system_info answers in ~50 ms when the lock is
free and took 6-7 s on about half the attempts; get_log itself took 15-20 s.

The fix is not to poll less often and not to sneak past authentication. It is
to stop sharing the thing the lock protects: the watcher authenticates as its
own client, keeps its own cookie, and guards it with its own private lock that
nothing outside this module ever takes. The router is happy to hold several
concurrent RCI sessions - the web UI and the Home Assistant integration
already do exactly that - and 'show log' is I/O, so the GIL is released while
it runs and the HTTP server keeps answering throughout.

Two consequences worth knowing. The watcher authenticates over HTTP like any
other RCI client, so - once 'ip http log auth' is enabled, which it is not by
default - its login shows up in the router log as
'Core::Scgi::Auth::Handler: opened session for user "admin" from "<router's own
LAN address>"' at startup, and again whenever the session is renewed after a
401. That is the same line a human logging into the web configurator produces,
so a rule watching for logins will see the watcher too; use 'exclude' on the
router's own address if you only care about everyone else. It does not write
'Core::Authenticator', which is the telnet/SSH channel. And core.rci_lock is now
taken only by the server's own calls - it stays because the guarantee it gives
is still needed the moment a second caller appears in core.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from hashlib import md5
from string import Template

try:
    import ssl
except ImportError:  # python built without ssl - https rules will say so
    ssl = None

import core
from backup import syslog
from helpers import _format_log_line, _parse_log_dict


# --- tunables ---------------------------------------------------------------

START_DELAY = 20          # let the socket bind and the first auth settle
LOG_FETCH_TIMEOUT = 20    # 'show log' is the heaviest call the watcher makes
DEFAULT_RCI_INTERVAL = 30
DEFAULT_COOLDOWN = 60     # seconds, per rule and per event key
DEFAULT_MAX_EVENTS = 5    # notifications per rule per poll, a flood stopper
DEFAULT_TIMEOUT = 10      # outbound HTTP
ANCHOR_LEN = 8            # log lines remembered to find our place again
MAX_NEW_LINES = 200       # per poll; anything beyond that is a burst, not news
VALUE_LIMIT = 300         # characters per substituted value

# Fields that change on their own and would make every diff look like a change.
VOLATILE = {
    "rxbytes", "txbytes", "uptime", "last_seen", "first_seen", "txrate",
    "rssi", "mcs", "expires", "dhcp_expires", "mws_uptime", "mws_rssi",
    "mws_txrate", "mws_mcs",
}

_MISSING = object()

_lock = threading.RLock()
_state = {"log": {}, "rci": {}, "cooldown": {}}
_stats = {
    "started": None,
    "rules_file": None,
    "rules_mtime": None,
    "rules_error": None,
    "rules": {},
    "last_cycle": None,
    "last_error": None,
}
_rules = []
_next_run = {}
_rules_mtime = None


# --- small helpers ----------------------------------------------------------

def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(text):
    return md5(text.encode("utf-8", "replace")).hexdigest()[:12]


def _safe_key(name):
    """Field name usable as a ${placeholder}: Template only accepts word chars."""
    return re.sub(r"\W", "_", str(name)).strip("_") or "field"


def _norm(value):
    if value is _MISSING or value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def _clip(text):
    text = str(text)
    return text if len(text) <= VALUE_LIMIT else text[:VALUE_LIMIT] + "..."


def state_path():
    return os.environ.get("MCP_WATCH_STATE", core.WATCH_STATE)


def rules_path():
    path = core.WATCH_RULES
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    return path


def _load_state():
    try:
        with open(state_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for section in ("log", "rci", "cooldown"):
                if isinstance(data.get(section), dict):
                    _state[section] = data[section]
            syslog("INFO: watcher state restored from %s" % state_path())
    except Exception:
        # No state is the normal case after a reboot: /tmp is RAM.
        pass


def _save_state():
    tmp = state_path() + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(_state, f)
        os.replace(tmp, state_path())
    except Exception as e:
        syslog("WARNING: watcher could not save state: %s" % e)


# --- secret masking ---------------------------------------------------------
#
# test_watch_rule renders a rule's outbound call so it can be inspected. What
# it renders contains, by design, everything needed to make that call - which
# for a Telegram rule means the bot token in the URL and the proxy password in
# the proxy URL. Until 2.7.1 both came back in clear text, and the tool is
# reachable over the plain-HTTP route as well, i.e. behind nothing but a path
# secret. The rendered view is for checking shape, not for reading credentials
# back out, so the credentials are replaced with *** on the way out.
#
# Two independent passes, because either alone leaves a hole. Values from the
# environment are masked by value: those are exactly the strings the rule
# loader substituted in, so they are caught wherever they ended up, including
# inside a body. Patterns catch what was written literally into the rules file
# and never passed through the environment at all.

SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASS|PASSWD|PASSWORD|KEY|AUTH|CRED|PROXY)", re.I)

SECRET_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token", "x-access-token",
}

MASK = "***"
MIN_SECRET_LEN = 6

MASK_PATTERNS = [
    # Telegram bot token in a path: /bot<digits>:<token>
    (re.compile(r"(/bot)\d{5,}:[A-Za-z0-9_\-]{10,}"), r"\1" + MASK),
    # user:password@host in any URL, including a proxy URL
    (re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+:[^/@\s]+@"),
     r"\1" + MASK + ":" + MASK + "@"),
    # token=..., "api_key": "...", password: ...
    (re.compile(r"((?:token|api[_-]?key|apikey|access[_-]?token|secret|"
                r"password|passwd|auth)[\"']?\s*[:=]\s*[\"']?)"
                r"([^\"'\s,&}]{%d,})" % MIN_SECRET_LEN, re.I), r"\1" + MASK),
]


def _secret_values():
    """Environment values that must never be echoed back.

    Only UPPERCASE names, matching the same namespace the rule loader
    substitutes from, and only names that look like a credential - masking
    every environment value would blank out ordinary text such as a host name.
    A proxy URL contributes its own user and password separately, so that they
    are still masked if a rule spelled them out itself.
    """
    out = []
    for name, value in os.environ.items():
        if not name.isupper() or not value or len(value) < MIN_SECRET_LEN:
            continue
        if not SECRET_ENV_RE.search(name):
            continue
        out.append(value)
        m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://([^/@\s:]+):([^/@\s]+)@", value)
        if m:
            out.extend(p for p in m.groups() if p and len(p) >= MIN_SECRET_LEN)
    # Longest first: a short secret contained in a longer one must not chop it
    # in half and leave the tail readable.
    return sorted(set(out), key=len, reverse=True)


def mask_secrets(text, values=None):
    if text is None:
        return None
    text = str(text)
    for value in (values if values is not None else _secret_values()):
        if value and value in text:
            text = text.replace(value, MASK)
    for pat, repl in MASK_PATTERNS:
        text = pat.sub(repl, text)
    return text


def mask_proxy(proxy, values=None):
    """Mask the credentials in a proxy URL, keep the host and port readable.

    Which proxy a rule goes through is a routing fact worth seeing when a
    delivery fails; the password in it is not. Masking by whole value would
    replace the entire URL with *** and take the host with it.
    """
    if not proxy:
        return None
    keep = [v for v in (values if values is not None else _secret_values())
            if v and v != str(proxy)]
    return mask_secrets(proxy, keep)


def mask_headers(headers, values=None):
    out = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in SECRET_HEADERS:
            out[name] = MASK
        else:
            out[name] = mask_secrets(value, values)
    return out


# --- the watcher's own RCI session ------------------------------------------
#
# Deliberately a copy of core's challenge-response rather than a call into it:
# the whole point is that this cookie is not core's cookie and this lock is not
# core.rci_lock. Sharing either would bring back exactly the contention this
# replaced. The scheme itself is the router's and does not change:
#   GET /auth -> 401 + X-NDM-Realm + X-NDM-Challenge + Set-Cookie
#   md5(login:realm:password) -> sha256(challenge + md5)
#   POST /auth {"login": ..., "password": sha256}

_sess = {"cookie": None, "logins": 0, "since": None, "last_error": None}
_sess_lock = threading.RLock()


def _session_auth():
    """Log in with our own cookie. Returns True on success."""
    try:
        urllib.request.urlopen(urllib.request.Request("%s/auth" % core.HOST),
                               timeout=LOG_FETCH_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        realm = e.headers.get("X-NDM-Realm", "")
        challenge = e.headers.get("X-NDM-Challenge", "")
        cookie_header = e.headers.get("Set-Cookie", "")
        cookie = cookie_header.split(";")[0] if cookie_header else ""
        md5_pass = hashlib.md5(
            ("%s:%s:%s" % (core.USER, realm, core.PASS)).encode()).hexdigest()
        sha = hashlib.sha256(("%s%s" % (challenge, md5_pass)).encode()).hexdigest()
        payload = json.dumps({"login": core.USER, "password": sha}).encode()
        resp = urllib.request.urlopen(urllib.request.Request(
            "%s/auth" % core.HOST,
            data=payload,
            headers={"Content-Type": "application/json", "Cookie": cookie},
            method="POST",
        ), timeout=LOG_FETCH_TIMEOUT)
        cookie2 = resp.headers.get("Set-Cookie", "")
        if cookie2:
            cookie = cookie2.split(";")[0]
        _sess["cookie"] = cookie
        _sess["logins"] += 1
        _sess["since"] = _now_str()
        _sess["last_error"] = None
        return True
    # No 401 at all means the router is not asking us to authenticate; carry on
    # with whatever cookie we have rather than inventing an error.
    return False


def _session_get(path, timeout):
    """GET /rci/<path> on our own session, one retry after a 401."""
    if not _sess["cookie"]:
        _session_auth()

    def do_request():
        req = urllib.request.Request(
            "%s/rci/%s" % (core.HOST, str(path).strip("/")),
            headers={"Cookie": _sess["cookie"] or ""},
            method="GET",
        )
        # No proxy, ever: this process carries proxy settings in its
        # environment for the outbound side of the rules, and a request to the
        # router that went out through a VPS would be an interesting bug to
        # find later.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)

    try:
        resp = do_request()
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        _sess["cookie"] = None
        _session_auth()
        resp = do_request()
    return resp.read().decode("utf-8", "replace")


def _session_post(commands, timeout):
    """POST /rci/ with a command body, on our own session, one retry after 401.

    Not every read is available as a GET path: 'show log' answers 404 on
    GET /rci/show/log and only exists as the POST form {"show": {"log": {}}}.
    Found the hard way on 2026-08-03, when 2.7.1 first moved the log source to
    the GET client and every log poll came back 404.
    """
    if not _sess["cookie"]:
        _session_auth()
    payload = json.dumps(commands).encode()

    def do_request():
        req = urllib.request.Request(
            "%s/rci/" % core.HOST,
            data=payload,
            headers={"Content-Type": "application/json",
                     "Cookie": _sess["cookie"] or ""},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)

    try:
        resp = do_request()
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        _sess["cookie"] = None
        _session_auth()
        resp = do_request()
    return resp.read().decode("utf-8", "replace")


def fetch_rci(path, timeout=LOG_FETCH_TIMEOUT):
    """Read /rci/<path> as parsed JSON. Takes the watcher's lock, never core's."""
    with _sess_lock:
        try:
            return json.loads(_session_get(path, timeout))
        except Exception as e:
            _sess["last_error"] = "%s %s: %s" % (_now_str(), type(e).__name__, e)
            raise


def fetch_rci_post(commands, timeout=LOG_FETCH_TIMEOUT):
    """Same, for reads that only exist as a POST command body."""
    with _sess_lock:
        try:
            return json.loads(_session_post(commands, timeout))
        except Exception as e:
            _sess["last_error"] = "%s %s: %s" % (_now_str(), type(e).__name__, e)
            raise


# --- rules ------------------------------------------------------------------

def _merge(defaults, rule):
    out = dict(defaults or {})
    out.update(rule or {})
    return out


def _norm_rule(raw, defaults, index):
    rule = _merge(defaults, raw)
    rule["id"] = str(rule.get("id") or "rule%d" % (index + 1))
    rule["source"] = str(rule.get("source") or "log").lower()
    rule["enabled"] = rule.get("enabled", True) is not False
    rule["cooldown"] = int(rule.get("cooldown", DEFAULT_COOLDOWN))
    rule["max_events"] = int(rule.get("max_events", DEFAULT_MAX_EVENTS))
    rule["timeout"] = int(rule.get("timeout", DEFAULT_TIMEOUT))
    rule["method"] = str(rule.get("method", "POST")).upper()

    if rule["source"] == "log":
        if not rule.get("match"):
            raise ValueError("rule '%s': source 'log' needs a 'match' regex" % rule["id"])
        rule["_re"] = re.compile(rule["match"])
        if rule.get("exclude"):
            rule["_re_not"] = re.compile(rule["exclude"])
        rule["interval"] = int(rule.get("interval", core.WATCH_INTERVAL))
    elif rule["source"] == "rci":
        if not rule.get("path"):
            raise ValueError("rule '%s': source 'rci' needs a 'path', e.g. 'show/ip/hotspot'" % rule["id"])
        rule["path"] = str(rule["path"]).strip("/")
        rule["key"] = str(rule.get("key", "id"))
        on = rule.get("on", ["appear"])
        rule["on"] = [on] if isinstance(on, str) else list(on)
        rule["where"] = {_safe_key(k): v for k, v in (rule.get("where") or {}).items()}
        rule["where_not"] = {_safe_key(k): v for k, v in (rule.get("where_not") or {}).items()}
        rule["track"] = [_safe_key(k) for k in (rule.get("track") or [])]
        rule["interval"] = int(rule.get("interval", DEFAULT_RCI_INTERVAL))
    else:
        raise ValueError("rule '%s': unknown source '%s' (log|rci)" % (rule["id"], rule["source"]))

    if not rule.get("url"):
        raise ValueError("rule '%s': no url (set one in the rule or in defaults)" % rule["id"])

    # Secrets and per-installation constants belong in .env, not in a rules file
    # that sits in a git checkout: $NAME in the url, the headers, the proxy or
    # the body is expanded from the environment when the file is loaded. The
    # proxy matters as much as the url - an authenticated proxy carries a
    # password in its own URL - and so does the body, which is where a chat id
    # or an API key usually ends up.
    #
    # Only UPPERCASE names are taken from the environment. Event fields are
    # lowercase (${line}, ${mac}, ${message}), so the two namespaces cannot
    # collide and a stray HOME or PATH in the environment can never eat a
    # placeholder that was meant to be filled in at event time.
    env = {k: v for k, v in os.environ.items() if k.isupper()}
    rule["url"] = Template(str(rule["url"])).safe_substitute(env)
    if rule.get("proxy"):
        rule["proxy"] = Template(str(rule["proxy"])).safe_substitute(env)
    headers = {}
    for k, v in (rule.get("headers") or {}).items():
        headers[str(k)] = Template(str(v)).safe_substitute(env)
    rule["headers"] = headers
    if rule.get("body") is not None:
        rule["body"] = _subst(rule["body"], env)
    return rule


def load_rules(force=False):
    """(rules, reloaded). Never raises: a broken file leaves the old rules."""
    global _rules, _rules_mtime
    path = rules_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _rules or _rules_mtime is not None:
            syslog("WARNING: watcher rules file disappeared: %s" % path)
            with _lock:
                _rules, _rules_mtime = [], None
                _stats["rules_error"] = "file not found: %s" % path
                _stats["rules_mtime"] = None
            return [], True
        with _lock:
            _stats["rules_file"] = path
            _stats["rules_error"] = "file not found: %s" % path
        return [], False

    if not force and mtime == _rules_mtime:
        return _rules, False

    try:
        with open(path) as f:
            doc = json.load(f)
        defaults = doc.get("defaults") or {}
        parsed = []
        for i, raw in enumerate(doc.get("rules") or []):
            parsed.append(_norm_rule(raw, defaults, i))
    except Exception as e:
        with _lock:
            _stats["rules_error"] = "%s: %s" % (type(e).__name__, e)
            _stats["rules_file"] = path
        syslog("ERROR: watcher rules not loaded (%s): %s" % (path, e))
        _rules_mtime = mtime  # do not re-read a broken file every second
        return _rules, False

    with _lock:
        _rules = parsed
        _rules_mtime = mtime
        _stats["rules_file"] = path
        _stats["rules_mtime"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        _stats["rules_error"] = None
        known = {r["id"] for r in parsed}
        for rid in list(_stats["rules"]):
            if rid not in known:
                del _stats["rules"][rid]
        for r in parsed:
            _stats["rules"].setdefault(r["id"], {
                "matches": 0, "sent": 0, "failed": 0,
                "last_match": None, "last_error": None,
            })
    syslog("INFO: watcher loaded %d rule(s) from %s" % (len(parsed), path))
    return parsed, True


# --- outbound ---------------------------------------------------------------

def _subst(obj, mapping):
    if isinstance(obj, str):
        return Template(obj).safe_substitute(mapping)
    if isinstance(obj, dict):
        return {k: _subst(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, mapping) for v in obj]
    return obj


def build_request(rule, mapping):
    """Render a rule into (method, url, headers, body_bytes). No I/O."""
    method = rule["method"]
    url = _subst(str(rule["url"]), mapping)
    headers = {k: _subst(v, mapping) for k, v in (rule.get("headers") or {}).items()}
    body = rule.get("body")
    data = None
    if body is not None and method not in ("GET", "HEAD"):
        rendered = _subst(body, mapping)
        if isinstance(rendered, (dict, list)):
            data = json.dumps(rendered, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        else:
            data = str(rendered).encode("utf-8")
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    return method, url, headers, data


def send(rule, mapping):
    """Perform the rule's HTTP call. Returns (ok, detail)."""
    method, url, headers, data = build_request(rule, mapping)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    handlers = []
    proxy = rule.get("proxy")
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if rule.get("verify_ssl") is False and ssl is not None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()

    try:
        resp = opener.open(req, timeout=rule["timeout"])
        code = resp.getcode()
        resp.read(256)
        return True, "HTTP %s" % code
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(200).decode("utf-8", "replace").strip()
        except Exception:
            pass
        return False, "HTTP %s %s" % (e.code, mask_secrets(detail))
    except Exception as e:
        hint = ""
        if "SSL" in type(e).__name__ or "certificate" in str(e).lower():
            hint = " (https from the router needs ca-certificates; or set \"verify_ssl\": false)"
        # An exception from urllib often quotes the URL it was opening, and that
        # URL carries the token. Errors are stored in get_watch_status and end
        # up in syslog, so they get the same treatment as a rendered request.
        return False, "%s: %s%s" % (type(e).__name__, mask_secrets(e), hint)


def _cooldown_ok(rule, event_key):
    if rule["cooldown"] <= 0:
        return True
    ck = "%s|%s" % (rule["id"], event_key)
    now = time.time()
    last = _state["cooldown"].get(ck, 0)
    if now - last < rule["cooldown"]:
        return False
    _state["cooldown"][ck] = now
    return True


def _prune_cooldowns():
    now = time.time()
    horizon = max([r["cooldown"] for r in _rules] or [DEFAULT_COOLDOWN]) + 60
    for ck in [k for k, ts in _state["cooldown"].items() if now - ts > horizon]:
        del _state["cooldown"][ck]


def fire(rule, mapping, event_key):
    if not _cooldown_ok(rule, event_key):
        return False
    with _lock:
        st = _stats["rules"].setdefault(rule["id"], {
            "matches": 0, "sent": 0, "failed": 0, "last_match": None, "last_error": None})
        st["matches"] += 1
        st["last_match"] = _now_str()
    ok, detail = send(rule, mapping)
    with _lock:
        if ok:
            st["sent"] += 1
            st["last_error"] = None
        else:
            st["failed"] += 1
            st["last_error"] = "%s %s" % (_now_str(), detail)
    if ok:
        syslog("INFO: watcher rule '%s' fired (%s) -> %s" % (rule["id"], event_key, detail))
    else:
        syslog("ERROR: watcher rule '%s' delivery failed (%s): %s" % (rule["id"], event_key, detail))
    return ok


def _base_mapping(rule, event):
    return {
        "rule": rule["id"],
        "event": event,
        "source": rule["source"],
        "now": _now_str(),
        "router": core.HOST,
    }


# --- source: log ------------------------------------------------------------

_LINE_RE = re.compile(r"^\[(?P<label>[^\]]*)\]\s+(?P<log_time>\w{3}\s+\d+\s[\d:]+)?\s*(?P<ident>\S+:)?\s*(?P<text>.*)$")


def _log_fields(line):
    out = {"line": line, "label": "", "log_time": "", "ident": "", "text": line}
    m = _LINE_RE.match(line)
    if m:
        out["label"] = m.group("label") or ""
        out["log_time"] = (m.group("log_time") or "").strip()
        out["ident"] = (m.group("ident") or "").rstrip(":")
        out["text"] = (m.group("text") or "").strip()
    return out


def _log_dict(data):
    """Log entries as {key: entry}, whatever shape RCI wrapped them in.

    POST /rci/ with {"show": {"log": {}}} answers {"show": {"log": ...}};
    GET /rci/show/log answers the inner node on its own, and either form may
    put the entries in a list rather than a numbered dict. All four are the
    same log.
    """
    if isinstance(data, list):
        return {str(i): v for i, v in enumerate(data)}
    if not isinstance(data, dict):
        return {}
    if "show" in data:
        node = _parse_log_dict(data)
    elif "log" in data:
        node = data["log"]
    else:
        node = data
    if isinstance(node, list):
        return {str(i): v for i, v in enumerate(node)}
    return node if isinstance(node, dict) else {}


def _find_new(hashes, anchor):
    """Index of the first unseen line, or None when our place is lost.

    Position is found by content, not by the log's own numbering. The router's
    log is a RAM ring buffer: after a reboot numbering starts over, and lines
    written before NTP answers carry wrong dates. Neither index nor timestamp
    can be trusted; the text can.
    """
    if not anchor:
        return None
    for size in range(len(anchor), 0, -1):
        needle = anchor[-size:]
        for start in range(len(hashes) - size, -1, -1):
            if hashes[start:start + size] == needle:
                return start + size
    return None


def poll_log(rules):
    log_dict = _log_dict(fetch_rci_post({"show": {"log": {}}}, LOG_FETCH_TIMEOUT))
    if not isinstance(log_dict, dict):
        return
    keys = sorted(log_dict.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
    lines = [_format_log_line(log_dict[k]) for k in keys]
    if not lines:
        return

    hashes = [_hash(l) for l in lines]
    anchor = _state["log"].get("anchor") or []
    start = _find_new(hashes, anchor)

    if start is None:
        # First run, or the buffer moved further than we can match (reboot,
        # burst). Adopt the current tail as normal and report nothing: the
        # alternative is replaying a whole boot's worth of lines as news.
        if anchor:
            syslog("INFO: watcher lost its place in the log (reboot or burst), resynced")
        _state["log"]["anchor"] = hashes[-ANCHOR_LEN:]
        _save_state()
        return

    new_lines = lines[start:]
    _state["log"]["anchor"] = hashes[-ANCHOR_LEN:]
    _save_state()
    if not new_lines:
        return
    if len(new_lines) > MAX_NEW_LINES:
        syslog("WARNING: watcher saw %d new log lines, examining the last %d"
               % (len(new_lines), MAX_NEW_LINES))
        new_lines = new_lines[-MAX_NEW_LINES:]

    for rule in rules:
        fired = 0
        for line in new_lines:
            if fired >= rule["max_events"]:
                break
            m = rule["_re"].search(line)
            if not m:
                continue
            if rule.get("_re_not") and rule["_re_not"].search(line):
                continue
            fields = _log_fields(line)
            mapping = {k: _clip(v) for k, v in fields.items()}
            for i, g in enumerate(m.groups(), start=1):
                mapping["m%d" % i] = _clip(g if g is not None else "")
            for name, g in (m.groupdict() or {}).items():
                mapping[_safe_key(name)] = _clip(g if g is not None else "")
            mapping.update(_base_mapping(rule, "log"))
            mapping["message"] = _subst(str(rule.get("message", "${line}")), mapping)
            # Cooldown key "*": for a log rule the cooldown is per RULE, not per
            # line. One web login writes several lines, and a flapping condition
            # writes them forever - the useful limit is "tell me at most once
            # every N seconds", not "once per distinct wording".
            if fire(rule, mapping, "*"):
                fired += 1
    _prune_cooldowns()


# --- source: rci ------------------------------------------------------------

def _extract_items(data, items_key=None):
    node = data.get(items_key) if (items_key and isinstance(data, dict)) else data
    if isinstance(node, list):
        return [x for x in node if isinstance(x, dict)]
    if isinstance(node, dict):
        lists = [v for v in node.values() if isinstance(v, list)]
        if len(lists) == 1:
            return [x for x in lists[0] if isinstance(x, dict)]
        if node and all(isinstance(v, dict) for v in node.values()):
            out = []
            for k, v in node.items():
                item = dict(v)
                item.setdefault("id", k)
                out.append(item)
            return out
    return []


def _flatten(item, prefix="", out=None):
    if out is None:
        out = {}
    for k, v in item.items():
        name = _safe_key("%s_%s" % (prefix, k) if prefix else k)
        if isinstance(v, dict):
            _flatten(v, name, out)
        elif isinstance(v, list):
            out[name] = ",".join(str(x) for x in v if not isinstance(x, (dict, list)))
        else:
            out[name] = v
    return out


def _matches(flat, rule):
    for k, want in rule["where"].items():
        got = flat.get(k, _MISSING)
        wants = want if isinstance(want, list) else [want]
        if not any(_norm(got) == _norm(w) for w in wants):
            return False
    for k, want in rule["where_not"].items():
        got = flat.get(k, _MISSING)
        wants = want if isinstance(want, list) else [want]
        if any(_norm(got) == _norm(w) for w in wants):
            return False
    return True


def _track_hash(flat, rule):
    fields = rule["track"] or [k for k in sorted(flat) if k not in VOLATILE]
    return _hash("|".join("%s=%s" % (f, _norm(flat.get(f, _MISSING))) for f in sorted(fields)))


def poll_rci(path, rules):
    data = fetch_rci(path, LOG_FETCH_TIMEOUT)

    for rule in rules:
        items = _extract_items(data, rule.get("items"))
        current = {}
        flats = {}
        for item in items:
            flat = _flatten(item)
            if not _matches(flat, rule):
                continue
            key = flat.get(_safe_key(rule["key"]))
            if key in (None, ""):
                continue
            key = str(key)
            current[key] = _track_hash(flat, rule)
            flats[key] = flat

        prev = _state["rci"].get(rule["id"])
        _state["rci"][rule["id"]] = current
        if prev is None:
            # First sight of this rule: today's picture is the baseline.
            continue

        events = []
        if "appear" in rule["on"]:
            events += [("appear", k) for k in current if k not in prev]
        if "disappear" in rule["on"]:
            events += [("disappear", k) for k in prev if k not in current]
        if "change" in rule["on"]:
            events += [("change", k) for k in current
                       if k in prev and current[k] != prev[k]]

        for event, key in events[:rule["max_events"]]:
            flat = flats.get(key, {})
            mapping = {k: _clip(v) for k, v in flat.items()}
            # Short aliases for nested fields: mws_ap is also reachable as ${ap}
            # unless the item has an 'ap' of its own. Nesting differs between
            # otherwise identical items (a client on the extender carries mws_*,
            # one on the controller does not) and a message should not have to
            # care which.
            for fk, fv in flat.items():
                if "_" in fk:
                    mapping.setdefault(fk.split("_", 1)[1], _clip(fv))
            mapping["key"] = key
            mapping["path"] = path
            mapping.update(_base_mapping(rule, event))
            default_msg = "${event} ${key} on ${path}"
            mapping["message"] = _subst(str(rule.get("message", default_msg)), mapping)
            fire(rule, mapping, "%s:%s" % (event, key))

    _prune_cooldowns()
    _save_state()


# --- loop -------------------------------------------------------------------

def _plan(rules):
    """{source_key: (interval, callable, argument)} for the enabled rules."""
    plan = {}
    log_rules = [r for r in rules if r["enabled"] and r["source"] == "log"]
    if log_rules:
        interval = max(2, min(r["interval"] for r in log_rules))
        plan["log"] = (interval, poll_log, log_rules)
    by_path = {}
    for r in rules:
        if r["enabled"] and r["source"] == "rci":
            by_path.setdefault(r["path"], []).append(r)
    for path, group in by_path.items():
        interval = max(5, min(r["interval"] for r in group))
        plan["rci:" + path] = (interval, poll_rci, (path, group))
    return plan


def watcher_loop():
    time.sleep(START_DELAY)
    _load_state()
    try:
        with _sess_lock:
            _session_auth()
        syslog("INFO: watcher opened its own RCI session on %s (core.rci_lock "
               "is not taken by the watcher)" % core.HOST)
    except Exception as e:
        # Not fatal: the first poll will try again. Worth a line, because a
        # watcher that cannot log in will look like a watcher with no events.
        _sess["last_error"] = "%s %s: %s" % (_now_str(), type(e).__name__, e)
        syslog("WARNING: watcher could not open its RCI session yet: %s: %s"
               % (type(e).__name__, e))
    with _lock:
        _stats["started"] = _now_str()
    syslog("INFO: watcher started")
    while True:
        try:
            rules, reloaded = load_rules()
            if reloaded:
                _next_run.clear()
            now = time.time()
            for src_key, (interval, fn, arg) in _plan(rules).items():
                if _next_run.get(src_key, 0) > now:
                    continue
                _next_run[src_key] = now + interval
                try:
                    if fn is poll_rci:
                        fn(arg[0], arg[1])
                    else:
                        fn(arg)
                except Exception as e:
                    with _lock:
                        _stats["last_error"] = "%s %s: %s: %s" % (
                            _now_str(), src_key, type(e).__name__, mask_secrets(e))
                    syslog("ERROR: watcher poll %s: %s: %s"
                           % (src_key, type(e).__name__, mask_secrets(e)))
            with _lock:
                _stats["last_cycle"] = _now_str()
        except Exception as e:
            with _lock:
                _stats["last_error"] = "%s cycle: %s: %s" % (
                    _now_str(), type(e).__name__, mask_secrets(e))
            syslog("ERROR: watcher cycle: %s: %s" % (type(e).__name__, mask_secrets(e)))
        time.sleep(1)


def start():
    """Start the watcher thread if it is configured. Returns a status string."""
    if not core.WATCH_ENABLED:
        return "watcher disabled (MCP_WATCH=false)"
    path = rules_path()
    if not os.path.exists(path):
        return "watcher idle: no rules file at %s" % path
    rules, _ = load_rules(force=True)
    if _stats["rules_error"]:
        return "watcher NOT started: %s" % _stats["rules_error"]
    threading.Thread(target=watcher_loop, daemon=True).start()
    enabled = [r["id"] for r in rules if r["enabled"]]
    return "watcher started, %d rule(s) active: %s" % (len(enabled), ", ".join(enabled) or "-")


# --- tools ------------------------------------------------------------------
#
# The watcher registers its own two tools instead of being listed in
# registry.py. One module then carries the whole feature - polling, rules,
# delivery and the tool surface - and a release that only adds a watcher does
# not have to touch the registry, the tool modules or the HTTP policy on a
# router that is currently serving. registry.py stays the place to look for
# everything else.

def tool_get_watch_status(args):
    rules, _ = load_rules()
    with _lock:
        out = {
            "enabled": core.WATCH_ENABLED,
            "running": _stats["started"] is not None,
            "started": _stats["started"],
            "last_cycle": _stats["last_cycle"],
            "last_error": _stats["last_error"],
            "rules_file": _stats["rules_file"] or rules_path(),
            "rules_mtime": _stats["rules_mtime"],
            "rules_error": _stats["rules_error"],
            "state_file": state_path(),
            "poll_interval_default": core.WATCH_INTERVAL,
            # The watcher's own RCI session. session_active false with a
            # session_error set means polls are failing to log in, which looks
            # from the outside exactly like "no events happened".
            "rci_session": {
                "own_session": True,
                "active": bool(_sess["cookie"]),
                "logins": _sess["logins"],
                "since": _sess["since"],
                "last_error": _sess["last_error"],
            },
            "log_anchor": len(_state["log"].get("anchor") or []),
            "rules": [],
        }
        now = time.time()
        for r in rules:
            src_key = "log" if r["source"] == "log" else "rci:" + r["path"]
            st = _stats["rules"].get(r["id"], {})
            out["rules"].append({
                "id": r["id"],
                "source": r["source"],
                "enabled": r["enabled"],
                "target": r["source"] == "log" and r.get("match") or r.get("path"),
                "interval": r["interval"],
                "cooldown": r["cooldown"],
                "next_poll_in": max(0, int(_next_run.get(src_key, now) - now)),
                "tracked_items": len(_state["rci"].get(r["id"]) or {}) if r["source"] == "rci" else None,
                "matches": st.get("matches", 0),
                "sent": st.get("sent", 0),
                "failed": st.get("failed", 0),
                "last_match": st.get("last_match"),
                "last_error": st.get("last_error"),
            })
    return json.dumps(out, indent=2, ensure_ascii=False)


def tool_test_watch_rule(args):
    """Render a rule's HTTP call with sample values, and optionally send it."""
    rule_id = args.get("rule_id")
    dry_run = args.get("dry_run", True)
    rules, _ = load_rules()
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return json.dumps({
            "error": "no rule '%s'" % rule_id,
            "known": [r["id"] for r in rules],
        }, indent=2, ensure_ascii=False)

    mapping = _base_mapping(rule, "test")
    if rule["source"] == "log":
        sample = "[I] %s ndm: watcher test line for rule %s" % (
            datetime.now().strftime("%b %d %H:%M:%S"), rule["id"])
        mapping.update({k: _clip(v) for k, v in _log_fields(sample).items()})
        for i in range(1, 10):
            mapping.setdefault("m%d" % i, "test")
    else:
        mapping.update({
            "key": "00:00:00:00:00:00", "mac": "00:00:00:00:00:00",
            "ip": "192.168.1.255", "name": "watcher test",
            "hostname": "watcher-test", "path": rule["path"],
            "interface_name": "Home",
        })
    mapping["message"] = _subst(str(rule.get("message", "watcher test: ${rule}")), mapping)

    method, url, headers, data = build_request(rule, mapping)
    # The real call below is made from the unmasked values; only what is
    # reported back is masked.
    secrets = _secret_values()
    out = {
        "rule": rule["id"],
        "dry_run": bool(dry_run),
        "request": {
            "method": method,
            "url": mask_secrets(url, secrets),
            "headers": mask_headers(headers, secrets),
            "body": mask_secrets(data.decode("utf-8", "replace"), secrets) if data else None,
            "proxy": mask_proxy(rule.get("proxy"), secrets),
        },
        "secrets_masked": True,
        "message": mask_secrets(mapping["message"], secrets),
    }
    if not dry_run:
        ok, detail = send(rule, mapping)
        out["sent"] = ok
        out["result"] = mask_secrets(detail, secrets)
    return json.dumps(out, indent=2, ensure_ascii=False)


WATCH_TOOLS = {
    "get_watch_status": {
        "description": (
            "Watcher status: whether the background event watcher is running, which "
            "rules file it read, the state of its own RCI session, and per rule - "
            "source, poll interval, cooldown, seconds to the next poll, "
            "match/sent/failed counters and the last delivery error. Use it to check "
            "that a rule is alive without waiting for the event it watches for."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "fn": tool_get_watch_status,
    },
    "test_watch_rule": {
        "description": (
            "Render a watcher rule's outbound HTTP call with sample event values, so "
            "the URL, headers and body can be inspected before a real event fires. "
            "Credentials in the rendered view are masked; the call itself, when sent, "
            "uses the real values. dry_run is TRUE by default and sends nothing; "
            "dry_run=false performs the call for real, which is how to prove the "
            "receiver is reachable from the router."
        ),
        "inputSchema": {"type": "object", "properties": {
            "rule_id": {"type": "string", "description": "Rule id from the rules file"},
            "dry_run": {"type": "boolean", "description": "Default true - render only, do not send"},
        }, "required": ["rule_id"]},
        "fn": tool_test_watch_rule,
    },
}


def register():
    """Publish the watcher's tools. Called on import of this module."""
    from registry import TOOLS
    import http_tools
    TOOLS.update(WATCH_TOOLS)
    # test_watch_rule can send a real request, so the plain-HTTP route must
    # treat it like every other state-changing tool: 403 unless allowlisted.
    http_tools.MUTATING_TOOLS.add("test_watch_rule")


register()
