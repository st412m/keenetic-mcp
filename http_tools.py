"""Plain-HTTP access to the MCP tools.

Home Assistant cannot speak MCP: rest_command, rest sensors and command_line
all do one plain HTTP request and expect a body back. This module backs the
GET /<SECRET>/tool/<name>?arg=value route in server.py so HA can reach the
same tools the LLM uses, without a second implementation of anything.

Policy, in one sentence: a URL is a weak credential, so this route is
READ-ONLY by default and every state-changing tool must be named explicitly
in MCP_HTTP_TOOL_ALLOWLIST before it can be reached this way.
"""

import core
from registry import TOOLS, call_tool


# Tools that change something: router config, client access, a reboot, or an
# outbound transfer. None of these are reachable over the HTTP route unless
# named in MCP_HTTP_TOOL_ALLOWLIST. Keep this list in sync when adding tools —
# a tool NOT listed here is treated as safe to expose.
MUTATING_TOOLS = {
    # WRITE_TOOLS (all default to dry_run=true, but the write path exists)
    "set_port_forwarding",
    "remove_port_forwarding",
    "set_keendns_mapping",
    "remove_keendns_mapping",
    "set_dhcp_host",
    "remove_dhcp_host",
    # State-changing tools without a dry_run guard
    "reboot",
    "register_client",
    "update_client",
    "block_client",
    "unblock_client",
    # Side effects outside the router (rsync to NAS, spawns a worker thread)
    "backup_config",
    "dump_log",
}

# Query parameters consumed by the route itself, never passed to a tool.
RESERVED_PARAMS = {"raw"}

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def is_allowed(name):
    """Return (allowed, reason). reason is None when allowed."""
    if not core.HTTP_TOOLS_ENABLED:
        return False, "HTTP tool route is disabled (MCP_HTTP_TOOLS=false)"
    if name not in TOOLS:
        return False, "unknown tool"
    if name in core.HTTP_TOOL_ALLOWLIST:
        return True, None
    if name in MUTATING_TOOLS:
        return False, (
            "tool changes state and is not exposed over HTTP; add it to "
            "MCP_HTTP_TOOL_ALLOWLIST in .env if you really want this"
        )
    return True, None


def allowed_tools():
    """Names servable over the HTTP route right now, in registry order."""
    return [name for name in TOOLS if is_allowed(name)[0]]


def describe_tools():
    """Listing for GET /<SECRET>/tools."""
    out = []
    for name in allowed_tools():
        spec = TOOLS[name]
        schema = spec.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        out.append({
            "tool": name,
            "description": spec.get("description", ""),
            "params": [
                {
                    "name": key,
                    "type": (props[key] or {}).get("type", "string"),
                    "required": key in required,
                    "description": (props[key] or {}).get("description", ""),
                }
                for key in props
            ],
            "mutating": name in MUTATING_TOOLS,
        })
    return out


def _coerce(key, raw, declared_type):
    """Turn one query-string value into what the tool actually expects.

    Query strings are all text. Tools take ints and bools. Guessing by
    content ("is it digits?") breaks on values that only look numeric, so
    the declared inputSchema type decides — and a value that does not fit
    is an explicit error rather than a silently wrong argument.
    """
    if declared_type == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError("parameter '%s' must be an integer, got %r" % (key, raw))
    if declared_type == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ValueError("parameter '%s' must be a number, got %r" % (key, raw))
    if declared_type == "boolean":
        low = str(raw).strip().lower()
        if low in TRUE_VALUES:
            return True
        if low in FALSE_VALUES:
            return False
        raise ValueError(
            "parameter '%s' must be true/false (also accepts 1/0, yes/no, on/off), got %r"
            % (key, raw))
    return raw


def coerce_args(name, query):
    """Build tool arguments from parse_qs() output. Raises ValueError."""
    schema = TOOLS[name].get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []

    args = {}
    unknown = []
    for key, values in query.items():
        if key in RESERVED_PARAMS:
            continue
        if not values:
            continue
        if key not in props:
            unknown.append(key)
            continue
        # Repeated ?x=1&x=2: last one wins, same as most HTTP stacks.
        args[key] = _coerce(key, values[-1], (props[key] or {}).get("type"))

    if unknown:
        raise ValueError(
            "unknown parameter(s) for %s: %s. Accepted: %s"
            % (name, ", ".join(sorted(unknown)),
               ", ".join(sorted(props)) or "(none)"))

    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(
            "missing required parameter(s) for %s: %s"
            % (name, ", ".join(missing)))

    return args


def run(name, query):
    """Execute a tool for the HTTP route.

    Returns (status_code, payload_dict). The caller decides how to render it.
    """
    allowed, reason = is_allowed(name)
    if not allowed:
        code = 404 if reason == "unknown tool" else 403
        return code, {"ok": False, "tool": name, "error": reason}

    try:
        args = coerce_args(name, query)
    except ValueError as e:
        return 400, {"ok": False, "tool": name, "error": str(e)}

    try:
        result = call_tool(name, args)
    except Exception as e:
        return 500, {"ok": False, "tool": name, "error": "%s: %s" % (type(e).__name__, e)}

    return 200, {"ok": True, "tool": name, "args": args, "result": result}
